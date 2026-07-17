from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Protocol

from .models import EvaluationReport, Incident


class IncidentStore(Protocol):
    def save_incident(self, incident: Incident) -> Incident: ...
    def get_incident(self, incident_id: str) -> Incident: ...
    def list_incidents(self) -> list[Incident]: ...
    def save_evaluation(self, report: EvaluationReport) -> EvaluationReport: ...
    def latest_evaluation(self) -> EvaluationReport | None: ...


class InMemoryStore:
    """Thread-safe local store. The interface is intentionally database-replaceable."""

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._evaluations: list[EvaluationReport] = []
        self._lock = RLock()

    def save_incident(self, incident: Incident) -> Incident:
        with self._lock:
            self._incidents[incident.id] = incident.model_copy(deep=True)
            return incident.model_copy(deep=True)

    def get_incident(self, incident_id: str) -> Incident:
        with self._lock:
            if incident_id not in self._incidents:
                raise KeyError(incident_id)
            return self._incidents[incident_id].model_copy(deep=True)

    def list_incidents(self) -> list[Incident]:
        with self._lock:
            items = sorted(self._incidents.values(), key=lambda item: item.created_at, reverse=True)
            return [item.model_copy(deep=True) for item in items]

    def save_evaluation(self, report: EvaluationReport) -> EvaluationReport:
        with self._lock:
            self._evaluations.append(report.model_copy(deep=True))
            return report.model_copy(deep=True)

    def latest_evaluation(self) -> EvaluationReport | None:
        with self._lock:
            return self._evaluations[-1].model_copy(deep=True) if self._evaluations else None

    def clear(self) -> None:
        with self._lock:
            self._incidents.clear()
            self._evaluations.clear()


class SQLiteStore:
    """Durable JSON checkpoint store backed by the Python standard library."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def save_incident(self, incident: Incident) -> Incident:
        payload = incident.model_dump_json()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO incidents (id, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                """,
                (incident.id, incident.created_at.isoformat(), incident.updated_at.isoformat(), payload),
            )
        return incident.model_copy(deep=True)

    def get_incident(self, incident_id: str) -> Incident:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        if row is None:
            raise KeyError(incident_id)
        return Incident.model_validate_json(row["payload"])

    def list_incidents(self) -> list[Incident]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM incidents ORDER BY created_at DESC"
            ).fetchall()
        return [Incident.model_validate_json(row["payload"]) for row in rows]

    def save_evaluation(self, report: EvaluationReport) -> EvaluationReport:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO evaluations (run_id, created_at, payload) VALUES (?, ?, ?)",
                (report.run_id, report.created_at.isoformat(), report.model_dump_json()),
            )
        return report.model_copy(deep=True)

    def latest_evaluation(self) -> EvaluationReport | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM evaluations ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return EvaluationReport.model_validate_json(row["payload"]) if row else None

    def clear(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM incidents")
            connection.execute("DELETE FROM evaluations")
