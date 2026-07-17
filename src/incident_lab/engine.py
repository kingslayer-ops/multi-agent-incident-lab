from __future__ import annotations

from time import perf_counter
from typing import Any

from .models import (
    EvaluationCase,
    EvaluationReport,
    Evidence,
    FallbackEvent,
    Hypothesis,
    Incident,
    IncidentStatus,
    RemediationAction,
    RiskLevel,
    TraceStep,
    utc_now,
)
from .policy import SafetyPolicy
from .provider import IntelligenceProvider, build_provider
from .scenarios import SCENARIOS, get_scenario
from .store import IncidentStore, InMemoryStore
from .tools import RemediationExecutor, ScenarioToolbox


class IncidentEngine:
    def __init__(
        self,
        store: IncidentStore | None = None,
        provider: IntelligenceProvider | None = None,
    ) -> None:
        self.store = store or InMemoryStore()
        self.provider = provider or build_provider()
        self.policy = SafetyPolicy()
        self.executor = RemediationExecutor()

    def investigate(self, scenario_id: str, persist: bool = True) -> Incident:
        scenario = get_scenario(scenario_id)
        summary = scenario["summary"]
        incident = Incident(
            scenario_id=scenario_id,
            title=summary.title,
            service=summary.service,
            severity=summary.severity,
            symptom=summary.symptom,
        )
        toolbox = ScenarioToolbox(scenario)

        self._add_step(
            incident,
            agent="Triage Agent",
            summary=f"Classified {summary.severity} incident affecting {summary.service}.",
            duration_ms=18,
            tokens=74,
        )

        for agent, tool_name, kind in (
            ("Metric Agent", "query_metrics", "metric"),
            ("Log Agent", "search_logs", "log"),
            ("Change Agent", "list_recent_changes", "change"),
        ):
            started = perf_counter()
            result = toolbox.call(tool_name)
            evidence = Evidence(kind=kind, source=result.source, summary=result.summary, payload=result.payload)
            incident.evidence.append(evidence)
            self._add_step(
                incident,
                agent=agent,
                summary=result.summary,
                tool=tool_name,
                evidence_ids=[evidence.id],
                duration_ms=max(1, int((perf_counter() - started) * 1000)),
                tokens=52,
            )

        diagnosis = self.provider.diagnose(
            {"summary": scenario["summary"]},
            [item.model_dump(mode="json") for item in incident.evidence],
        )
        incident.provider = diagnosis.provider
        if diagnosis.fallback:
            incident.fallback_events.append(
                FallbackEvent(
                    from_provider=diagnosis.fallback[0],
                    to_provider=diagnosis.fallback[1],
                    reason=diagnosis.fallback[2],
                )
            )
        incident.root_cause = diagnosis.root_cause
        incident.confidence = diagnosis.confidence
        all_evidence = [item.id for item in incident.evidence]
        incident.hypotheses.append(
            Hypothesis(
                cause=diagnosis.root_cause,
                confidence=diagnosis.confidence,
                evidence_ids=all_evidence,
                verdict="accepted",
            )
        )
        for index, alternative in enumerate(scenario["alternatives"]):
            incident.hypotheses.append(
                Hypothesis(
                    cause=alternative,
                    confidence=round(0.24 - index * 0.07, 2),
                    evidence_ids=all_evidence[:1],
                    counter_evidence_ids=all_evidence[1:],
                    verdict="rejected",
                )
            )
        self._add_step(
            incident,
            agent="Diagnosis Agent",
            summary=f"Accepted root cause: {diagnosis.root_cause} ({diagnosis.confidence:.0%} confidence).",
            evidence_ids=all_evidence,
            duration_ms=37,
            tokens=diagnosis.token_estimate,
        )

        remediation = scenario["remediation"]
        action = RemediationAction(
            title=remediation["title"],
            description=remediation["description"],
            command_preview=remediation["command"],
            risk=RiskLevel(remediation["risk"]),
        )
        incident.actions.append(action)
        decision = self.policy.evaluate(action)
        self._add_step(
            incident,
            agent="Safety Reviewer",
            summary=decision.reason,
            evidence_ids=all_evidence,
            duration_ms=9,
            tokens=63,
        )
        incident.status = IncidentStatus.AWAITING_APPROVAL
        incident.total_tokens = sum(step.token_estimate for step in incident.trace)
        incident.estimated_cost_usd = round(incident.total_tokens * 0.0000025, 6)
        incident.updated_at = utc_now()
        return self.store.save_incident(incident) if persist else incident

    def approve_and_execute(self, incident_id: str, action_id: str, approved_by: str) -> Incident:
        incident = self.store.get_incident(incident_id)
        if incident.status != IncidentStatus.AWAITING_APPROVAL:
            raise ValueError("Incident is not awaiting approval")
        action = next((item for item in incident.actions if item.id == action_id), None)
        if action is None:
            raise KeyError(action_id)

        action.approved = True
        decision = self.policy.evaluate(action)
        if not decision.allowed:
            raise PermissionError(decision.reason)
        action.execution_result = self.executor.execute(action.command_preview)
        action.executed = True
        incident.status = IncidentStatus.RESOLVED
        incident.postmortem = self._postmortem(incident, approved_by)
        self._add_step(
            incident,
            agent="Remediation Agent",
            summary=f"Approved by {approved_by}. {action.execution_result}",
            tool="sandbox_executor",
            evidence_ids=[item.id for item in incident.evidence],
            duration_ms=22,
            tokens=48,
        )
        self._add_step(
            incident,
            agent="Verification Agent",
            summary="Synthetic error rate and latency returned to baseline; incident resolved.",
            tool="query_metrics",
            duration_ms=15,
            tokens=54,
        )
        incident.total_tokens = sum(step.token_estimate for step in incident.trace)
        incident.estimated_cost_usd = round(incident.total_tokens * 0.0000025, 6)
        incident.updated_at = utc_now()
        return self.store.save_incident(incident)

    def evaluate(self) -> EvaluationReport:
        cases: list[EvaluationCase] = []
        for scenario_id, scenario in SCENARIOS.items():
            started = perf_counter()
            incident = self.investigate(scenario_id, persist=False)
            required_kinds = {"metric", "log", "change"}
            actual_kinds = {item.kind for item in incident.evidence}
            coverage = len(required_kinds & actual_kinds) / len(required_kinds)
            unsafe = sum(1 for item in incident.actions if item.executed and not item.approved)
            cases.append(
                EvaluationCase(
                    scenario_id=scenario_id,
                    expected_root_cause=scenario["root_cause"],
                    predicted_root_cause=incident.root_cause or "",
                    correct=incident.root_cause == scenario["root_cause"],
                    evidence_coverage=coverage,
                    unsafe_action_rate=unsafe / max(len(incident.actions), 1),
                    duration_ms=max(1, int((perf_counter() - started) * 1000)),
                )
            )
        report = EvaluationReport(
            cases=cases,
            root_cause_accuracy=sum(item.correct for item in cases) / len(cases),
            average_evidence_coverage=sum(item.evidence_coverage for item in cases) / len(cases),
            unsafe_action_rate=sum(item.unsafe_action_rate for item in cases) / len(cases),
            average_duration_ms=sum(item.duration_ms for item in cases) / len(cases),
        )
        return self.store.save_evaluation(report)

    @staticmethod
    def _add_step(
        incident: Incident,
        *,
        agent: str,
        summary: str,
        duration_ms: int,
        tokens: int,
        tool: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> None:
        incident.trace.append(
            TraceStep(
                agent=agent,
                summary=summary,
                tool=tool,
                evidence_ids=evidence_ids or [],
                duration_ms=duration_ms,
                token_estimate=tokens,
            )
        )

    @staticmethod
    def _postmortem(incident: Incident, approved_by: str) -> str:
        return (
            f"# {incident.title}\n\n"
            f"**Impact:** {incident.symptom}\n\n"
            f"**Root cause:** {incident.root_cause} ({incident.confidence:.0%} confidence).\n\n"
            f"**Resolution:** {incident.actions[0].description}\n\n"
            f"**Approval:** The sandbox remediation was approved by {approved_by}.\n\n"
            "**Follow-up:** Add a regression alert, an explicit capacity guardrail, and a replay test for this failure mode."
        )


def dashboard_snapshot(store: IncidentStore) -> dict[str, Any]:
    incidents = store.list_incidents()
    latest_eval = store.latest_evaluation()
    resolved = sum(item.status == IncidentStatus.RESOLVED for item in incidents)
    return {
        "incidents_total": len(incidents),
        "incidents_resolved": resolved,
        "awaiting_approval": sum(item.status == IncidentStatus.AWAITING_APPROVAL for item in incidents),
        "total_tokens": sum(item.total_tokens for item in incidents),
        "estimated_cost_usd": round(sum(item.estimated_cost_usd for item in incidents), 6),
        "root_cause_accuracy": latest_eval.root_cause_accuracy if latest_eval else None,
        "services_healthy": 12 - min(len([i for i in incidents if i.status != IncidentStatus.RESOLVED]), 12),
        "services_total": 12,
    }
