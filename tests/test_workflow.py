from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from incident_lab import __version__
from incident_lab.models import Incident, IncidentStatus, StepStatus, WorkflowStatus
from incident_lab.scenarios import get_scenario
from incident_lab.store import SQLiteStore
from incident_lab.workflow import WorkflowStepExecutor, WorkflowWorker
from incident_lab.workflow_store import (
    SCHEMA_VERSION,
    STEP_DEFINITIONS,
    WORKFLOW_VERSION,
    InvalidTransitionError,
    LostLeaseError,
    WorkflowStore,
)


def make_store(tmp_path: Path) -> WorkflowStore:
    path = tmp_path / "workflow.db"
    SQLiteStore(path)
    return WorkflowStore(path, __version__)


def create_incident(store: WorkflowStore, scenario_id: str = "auth-clock-skew") -> Incident:
    summary = get_scenario(scenario_id)["summary"]
    return store.create_run(Incident(
        scenario_id=scenario_id,
        title=summary.title,
        service=summary.service,
        severity=summary.severity,
        symptom=summary.symptom,
        status=IncidentStatus.QUEUED,
    ))


def test_workflow_definition_is_versioned_and_uses_stable_keys(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    run = store.get_run_for_incident(incident.id)
    assert run.workflow_version == WORKFLOW_VERSION == "1.2"
    assert run.schema_version == SCHEMA_VERSION
    assert run.engine_version == __version__
    assert [item[0] for item in STEP_DEFINITIONS] == [
        "triage", "collect_metrics", "collect_logs", "inspect_changes", "form_hypotheses",
        "safety_review", "wait_approval", "execute_remediation", "verify_recovery",
        "generate_postmortem",
    ]


def test_atomic_claim_allows_only_one_worker(tmp_path) -> None:
    store = make_store(tmp_path)
    create_incident(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker: store.claim_next(worker), ["worker-a", "worker-b"]))
    assert sum(claim is not None for claim in claims) == 1


def test_expired_lease_is_recovered_and_stale_commit_is_rejected(tmp_path) -> None:
    store = make_store(tmp_path)
    create_incident(store)
    stale = store.claim_next("dead-worker", lease_seconds=0.02)
    assert stale is not None
    time.sleep(0.03)
    assert store.recover_expired() == 1
    replacement = store.claim_next("replacement")
    assert replacement is not None
    assert replacement.step.id == stale.step.id
    assert replacement.step.attempt_count == 2
    with pytest.raises(LostLeaseError):
        store.complete_step(stale, stale.incident, {"should": "rollback"})
    assert store.list_steps(stale.run.id)[0].status == StepStatus.RUNNING


