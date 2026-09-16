from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from incident_lab.runtime import PollingNotifier, RedisNotifier, build_notifier, build_runtime_stores


def test_sqlite_runtime_remains_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("INCIDENT_LAB_DATABASE_URL", raising=False)
    monkeypatch.setenv("INCIDENT_LAB_DB_PATH", str(tmp_path / "runtime.db"))
    runtime = build_runtime_stores()
    assert runtime.backend == "sqlite"
    assert runtime.workflow.path == tmp_path / "runtime.db"


def test_invalid_database_url_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("INCIDENT_LAB_DATABASE_URL", "sqlite:///wrong.db")
    with pytest.raises(RuntimeError, match="must use PostgreSQL"):
        build_runtime_stores()


def test_polling_notifier_is_default(monkeypatch) -> None:
    monkeypatch.delenv("INCIDENT_LAB_REDIS_URL", raising=False)
    assert isinstance(build_notifier(), PollingNotifier)


def test_redis_notifier_bounds_wakeup_backlog(monkeypatch) -> None:
    calls = []

    class FakeRedis:
        @classmethod
        def from_url(cls, url, **kwargs):
            calls.append(("connect", url, kwargs))
            return cls()
        def lpush(self, key, value):
            calls.append(("lpush", key, value))
            return self
        def ltrim(self, key, start, end):
            calls.append(("ltrim", key, start, end))
            return self
        def pipeline(self, transaction):
            calls.append(("pipeline", transaction))
            return self
        def execute(self):
            calls.append(("execute",))
        def brpop(self, key, timeout):
            calls.append(("brpop", key, timeout))

    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=FakeRedis))
    notifier = RedisNotifier("redis://example/0")
    notifier.notify()
    notifier.wait(2.2)
    assert ("pipeline", True) in calls
    assert ("execute",) in calls
    assert ("ltrim", "incident-lab:wakeup", 0, 99) in calls
    assert ("brpop", "incident-lab:wakeup", 2) in calls
