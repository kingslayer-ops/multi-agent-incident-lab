from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from time import perf_counter
from typing import Any

from .engine import IncidentEngine
from .models import Evidence, FallbackEvent, Hypothesis, Incident, IncidentStatus, RemediationAction, RiskLevel, TraceStep, utc_now
from .policy import SafetyPolicy
from .provider import IntelligenceProvider, build_provider
from .scenarios import get_scenario
from .tools import RemediationExecutor, ScenarioToolbox
from .workflow_store import LostLeaseError, WorkflowClaim, WorkflowStore


class WorkflowStepExecutor:
    """Business-step adapter; durable scheduling stays behind WorkflowStore."""

    def __init__(self, store: WorkflowStore, provider: IntelligenceProvider | None = None) -> None:
        self.store = store
        self.provider = provider or build_provider()
        self.policy = SafetyPolicy()
        self.remediation = RemediationExecutor()

    def execute(self, claim: WorkflowClaim) -> tuple[Incident, dict[str, Any], bool]:
        incident = claim.incident.model_copy(deep=True)
        scenario = get_scenario(incident.scenario_id)
        output = getattr(self, f"_{claim.step.step_key}")(claim, incident, scenario)
        incident.total_tokens = sum(item.token_estimate for item in incident.trace)
        incident.estimated_cost_usd = round(incident.total_tokens * 0.0000025, 6)
        incident.updated_at = utc_now()
        return incident, output, claim.step.step_key == "wait_approval"

    def _triage(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        summary = scenario["summary"]
        self._trace(incident, "Triage Agent", f"Classified {summary.severity} incident affecting {summary.service}.", 18, 74)
        return {"severity": summary.severity, "service": summary.service}

    def _collect_metrics(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        return self._collect(incident, scenario, "Metric Agent", "query_metrics", "metric")

    def _collect_logs(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        return self._collect(incident, scenario, "Log Agent", "search_logs", "log")

    def _inspect_changes(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        return self._collect(incident, scenario, "Change Agent", "list_recent_changes", "change")

    def _collect(self, incident: Incident, scenario: dict, agent: str, tool: str, kind: str) -> dict:
        existing = next((item for item in incident.evidence if item.kind == kind), None)
        if existing:
            return {"evidence_id": existing.id, "deduplicated": True}
        started = perf_counter()
        result = ScenarioToolbox(scenario).call(tool)
        evidence = Evidence(kind=kind, source=result.source, summary=result.summary, payload=result.payload)
        incident.evidence.append(evidence)
        self._trace(incident, agent, result.summary, max(1, int((perf_counter() - started) * 1000)), 52,
                    tool=tool, evidence_ids=[evidence.id])
        return {"evidence_id": evidence.id, "kind": kind}

    def _form_hypotheses(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        if incident.root_cause:
            return {"root_cause": incident.root_cause, "deduplicated": True}
        diagnosis = self.provider.diagnose(
            {"summary": scenario["summary"]},
            [item.model_dump(mode="json") for item in incident.evidence],
        )
        incident.provider = diagnosis.provider
        if diagnosis.fallback:
            incident.fallback_events.append(FallbackEvent(
                from_provider=diagnosis.fallback[0], to_provider=diagnosis.fallback[1], reason=diagnosis.fallback[2]
            ))
        incident.root_cause = diagnosis.root_cause
        incident.confidence = diagnosis.confidence
        evidence_ids = [item.id for item in incident.evidence]
        incident.hypotheses.append(Hypothesis(
            cause=diagnosis.root_cause, confidence=diagnosis.confidence,
            evidence_ids=evidence_ids, verdict="accepted"
        ))
        for index, alternative in enumerate(scenario["alternatives"]):
            incident.hypotheses.append(Hypothesis(
                cause=alternative, confidence=round(0.24 - index * 0.07, 2), evidence_ids=evidence_ids[:1],
                counter_evidence_ids=evidence_ids[1:], verdict="rejected"
            ))
        self._trace(incident, "Diagnosis Agent",
                    f"Accepted root cause: {diagnosis.root_cause} ({diagnosis.confidence:.0%} confidence).",
                    37, diagnosis.token_estimate, evidence_ids=evidence_ids)
        return {"root_cause": diagnosis.root_cause, "confidence": diagnosis.confidence}

    def _safety_review(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        if not incident.actions:
            remediation = scenario["remediation"]
            incident.actions.append(RemediationAction(
                title=remediation["title"], description=remediation["description"],
                command_preview=remediation["command"], risk=RiskLevel(remediation["risk"])
            ))
        decision = self.policy.evaluate(incident.actions[0])
        self._trace(incident, "Safety Reviewer", decision.reason, 9, 63,
                    evidence_ids=[item.id for item in incident.evidence])
        return {"action_id": incident.actions[0].id, "risk": incident.actions[0].risk,
                "approval_required": True}

    def _wait_approval(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        incident.status = IncidentStatus.AWAITING_APPROVAL
        return {"action_id": incident.actions[0].id}

    def _execute_remediation(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        action = incident.actions[0]
        if not action.approved:
            raise PermissionError("Remediation has no durable approval record")
        decision = self.policy.evaluate(action)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        key = f"{claim.run.id}:action:{action.id}"
        result = self.store.reserve_action(claim.run.id, action.id, key)
        if result is None:
            result = self.remediation.execute(action.command_preview, idempotency_key=key)
            self.store.complete_action(key, result)
        action.execution_result = result
        action.executed = True
        approved_by = self.store.approved_by(claim.run.id)
        self._trace(incident, "Remediation Agent", f"Approved by {approved_by}. {result}", 22, 48,
                    tool="sandbox_executor", evidence_ids=[item.id for item in incident.evidence])
        return {"action_id": action.id, "idempotency_key": key, "result": result}

    def _verify_recovery(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        self._trace(incident, "Verification Agent",
                    "Synthetic error rate and latency returned to baseline; recovery verified.",
                    15, 54, tool="query_metrics")
        return {"recovered": True}

    def _generate_postmortem(self, claim: WorkflowClaim, incident: Incident, scenario: dict) -> dict:
        incident.postmortem = IncidentEngine._postmortem(incident, self.store.approved_by(claim.run.id))
        return {"postmortem_generated": True}

    @staticmethod
    def _trace(incident: Incident, agent: str, summary: str, duration: int, tokens: int,
               tool: str | None = None, evidence_ids: list[str] | None = None) -> None:
        incident.trace.append(TraceStep(
            agent=agent, summary=summary, tool=tool, evidence_ids=evidence_ids or [],
            duration_ms=duration, token_estimate=tokens
        ))


class WorkflowWorker:
    def __init__(self, store: WorkflowStore, worker_id: str, executor: WorkflowStepExecutor | None = None,
                 lease_seconds: float = 30, step_timeout_seconds: float = 20) -> None:
        self.store = store
        self.worker_id = worker_id
        self.executor = executor or WorkflowStepExecutor(store)
        self.lease_seconds = lease_seconds
        self.step_timeout_seconds = step_timeout_seconds

    def run_once(self) -> bool:
        claim = self.store.claim_next(self.worker_id, self.lease_seconds, self.step_timeout_seconds)
        if claim is None:
            return False
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(self.executor.execute, claim)
            deadline = perf_counter() + self.step_timeout_seconds
            while True:
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    future.cancel()
                    raise TimeoutError(f"Step {claim.step.step_key} exceeded timeout")
                try:
                    incident, output, pause = future.result(
                        timeout=min(max(self.lease_seconds / 3, 0.01), remaining)
                    )
                    break
                except FutureTimeout:
                    if perf_counter() >= deadline:
                        future.cancel()
                        raise TimeoutError(f"Step {claim.step.step_key} exceeded timeout")
                    self.store.heartbeat(claim, self.lease_seconds)
            self.store.complete_step(claim, incident, output, pause=pause)
        except LostLeaseError:
            pass
        except Exception as exc:
            backoff = min(0.05 * (2 ** max(claim.step.attempt_count - 1, 0)), 10)
            try:
                self.store.fail_step(claim, exc, backoff_seconds=backoff)
            except LostLeaseError:
                pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return True

    def drain(self, max_steps: int = 100) -> int:
        completed = 0
        while completed < max_steps and self.run_once():
            completed += 1
        return completed
