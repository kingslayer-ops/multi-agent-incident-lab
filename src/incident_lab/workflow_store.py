from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .models import Incident, IncidentStatus, StepStatus, WorkflowEvent, WorkflowRun, WorkflowStatus, WorkflowStep


WORKFLOW_NAME = "incident_investigation"
WORKFLOW_VERSION = "1.2"
SCHEMA_VERSION = 1

STEP_DEFINITIONS = (
    ("triage", "Triage Agent"),
    ("collect_metrics", "Metric Agent"),
    ("collect_logs", "Log Agent"),
    ("inspect_changes", "Change Agent"),
    ("form_hypotheses", "Diagnosis Agent"),
    ("safety_review", "Safety Reviewer"),
    ("wait_approval", "Human Approval"),
    ("execute_remediation", "Remediation Agent"),
    ("verify_recovery", "Verification Agent"),
    ("generate_postmortem", "Workflow Engine"),
)

TERMINAL = {WorkflowStatus.RESOLVED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class WorkflowClaim:
    run: WorkflowRun
    step: WorkflowStep
    incident: Incident
    lease_version: int


class LostLeaseError(RuntimeError):
    pass


class InvalidTransitionError(RuntimeError):
    pass


class WorkflowStore:
    """SQLite-backed durable queue, checkpoint store, and transactional event log."""

    def __init__(self, path: str | Path, engine_version: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine_version = engine_version
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL UNIQUE REFERENCES incidents(id),
                    workflow_name TEXT NOT NULL,
                    workflow_version TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    engine_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step_key TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    cancel_requested_at TEXT,
                    failure_code TEXT,
                    failure_message TEXT,
                    version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    id TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
                    step_key TEXT NOT NULL,
                    step_order INTEGER NOT NULL,
                    agent_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    policy_json TEXT NOT NULL DEFAULT '{}',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    next_retry_at TEXT,
                    timeout_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    error_type TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(workflow_run_id, step_key)
                );
                CREATE TABLE IF NOT EXISTS workflow_step_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(id),
                    attempt_number INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    error_message TEXT,
                    UNIQUE(workflow_step_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
                    incident_id TEXT NOT NULL REFERENCES incidents(id),
                    event_type TEXT NOT NULL,
                    step_key TEXT,
                    sequence_number INTEGER NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(workflow_run_id, sequence_number)
                );
                CREATE TABLE IF NOT EXISTS approval_records (
                    workflow_run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id),
                    action_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_executions (
                    idempotency_key TEXT PRIMARY KEY,
                    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
                    action_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_steps_runnable
                    ON workflow_steps(status, available_at, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_events_run_id
                    ON workflow_events(workflow_run_id, id);
                """
            )

    def create_run(self, incident: Incident) -> Incident:
        now = utc_now()
        run_id = f"run-{uuid4().hex[:10]}"
        incident.workflow_run_id = run_id
        incident.status = IncidentStatus.QUEUED
        incident.current_step_key = STEP_DEFINITIONS[0][0]
        incident.updated_at = now
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._save_incident(connection, incident)
                connection.execute(
                    """INSERT INTO workflow_runs
                    (id, incident_id, workflow_name, workflow_version, schema_version, engine_version,
                     status, current_step_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, incident.id, WORKFLOW_NAME, WORKFLOW_VERSION, SCHEMA_VERSION,
                     self.engine_version, WorkflowStatus.QUEUED, STEP_DEFINITIONS[0][0],
                     now.isoformat(), now.isoformat()),
                )
                self._insert_step(connection, run_id, 0, now)
                self._event(connection, run_id, incident.id, "run.created", STEP_DEFINITIONS[0][0],
                            {"status": WorkflowStatus.QUEUED, "workflow_version": WORKFLOW_VERSION})
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return incident.model_copy(deep=True)

    def claim_next(self, worker_id: str, lease_seconds: float = 30, timeout_seconds: float = 20) -> WorkflowClaim | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._recover_expired(connection, now)
                row = connection.execute(
                    """SELECT s.*, r.incident_id FROM workflow_steps s
                    JOIN workflow_runs r ON r.id=s.workflow_run_id
                    WHERE s.status IN (?, ?) AND s.available_at <= ?
                      AND r.status NOT IN (?, ?, ?)
                    ORDER BY s.available_at, s.step_order LIMIT 1""",
                    (StepStatus.QUEUED, StepStatus.RETRY_SCHEDULED, now.isoformat(),
                     WorkflowStatus.RESOLVED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                lease_expires = now + timedelta(seconds=lease_seconds)
                timeout_at = now + timedelta(seconds=timeout_seconds)
                updated = connection.execute(
                    """UPDATE workflow_steps SET status=?, lease_owner=?, lease_expires_at=?, heartbeat_at=?,
                    timeout_at=?, started_at=COALESCE(started_at, ?), attempt_count=attempt_count+1,
                    version=version+1 WHERE id=? AND status IN (?, ?)""",
                    (StepStatus.RUNNING, worker_id, lease_expires.isoformat(), now.isoformat(),
                     timeout_at.isoformat(), now.isoformat(), row["id"], StepStatus.QUEUED,
                     StepStatus.RETRY_SCHEDULED),
                )
                if updated.rowcount != 1:
                    connection.rollback()
                    return None
                step_row = connection.execute("SELECT * FROM workflow_steps WHERE id=?", (row["id"],)).fetchone()
                attempt = step_row["attempt_count"]
                connection.execute(
                    """INSERT INTO workflow_step_attempts
                    (workflow_step_id, attempt_number, worker_id, status, started_at)
                    VALUES (?, ?, ?, 'running', ?)""",
                    (row["id"], attempt, worker_id, now.isoformat()),
                )
                connection.execute(
                    """UPDATE workflow_runs SET status=?, current_step_key=?, started_at=COALESCE(started_at, ?),
                    updated_at=?, version=version+1 WHERE id=?""",
                    (WorkflowStatus.RUNNING, row["step_key"], now.isoformat(), now.isoformat(), row["workflow_run_id"]),
                )
                incident = self._load_incident(connection, row["incident_id"])
                incident.status = IncidentStatus.RUNNING
                incident.current_step_key = row["step_key"]
                incident.retry_count = max(0, attempt - 1)
                incident.updated_at = now
                self._save_incident(connection, incident)
                self._event(connection, row["workflow_run_id"], row["incident_id"], "step.started",
                            row["step_key"], {"worker_id": worker_id, "attempt": attempt})
                run = self._run_from_row(connection.execute(
                    "SELECT * FROM workflow_runs WHERE id=?", (row["workflow_run_id"],)
                ).fetchone())
                step = self._step_from_row(step_row)
                connection.commit()
                return WorkflowClaim(run, step, incident, step.version)
            except Exception:
                connection.rollback()
                raise

    def heartbeat(self, claim: WorkflowClaim, lease_seconds: float = 30) -> None:
        now = utc_now()
        with self.connect() as connection:
            updated = connection.execute(
                """UPDATE workflow_steps SET heartbeat_at=?, lease_expires_at=?
                WHERE id=? AND status=? AND lease_owner=? AND version=?""",
                (now.isoformat(), (now + timedelta(seconds=lease_seconds)).isoformat(), claim.step.id,
                 StepStatus.RUNNING, claim.step.lease_owner, claim.lease_version),
            )
            if updated.rowcount != 1:
                raise LostLeaseError(claim.step.id)

    def complete_step(self, claim: WorkflowClaim, incident: Incident, output: dict, *, pause: bool = False) -> Incident:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute("SELECT * FROM workflow_steps WHERE id=?", (claim.step.id,)).fetchone()
                if (current is None or current["status"] != StepStatus.RUNNING
                        or current["lease_owner"] != claim.step.lease_owner
                        or current["version"] != claim.lease_version
                        or dt(current["lease_expires_at"]) < now):
                    raise LostLeaseError(claim.step.id)
                run_row = connection.execute(
                    "SELECT status FROM workflow_runs WHERE id=?", (claim.run.id,)
                ).fetchone()
                if run_row["status"] == WorkflowStatus.CANCEL_REQUESTED:
                    connection.execute(
                        """UPDATE workflow_steps SET status=?, output_json=?, finished_at=?, lease_owner=NULL,
                        lease_expires_at=NULL, heartbeat_at=NULL, version=version+1 WHERE id=?""",
                        (StepStatus.COMPLETED, json.dumps(output), now.isoformat(), claim.step.id),
                    )
                    connection.execute(
                        """UPDATE workflow_step_attempts SET status='completed', finished_at=?
                        WHERE workflow_step_id=? AND attempt_number=?""",
                        (now.isoformat(), claim.step.id, current["attempt_count"]),
                    )
                    incident.status = IncidentStatus.CANCELLED
                    incident.current_step_key = None
                    incident.updated_at = now
                    self._save_incident(connection, incident)
                    connection.execute(
                        """UPDATE workflow_runs SET status=?, current_step_key=NULL, updated_at=?,
                        finished_at=?, version=version+1 WHERE id=?""",
                        (WorkflowStatus.CANCELLED, now.isoformat(), now.isoformat(), claim.run.id),
                    )
                    self._event(connection, claim.run.id, incident.id, "run.cancelled", claim.step.step_key, {})
                    connection.commit()
                    return incident.model_copy(deep=True)
                step_status = StepStatus.WAITING_APPROVAL if pause else StepStatus.COMPLETED
                connection.execute(
                    """UPDATE workflow_steps SET status=?, output_json=?, finished_at=?, lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, error_type=NULL, error_code=NULL,
                    error_message=NULL, version=version+1 WHERE id=?""",
                    (step_status, json.dumps(output), now.isoformat(), claim.step.id),
                )
                connection.execute(
                    """UPDATE workflow_step_attempts SET status='completed', finished_at=?
                    WHERE workflow_step_id=? AND attempt_number=?""",
                    (now.isoformat(), claim.step.id, current["attempt_count"]),
                )
                next_index = claim.step.step_order + 1
                if pause:
                    run_status = WorkflowStatus.WAITING_APPROVAL
                    incident.status = IncidentStatus.AWAITING_APPROVAL
                    next_key = claim.step.step_key
                    event_type = "approval.requested"
                elif next_index < len(STEP_DEFINITIONS):
                    self._insert_step(connection, claim.run.id, next_index, now)
                    next_key = STEP_DEFINITIONS[next_index][0]
                    run_status = WorkflowStatus.RUNNING
                    incident.status = IncidentStatus.RUNNING
                    event_type = "step.completed"
                else:
                    next_key = None
                    run_status = WorkflowStatus.RESOLVED
                    incident.status = IncidentStatus.RESOLVED
                    event_type = "run.resolved"
                incident.current_step_key = next_key
                incident.updated_at = now
                self._save_incident(connection, incident)
                connection.execute(
                    """UPDATE workflow_runs SET status=?, current_step_key=?, updated_at=?,
                    finished_at=CASE WHEN ?=? THEN ? ELSE finished_at END, version=version+1 WHERE id=?""",
                    (run_status, next_key, now.isoformat(), run_status, WorkflowStatus.RESOLVED,
                     now.isoformat(), claim.run.id),
                )
                self._event(connection, claim.run.id, incident.id, event_type, claim.step.step_key,
                            {"status": run_status, "attempt": current["attempt_count"], **output})
                connection.commit()
                return incident.model_copy(deep=True)
            except Exception:
                connection.rollback()
                raise

    def fail_step(self, claim: WorkflowClaim, error: Exception, backoff_seconds: float = 0) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute("SELECT * FROM workflow_steps WHERE id=?", (claim.step.id,)).fetchone()
                if (
                    current is None
                    or current["status"] != StepStatus.RUNNING
                    or current["lease_owner"] != claim.step.lease_owner
                    or current["version"] != claim.lease_version
                    or not current["lease_expires_at"]
                    or dt(current["lease_expires_at"]) <= now
                ):
                    raise LostLeaseError(claim.step.id)
                run = connection.execute(
                    "SELECT status FROM workflow_runs WHERE id=?", (claim.run.id,)
                ).fetchone()
                if run["status"] == WorkflowStatus.CANCEL_REQUESTED:
                    connection.execute(
                        """UPDATE workflow_steps SET status=?, finished_at=?, lease_owner=NULL,
                        lease_expires_at=NULL, heartbeat_at=NULL, version=version+1 WHERE id=?""",
                        (StepStatus.CANCELLED, now.isoformat(), claim.step.id),
                    )
                    connection.execute(
                        """UPDATE workflow_step_attempts SET status='cancelled', finished_at=?, error_message=?
                        WHERE workflow_step_id=? AND attempt_number=?""",
                        (now.isoformat(), str(error), claim.step.id, current["attempt_count"]),
                    )
                    incident = self._load_incident(connection, claim.run.incident_id)
                    incident.status = IncidentStatus.CANCELLED
                    incident.current_step_key = None
                    incident.updated_at = now
                    self._save_incident(connection, incident)
                    connection.execute(
                        """UPDATE workflow_runs SET status=?, current_step_key=NULL, updated_at=?,
                        finished_at=?, version=version+1 WHERE id=?""",
                        (WorkflowStatus.CANCELLED, now.isoformat(), now.isoformat(), claim.run.id),
                    )
                    self._event(connection, claim.run.id, incident.id, "run.cancelled", claim.step.step_key, {})
                    connection.commit()
                    return
                retry = current["attempt_count"] < current["max_attempts"]
                available = now + timedelta(seconds=backoff_seconds)
                step_status = StepStatus.RETRY_SCHEDULED if retry else StepStatus.FAILED
                run_status = WorkflowStatus.RETRY_SCHEDULED if retry else WorkflowStatus.FAILED
                connection.execute(
                    """UPDATE workflow_steps SET status=?, available_at=?, next_retry_at=?, lease_owner=NULL,
                    lease_expires_at=NULL, heartbeat_at=NULL, error_type=?, error_code=?, error_message=?,
                    version=version+1 WHERE id=?""",
                    (step_status, available.isoformat(), available.isoformat() if retry else None,
                     type(error).__name__, "STEP_EXECUTION_ERROR", str(error), claim.step.id),
                )
                connection.execute(
                    """UPDATE workflow_step_attempts SET status='failed', finished_at=?, error_message=?
                    WHERE workflow_step_id=? AND attempt_number=?""",
                    (now.isoformat(), str(error), claim.step.id, current["attempt_count"]),
                )
                incident = self._load_incident(connection, claim.run.incident_id)
                incident.status = IncidentStatus.RETRY_SCHEDULED if retry else IncidentStatus.FAILED
                incident.retry_count = current["attempt_count"]
                incident.updated_at = now
                self._save_incident(connection, incident)
                connection.execute(
                    """UPDATE workflow_runs SET status=?, updated_at=?, failure_code=?, failure_message=?,
                    finished_at=CASE WHEN ?=? THEN ? ELSE finished_at END, version=version+1 WHERE id=?""",
                    (run_status, now.isoformat(), None if retry else "STEP_FAILED", str(error),
                     run_status, WorkflowStatus.FAILED, now.isoformat(), claim.run.id),
                )
                self._event(connection, claim.run.id, incident.id,
                            "step.retry_scheduled" if retry else "run.failed", claim.step.step_key,
                            {"attempt": current["attempt_count"], "error": str(error)})
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def approve(self, incident_id: str, action_id: str, approved_by: str) -> Incident:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run_row = connection.execute("SELECT * FROM workflow_runs WHERE incident_id=?", (incident_id,)).fetchone()
                if run_row is None:
                    raise KeyError(incident_id)
                existing = connection.execute("SELECT * FROM approval_records WHERE workflow_run_id=?", (run_row["id"],)).fetchone()
                incident = self._load_incident(connection, incident_id)
                if existing:
                    if existing["decision"] == "approved" and existing["action_id"] == action_id:
                        connection.commit()
                        return incident
                    raise InvalidTransitionError("Approval already has a final decision")
                if run_row["status"] != WorkflowStatus.WAITING_APPROVAL:
                    raise InvalidTransitionError("Incident is not waiting for approval")
                action = next((item for item in incident.actions if item.id == action_id), None)
                if action is None:
                    raise KeyError(action_id)
                wait_step = connection.execute(
                    "SELECT * FROM workflow_steps WHERE workflow_run_id=? AND step_key='wait_approval'",
                    (run_row["id"],),
                ).fetchone()
                connection.execute(
                    "INSERT INTO approval_records VALUES (?, ?, 'approved', ?, ?)",
                    (run_row["id"], action_id, approved_by, now.isoformat()),
                )
                connection.execute(
                    "UPDATE workflow_steps SET status=?, finished_at=?, version=version+1 WHERE id=?",
                    (StepStatus.COMPLETED, now.isoformat(), wait_step["id"]),
                )
                self._insert_step(connection, run_row["id"], wait_step["step_order"] + 1, now)
                action.approved = True
                incident.status = IncidentStatus.RESUMING
                incident.current_step_key = "execute_remediation"
                incident.updated_at = now
                self._save_incident(connection, incident)
                connection.execute(
                    """UPDATE workflow_runs SET status=?, current_step_key='execute_remediation',
                    updated_at=?, version=version+1 WHERE id=?""",
                    (WorkflowStatus.RESUMING, now.isoformat(), run_row["id"]),
                )
                self._event(connection, run_row["id"], incident_id, "run.resumed", "wait_approval",
                            {"approved_by": approved_by, "action_id": action_id})
                connection.commit()
                return incident.model_copy(deep=True)
            except Exception:
                connection.rollback()
                raise

    def cancel(self, incident_id: str) -> Incident:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = connection.execute("SELECT * FROM workflow_runs WHERE incident_id=?", (incident_id,)).fetchone()
                if run is None:
                    raise KeyError(incident_id)
                incident = self._load_incident(connection, incident_id)
                if WorkflowStatus(run["status"]) in TERMINAL:
                    connection.commit()
                    return incident
                running = connection.execute(
                    "SELECT 1 FROM workflow_steps WHERE workflow_run_id=? AND status=?", (run["id"], StepStatus.RUNNING)
                ).fetchone()
                status = WorkflowStatus.CANCEL_REQUESTED if running else WorkflowStatus.CANCELLED
                incident.status = IncidentStatus.CANCEL_REQUESTED if running else IncidentStatus.CANCELLED
                incident.updated_at = now
                self._save_incident(connection, incident)
                connection.execute(
                    """UPDATE workflow_runs SET status=?, cancel_requested_at=?, updated_at=?,
                    finished_at=CASE WHEN ?=? THEN ? ELSE finished_at END, version=version+1 WHERE id=?""",
                    (status, now.isoformat(), now.isoformat(), status, WorkflowStatus.CANCELLED,
                     now.isoformat(), run["id"]),
                )
                if not running:
                    connection.execute(
                        "UPDATE workflow_steps SET status=? WHERE workflow_run_id=? AND status IN (?, ?)",
                        (StepStatus.CANCELLED, run["id"], StepStatus.QUEUED, StepStatus.RETRY_SCHEDULED),
                    )
                self._event(connection, run["id"], incident_id,
                            "run.cancel_requested" if running else "run.cancelled", run["current_step_key"], {})
                connection.commit()
                return incident.model_copy(deep=True)
            except Exception:
                connection.rollback()
                raise

    def retry(self, incident_id: str, max_manual_retries: int = 3) -> Incident:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = connection.execute("SELECT * FROM workflow_runs WHERE incident_id=?", (incident_id,)).fetchone()
                if run is None:
                    raise KeyError(incident_id)
                if run["status"] != WorkflowStatus.FAILED:
                    raise InvalidTransitionError("Only failed workflows can be retried")
                step = connection.execute(
                    "SELECT * FROM workflow_steps WHERE workflow_run_id=? AND status=? ORDER BY step_order DESC LIMIT 1",
                    (run["id"], StepStatus.FAILED),
                ).fetchone()
                if step is None or step["attempt_count"] >= step["max_attempts"] + max_manual_retries:
                    raise InvalidTransitionError("Manual retry limit reached")
                connection.execute(
                    """UPDATE workflow_steps SET status=?, available_at=?, error_type=NULL, error_code=NULL,
                    error_message=NULL, version=version+1 WHERE id=?""",
                    (StepStatus.QUEUED, now.isoformat(), step["id"]),
                )
                incident = self._load_incident(connection, incident_id)
                incident.status = IncidentStatus.QUEUED
                incident.current_step_key = step["step_key"]
                incident.updated_at = now
                self._save_incident(connection, incident)
                connection.execute(
                    """UPDATE workflow_runs SET status=?, current_step_key=?, failure_code=NULL,
                    failure_message=NULL, finished_at=NULL, updated_at=?, version=version+1 WHERE id=?""",
                    (WorkflowStatus.QUEUED, step["step_key"], now.isoformat(), run["id"]),
                )
                self._event(connection, run["id"], incident_id, "run.retry_requested", step["step_key"], {})
                connection.commit()
                return incident.model_copy(deep=True)
            except Exception:
                connection.rollback()
                raise

    def reserve_action(self, run_id: str, action_id: str, idempotency_key: str) -> str | None:
        now = utc_now().isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status, result FROM action_executions WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                connection.commit()
                if existing["status"] == "completed":
                    return existing["result"]
                return f"Recovered reserved action {idempotency_key}; duplicate execution suppressed."
            connection.execute(
                "INSERT INTO action_executions VALUES (?, ?, ?, 'running', NULL, ?, NULL)",
                (idempotency_key, run_id, action_id, now),
            )
            connection.commit()
            return None

    def complete_action(self, idempotency_key: str, result: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE action_executions SET status='completed', result=?, completed_at=? WHERE idempotency_key=?",
                (result, utc_now().isoformat(), idempotency_key),
            )

    def approved_by(self, run_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT decided_by FROM approval_records WHERE workflow_run_id=? AND decision='approved'",
                (run_id,),
            ).fetchone()
        if row is None:
            raise InvalidTransitionError("No durable approval record exists")
        return row["decided_by"]

    def get_run_for_incident(self, incident_id: str) -> WorkflowRun:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM workflow_runs WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise KeyError(incident_id)
        return self._run_from_row(row)

    def list_steps(self, run_id: str) -> list[WorkflowStep]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_steps WHERE workflow_run_id=? ORDER BY step_order", (run_id,)
            ).fetchall()
        return [self._step_from_row(row) for row in rows]

    def list_events(self, run_id: str, after_id: int = 0) -> list[WorkflowEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_events WHERE workflow_run_id=? AND id>? ORDER BY id", (run_id, after_id)
            ).fetchall()
        return [WorkflowEvent(
            id=row["id"], workflow_run_id=row["workflow_run_id"], incident_id=row["incident_id"],
            event_type=row["event_type"], step_key=row["step_key"], sequence_number=row["sequence_number"],
            payload=json.loads(row["payload_json"]), created_at=dt(row["created_at"])
        ) for row in rows]

    def recover_expired(self) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = self._recover_expired(connection, utc_now())
            connection.commit()
            return count

    def _recover_expired(self, connection: sqlite3.Connection, now: datetime) -> int:
        rows = connection.execute(
            """SELECT s.*, r.incident_id, r.status AS run_status FROM workflow_steps s
            JOIN workflow_runs r ON r.id=s.workflow_run_id
            WHERE s.status=? AND s.lease_expires_at<?""", (StepStatus.RUNNING, now.isoformat())
        ).fetchall()
        for row in rows:
            cancelled = row["run_status"] == WorkflowStatus.CANCEL_REQUESTED
            connection.execute(
                """UPDATE workflow_steps SET status=?, available_at=?, lease_owner=NULL, lease_expires_at=NULL,
                heartbeat_at=NULL, finished_at=CASE WHEN ? THEN ? ELSE finished_at END,
                version=version+1 WHERE id=?""",
                (StepStatus.CANCELLED if cancelled else StepStatus.QUEUED, now.isoformat(), cancelled,
                 now.isoformat(), row["id"]),
            )
            connection.execute(
                """UPDATE workflow_step_attempts SET status=?, finished_at=?, error_message=?
                WHERE workflow_step_id=? AND attempt_number=?""",
                ("cancelled" if cancelled else "abandoned", now.isoformat(), "worker lease expired",
                 row["id"], row["attempt_count"]),
            )
            connection.execute(
                """UPDATE workflow_runs SET status=?, current_step_key=CASE WHEN ? THEN NULL ELSE current_step_key END,
                updated_at=?, finished_at=CASE WHEN ? THEN ? ELSE finished_at END, version=version+1 WHERE id=?""",
                (WorkflowStatus.CANCELLED if cancelled else WorkflowStatus.QUEUED, cancelled,
                 now.isoformat(), cancelled, now.isoformat(), row["workflow_run_id"]),
            )
            incident = self._load_incident(connection, row["incident_id"])
            incident.status = IncidentStatus.CANCELLED if cancelled else IncidentStatus.QUEUED
            if cancelled:
                incident.current_step_key = None
            incident.updated_at = now
            self._save_incident(connection, incident)
            self._event(
                connection,
                row["workflow_run_id"],
                row["incident_id"],
                "run.cancelled" if cancelled else "step.lease_expired",
                row["step_key"],
                {"previous_owner": row["lease_owner"]},
            )
        return len(rows)

    def _insert_step(self, connection: sqlite3.Connection, run_id: str, index: int, now: datetime) -> None:
        step_key, agent = STEP_DEFINITIONS[index]
        connection.execute(
            """INSERT OR IGNORE INTO workflow_steps
            (id, workflow_run_id, step_key, step_order, agent_name, status, available_at,
             idempotency_key, input_json, output_json, policy_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?)""",
            (f"wstep-{uuid4().hex[:12]}", run_id, step_key, index, agent, StepStatus.QUEUED,
             now.isoformat(), f"{run_id}:{step_key}", json.dumps({"max_attempts": 3})),
        )

    def _event(self, connection: sqlite3.Connection, run_id: str, incident_id: str,
               event_type: str, step_key: str | None, payload: dict) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM workflow_events WHERE workflow_run_id=?",
            (run_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO workflow_events
            (workflow_run_id, incident_id, event_type, step_key, sequence_number, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, incident_id, event_type, step_key, sequence, json.dumps(payload, default=str),
             utc_now().isoformat()),
        )

    @staticmethod
    def _save_incident(connection: sqlite3.Connection, incident: Incident) -> None:
        connection.execute(
            """INSERT INTO incidents (id, created_at, updated_at, payload) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload""",
            (incident.id, incident.created_at.isoformat(), incident.updated_at.isoformat(), incident.model_dump_json()),
        )

    @staticmethod
    def _load_incident(connection: sqlite3.Connection, incident_id: str) -> Incident:
        row = connection.execute("SELECT payload FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if row is None:
            raise KeyError(incident_id)
        return Incident.model_validate_json(row["payload"])

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> WorkflowRun:
        return WorkflowRun(
            id=row["id"], incident_id=row["incident_id"], workflow_name=row["workflow_name"],
            workflow_version=row["workflow_version"], schema_version=row["schema_version"],
            engine_version=row["engine_version"], status=row["status"], current_step_key=row["current_step_key"],
            created_at=dt(row["created_at"]), started_at=dt(row["started_at"]),
            finished_at=dt(row["finished_at"]), updated_at=dt(row["updated_at"]),
            cancel_requested_at=dt(row["cancel_requested_at"]), failure_code=row["failure_code"],
            failure_message=row["failure_message"], version=row["version"],
        )

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> WorkflowStep:
        return WorkflowStep(
            id=row["id"], workflow_run_id=row["workflow_run_id"], step_key=row["step_key"],
            step_order=row["step_order"], agent_name=row["agent_name"], status=row["status"],
            input_json=json.loads(row["input_json"]), output_json=json.loads(row["output_json"]),
            policy_json=json.loads(row["policy_json"]), attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"], available_at=dt(row["available_at"]),
            started_at=dt(row["started_at"]), finished_at=dt(row["finished_at"]),
            next_retry_at=dt(row["next_retry_at"]), timeout_at=dt(row["timeout_at"]),
            lease_owner=row["lease_owner"], lease_expires_at=dt(row["lease_expires_at"]),
            heartbeat_at=dt(row["heartbeat_at"]), idempotency_key=row["idempotency_key"],
            error_type=row["error_type"], error_code=row["error_code"], error_message=row["error_message"],
            version=row["version"],
        )
