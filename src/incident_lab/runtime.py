from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import __version__
from .store import IncidentStore, SQLiteStore
from .workflow_store import WorkflowStore


class WorkNotifier(Protocol):
    name: str
    def notify(self) -> None: ...
    def wait(self, timeout_seconds: float) -> None: ...


class PollingNotifier:
    name = "polling"
    def notify(self) -> None:
        return None
    def wait(self, timeout_seconds: float) -> None:
        time.sleep(timeout_seconds)


class RedisNotifier:
    name = "redis"
    def __init__(self, url: str, queue_name: str = "incident-lab:wakeup") -> None:
        try:
            from redis import Redis
        except ImportError as exc:  # pragma: no cover - production dependency guard
            raise RuntimeError('Redis mode requires pip install ".[production]"') from exc
        self.client = Redis.from_url(url, decode_responses=True, socket_timeout=5)
        self.queue_name = queue_name

    def notify(self) -> None:
        pipeline = self.client.pipeline(transaction=True)
        pipeline.lpush(self.queue_name, "work")
        pipeline.ltrim(self.queue_name, 0, 99)
        pipeline.execute()

    def wait(self, timeout_seconds: float) -> None:
        self.client.brpop(self.queue_name, timeout=max(1, int(timeout_seconds)))


@dataclass(frozen=True)
class RuntimeStores:
    incidents: IncidentStore
    workflow: WorkflowStore
    backend: str


def build_runtime_stores() -> RuntimeStores:
    database_url = os.getenv("INCIDENT_LAB_DATABASE_URL", "").strip()
    if database_url:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise RuntimeError("INCIDENT_LAB_DATABASE_URL must use PostgreSQL")
        from .postgres import PostgresIncidentStore, PostgresWorkflowStore
        return RuntimeStores(
            PostgresIncidentStore(database_url),
            PostgresWorkflowStore(database_url, __version__),
            "postgresql",
        )
    path = Path(os.getenv("INCIDENT_LAB_DB_PATH", "data/incident-lab.db"))
    incidents = SQLiteStore(path)
    return RuntimeStores(incidents, WorkflowStore(path, __version__), "sqlite")


def build_notifier() -> WorkNotifier:
    redis_url = os.getenv("INCIDENT_LAB_REDIS_URL", "").strip()
    return RedisNotifier(redis_url) if redis_url else PollingNotifier()
