from incident_lab.engine import IncidentEngine
from incident_lab.store import SQLiteStore
import pytest


def test_sqlite_store_survives_reopen(tmp_path) -> None:
    path = tmp_path / "incident-lab.db"
    first = IncidentEngine(store=SQLiteStore(path))
    incident = first.investigate("order-deadlock")

    second = SQLiteStore(path)
    recovered = second.get_incident(incident.id)

    assert recovered.root_cause == "database deadlock caused by inconsistent lock ordering"
    assert recovered.status == "awaiting_approval"


def test_sqlite_persists_evaluation_report(tmp_path) -> None:
    path = tmp_path / "incident-lab.db"
    report = IncidentEngine(store=SQLiteStore(path)).evaluate()
    recovered = SQLiteStore(path).latest_evaluation()
    assert recovered is not None
    assert recovered.run_id == report.run_id
    assert len(recovered.cases) == 12


def test_sqlite_clear_and_missing_record(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "clear.db")
    incident = IncidentEngine(store=store).investigate("gateway-tls-expiry")
    assert len(store.list_incidents()) == 1
    store.clear()
    assert store.list_incidents() == []
    assert store.latest_evaluation() is None
    with pytest.raises(KeyError):
        store.get_incident(incident.id)