def test_state_and_event_commit_roll_back_together(tmp_path) -> None:
    store = make_store(tmp_path)
    create_incident(store)
    claim = store.claim_next("rollback-worker")
    assert claim is not None
    with store.connect() as connection:
        connection.execute("""CREATE TRIGGER reject_events BEFORE INSERT ON workflow_events
                            BEGIN SELECT RAISE(ABORT, 'event rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.complete_step(claim, claim.incident, {"value": 1})
    step = store.list_steps(claim.run.id)[0]
    assert step.status == StepStatus.RUNNING
    assert step.output_json == {}


def test_worker_retries_transient_failure_without_duplicate_evidence(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)

    class FailOnce(WorkflowStepExecutor):
        failures = 0

        def execute(self, claim):
            if claim.step.step_key == "collect_metrics" and self.failures == 0:
                self.failures += 1
                raise RuntimeError("temporary metrics outage")
            return super().execute(claim)

    worker = WorkflowWorker(store, "retry-worker", FailOnce(store))
    assert worker.run_once()
    assert worker.run_once()
    time.sleep(0.06)
    assert worker.run_once()
    current = SQLiteStore(store.path).get_incident(incident.id)
    assert len([item for item in current.evidence if item.kind == "metric"]) == 1
    metric_step = store.list_steps(incident.workflow_run_id)[1]
    assert metric_step.attempt_count == 2


def test_worker_heartbeats_long_step_and_enforces_timeout(tmp_path) -> None:
    store = make_store(tmp_path)
    first = create_incident(store)

    class SlowSuccess(WorkflowStepExecutor):
        def execute(self, claim):
            time.sleep(0.08)
            return super().execute(claim)

    worker = WorkflowWorker(
        store, "heartbeat-worker", SlowSuccess(store), lease_seconds=0.03, step_timeout_seconds=0.2
    )
    assert worker.run_once()
    assert store.list_steps(first.workflow_run_id)[0].status == StepStatus.COMPLETED
    store.cancel(first.id)

    second = create_incident(store, "logging-disk-full")

    class TooSlow(WorkflowStepExecutor):
        def execute(self, claim):
            time.sleep(0.2)
            return claim.incident, {}, False

    timed = WorkflowWorker(
        store, "timeout-worker", TooSlow(store), lease_seconds=0.2, step_timeout_seconds=0.03
    )
    started = time.perf_counter()
    assert timed.run_once()
    assert time.perf_counter() - started < 0.15
    assert store.list_steps(second.workflow_run_id)[0].status == StepStatus.RETRY_SCHEDULED


def test_approval_is_final_and_concurrent_requests_are_idempotent(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    worker = WorkflowWorker(store, "approval-worker")
    assert worker.drain() == 7
    waiting = SQLiteStore(store.path).get_incident(incident.id)
    action_id = waiting.actions[0].id
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda operator: store.approve(incident.id, action_id, operator), ["alice", "bob"]))
    assert all(result.status == IncidentStatus.RESUMING for result in results)
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM approval_records").fetchone()[0] == 1
    with pytest.raises(InvalidTransitionError):
        store.approve(incident.id, "different-action", "mallory")
    assert worker.drain() == 3
    resolved = SQLiteStore(store.path).get_incident(incident.id)
    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.actions[0].executed


def test_cancel_running_workflow_finishes_at_safe_boundary(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    claim = store.claim_next("busy-worker")
    assert claim is not None
    assert store.cancel(incident.id).status == IncidentStatus.CANCEL_REQUESTED
    processed, output, pause = WorkflowStepExecutor(store).execute(claim)
    assert store.complete_step(claim, processed, output, pause=pause).status == IncidentStatus.CANCELLED
    assert store.claim_next("other-worker") is None


def test_cancelled_running_failure_does_not_retry(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    claim = store.claim_next("failing-worker")
    assert claim is not None
    store.cancel(incident.id)
    store.fail_step(claim, RuntimeError("worker stopped at cancellation boundary"))
    current = SQLiteStore(store.path).get_incident(incident.id)
    assert current.status == IncidentStatus.CANCELLED
    assert store.get_run_for_incident(incident.id).status == WorkflowStatus.CANCELLED
    assert store.list_steps(incident.workflow_run_id)[0].status == StepStatus.CANCELLED


def test_failed_workflow_can_be_manually_retried(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    claim = store.claim_next("failure-worker")
    assert claim is not None
    with store.connect() as connection:
        connection.execute("UPDATE workflow_steps SET max_attempts=1 WHERE id=?", (claim.step.id,))
    store.fail_step(claim, RuntimeError("permanent failure"))
    assert store.get_run_for_incident(incident.id).status == WorkflowStatus.FAILED
    assert store.retry(incident.id).status == IncidentStatus.QUEUED
    assert store.claim_next("manual-retry") is not None


def test_action_reservation_suppresses_duplicate_execution(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    key = f"{incident.workflow_run_id}:action:test"
    assert store.reserve_action(incident.workflow_run_id, "action-test", key) is None
    assert "duplicate execution suppressed" in store.reserve_action(
        incident.workflow_run_id, "action-test", key
    )
    store.complete_action(key, "done")
    assert store.reserve_action(incident.workflow_run_id, "action-test", key) == "done"


def test_real_worker_process_crash_recovers_from_lease(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "INCIDENT_LAB_DB_PATH": str(store.path),
        "INCIDENT_LAB_LEASE_SECONDS": "0.05",
        "INCIDENT_LAB_STEP_TIMEOUT_SECONDS": "1",
        "INCIDENT_LAB_WORKER_ID": "crash-worker",
    })
    crashed = subprocess.run(
        [sys.executable, "-m", "incident_lab.worker", "--crash-after-claim"],
        env=env,
        check=False,
    )
    assert crashed.returncode == 17
    time.sleep(0.07)
    assert store.recover_expired() == 1
    assert WorkflowWorker(store, "restart-worker").run_once()
    steps = store.list_steps(incident.workflow_run_id)
    assert steps[0].status == StepStatus.COMPLETED
    assert steps[0].attempt_count == 2


def test_event_sequence_is_strictly_ordered(tmp_path) -> None:
    store = make_store(tmp_path)
    incident = create_incident(store)
    WorkflowWorker(store, "event-worker").drain()
    events = store.list_events(incident.workflow_run_id)
    sequences = [event.sequence_number for event in events]
    assert sequences == list(range(1, len(events) + 1))
