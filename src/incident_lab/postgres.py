from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .models import EvaluationReport, Incident, IncidentStatus, StepStatus, WorkflowStatus
from .workflow_store import (
    STEP_DEFINITIONS,
    WorkflowClaim,
    WorkflowStore,
    utc_now,
)


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - exercised by production configuration
        raise RuntimeError('PostgreSQL mode requires pip install ".[production]"') from exc
    return psycopg, dict_row


def _sql(query: str) -> str:
    return query.replace("BEGIN IMMEDIATE", "BEGIN").replace("?", "%s")


class PostgresConnection(AbstractContextManager):
    """Small DB-API compatibility layer used by the proven workflow store logic."""

    def __init__(self, dsn: str) -> None:
        psycopg, dict_row = _psycopg()
        self.raw = psycopg.connect(dsn, row_factory=dict_row)

    def __enter__(self):
        self.raw.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self.raw.__exit__(exc_type, exc, tb)

    def execute(self, query: str, params: tuple[Any, ...] | None = None):
        return self.raw.execute(_sql(query), params or ())

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY, incident_id TEXT NOT NULL UNIQUE REFERENCES incidents(id),
    workflow_name TEXT NOT NULL, workflow_version TEXT NOT NULL, schema_version INTEGER NOT NULL,
    engine_version TEXT NOT NULL, status TEXT NOT NULL, current_step_key TEXT,
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL,
    cancel_requested_at TEXT, failure_code TEXT, failure_message TEXT, version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS workflow_steps (
    id TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    step_key TEXT NOT NULL, step_order INTEGER NOT NULL, agent_name TEXT NOT NULL, status TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}', output_json TEXT NOT NULL DEFAULT '{}',
    policy_json TEXT NOT NULL DEFAULT '{}', attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3, available_at TEXT NOT NULL, started_at TEXT,
    finished_at TEXT, next_retry_at TEXT, timeout_at TEXT, lease_owner TEXT, lease_expires_at TEXT,
    heartbeat_at TEXT, idempotency_key TEXT NOT NULL UNIQUE, error_type TEXT, error_code TEXT,
    error_message TEXT, version INTEGER NOT NULL DEFAULT 0, UNIQUE(workflow_run_id, step_key)
);
CREATE TABLE IF NOT EXISTS workflow_step_attempts (
    id BIGSERIAL PRIMARY KEY, workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(id),
    attempt_number INTEGER NOT NULL, worker_id TEXT NOT NULL, status TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, error_message TEXT,
    UNIQUE(workflow_step_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS workflow_events (
    id BIGSERIAL PRIMARY KEY, workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    incident_id TEXT NOT NULL REFERENCES incidents(id), event_type TEXT NOT NULL, step_key TEXT,
    sequence_number INTEGER NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
    UNIQUE(workflow_run_id, sequence_number)
);
CREATE TABLE IF NOT EXISTS approval_records (
    workflow_run_id TEXT PRIMARY KEY REFERENCES workflow_runs(id), action_id TEXT NOT NULL,
    decision TEXT NOT NULL, decided_by TEXT NOT NULL, decided_at TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS action_executions (
    idempotency_key TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    action_id TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL, completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_steps_runnable ON workflow_steps(status, available_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON workflow_events(workflow_run_id, id);
"""


class PostgresIncidentStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._initialize()

    def _connect(self) -> PostgresConnection:
        return PostgresConnection(self.dsn)

    def _initialize(self) -> None:
        psycopg, _ = _psycopg()
        with psycopg.connect(self.dsn) as connection:
            connection.execute(POSTGRES_SCHEMA)

    def save_incident(self, incident: Incident) -> Incident:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO incidents (id, created_at, updated_at, payload) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload""",
                (incident.id, incident.created_at.isoformat(), incident.updated_at.isoformat(), incident.model_dump_json()),
            )
        return incident.model_copy(deep=True)

    def get_incident(self, incident_id: str) -> Incident:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if row is None:
            raise KeyError(incident_id)
        return Incident.model_validate_json(row["payload"])

    def list_incidents(self) -> list[Incident]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload FROM incidents ORDER BY created_at DESC").fetchall()
        return [Incident.model_validate_json(row["payload"]) for row in rows]

    def save_evaluation(self, report: EvaluationReport) -> EvaluationReport:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO evaluations (run_id, created_at, payload) VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET created_at=excluded.created_at, payload=excluded.payload""",
                (report.run_id, report.created_at.isoformat(), report.model_dump_json()),
            )
        return report.model_copy(deep=True)

    def latest_evaluation(self) -> EvaluationReport | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM evaluations ORDER BY created_at DESC LIMIT 1").fetchone()
        return EvaluationReport.model_validate_json(row["payload"]) if row else None

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute("TRUNCATE evaluations, incidents CASCADE")


class PostgresWorkflowStore(WorkflowStore):
    """PostgreSQL workflow store with row-level, skip-locked worker claims."""

    def __init__(self, dsn: str, engine_version: str) -> None:
        self.dsn = dsn
        self.engine_version = engine_version
        self._initialize()

    def connect(self) -> PostgresConnection:
        return PostgresConnection(self.dsn)

    def _initialize(self) -> None:
        psycopg, _ = _psycopg()
        with psycopg.connect(self.dsn) as connection:
            connection.execute(POSTGRES_SCHEMA)

    def claim_next(self, worker_id: str, lease_seconds: float = 30, timeout_seconds: float = 20) -> WorkflowClaim | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN")
            try:
                self._recover_expired(connection, now)
                row = connection.execute(
                    """SELECT s.*, r.incident_id FROM workflow_steps s
                    JOIN workflow_runs r ON r.id=s.workflow_run_id
                    WHERE s.status IN (?, ?) AND s.available_at <= ?
                      AND r.status NOT IN (?, ?, ?)
                    ORDER BY s.available_at, s.step_order LIMIT 1 FOR UPDATE OF s SKIP LOCKED""",
                    (StepStatus.QUEUED, StepStatus.RETRY_SCHEDULED, now.isoformat(),
                     WorkflowStatus.RESOLVED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                lease_expires = now + timedelta(seconds=lease_seconds)
                timeout_at = now + timedelta(seconds=timeout_seconds)
                connection.execute(
                    """UPDATE workflow_steps SET status=?, lease_owner=?, lease_expires_at=?, heartbeat_at=?,
                    timeout_at=?, started_at=COALESCE(started_at, ?), attempt_count=attempt_count+1,
                    version=version+1 WHERE id=?""",
                    (StepStatus.RUNNING, worker_id, lease_expires.isoformat(), now.isoformat(),
                     timeout_at.isoformat(), now.isoformat(), row["id"]),
                )
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
                connection.commit()
                return WorkflowClaim(run, self._step_from_row(step_row), incident, step_row["version"])
            except Exception:
                connection.rollback()
                raise

    def _recover_expired(self, connection, now: datetime) -> int:
        rows = connection.execute(
            """SELECT s.*, r.incident_id, r.status AS run_status FROM workflow_steps s
            JOIN workflow_runs r ON r.id=s.workflow_run_id
            WHERE s.status=? AND s.lease_expires_at<?
            FOR UPDATE OF s SKIP LOCKED""",
            (StepStatus.RUNNING, now.isoformat()),
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

    def _event(self, connection, run_id: str, incident_id: str,
               event_type: str, step_key: str | None, payload: dict) -> None:
        # MAX(sequence)+1 needs per-run serialization when an approval and a
        # worker transition arrive together. The lock lasts only for this DB tx.
        connection.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (run_id,))
        row = connection.execute(
            """SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_sequence
            FROM workflow_events WHERE workflow_run_id=?""",
            (run_id,),
        ).fetchone()
        connection.execute(
            """INSERT INTO workflow_events
            (workflow_run_id, incident_id, event_type, step_key, sequence_number, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, incident_id, event_type, step_key, row["next_sequence"],
             json.dumps(payload, default=str), utc_now().isoformat()),
        )

    def _insert_step(self, connection, run_id: str, index: int, now: datetime) -> None:
        step_key, agent = STEP_DEFINITIONS[index]
        connection.execute(
            """INSERT INTO workflow_steps
            (id, workflow_run_id, step_key, step_order, agent_name, status, available_at,
             idempotency_key, input_json, output_json, policy_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?)
            ON CONFLICT(workflow_run_id, step_key) DO NOTHING""",
            (f"wstep-{uuid4().hex[:12]}", run_id, step_key, index, agent, StepStatus.QUEUED,
             now.isoformat(), f"{run_id}:{step_key}", json.dumps({"max_attempts": 3})),
        )
