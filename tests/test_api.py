from time import perf_counter

from fastapi.testclient import TestClient

from incident_lab import __version__
from incident_lab.api import create_app
from incident_lab.store import SQLiteStore
from incident_lab.workflow import WorkflowWorker
from incident_lab.workflow_store import WorkflowStore


def make_runtime(tmp_path):
    database = tmp_path / "incident-lab.db"
    durable = SQLiteStore(database)
    workflow = WorkflowStore(database, __version__)
    return TestClient(create_app(durable, workflow)), workflow


def test_health_and_scenarios(tmp_path) -> None:
    client, _ = make_runtime(tmp_path)
    assert client.get("/health").json()["status"] == "ok"
    scenarios = client.get("/api/scenarios")
    assert scenarios.status_code == 200
    assert len(scenarios.json()) == 12


def test_create_is_non_blocking_and_full_worker_flow(tmp_path) -> None:
    client, store = make_runtime(tmp_path)
    started = perf_counter()
    created = client.post("/api/incidents", json={"scenario_id": "payment-pool-exhaustion"})
    elapsed = perf_counter() - started
    assert created.status_code == 202
    assert elapsed < 0.5
    incident = created.json()
    assert incident["status"] == "queued"

    worker = WorkflowWorker(store, "api-test")
    assert worker.drain() == 7
    waiting = client.get(f"/api/incidents/{incident['id']}").json()
    assert waiting["status"] == "awaiting_approval"

    approved = client.post(
        f"/api/incidents/{incident['id']}/approve",
        json={"action_id": waiting["actions"][0]["id"], "approved_by": "api-tester"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "resuming"
    assert worker.drain() == 3
    resolved = client.get(f"/api/incidents/{incident['id']}").json()
    assert resolved["status"] == "resolved"
    assert client.get("/api/dashboard").json()["incidents_resolved"] == 1
    assert "Root cause" in client.get(f"/api/incidents/{incident['id']}/postmortem").text


def test_invalid_scenario_returns_404(tmp_path) -> None:
    client, _ = make_runtime(tmp_path)
    assert client.post("/api/incidents", json={"scenario_id": "does-not-exist"}).status_code == 404


def test_evaluation_endpoint(tmp_path) -> None:
    client, _ = make_runtime(tmp_path)
    response = client.post("/api/evaluations/run")
    assert response.status_code == 200
    assert response.json()["root_cause_accuracy"] == 1.0


def test_sse_replays_after_last_event_id(tmp_path) -> None:
    client, store = make_runtime(tmp_path)
    incident = client.post("/api/incidents", json={"scenario_id": "auth-clock-skew"}).json()
    WorkflowWorker(store, "sse-test").run_once()
    all_events = client.get(f"/api/incidents/{incident['id']}/events?follow=false")
    assert all_events.status_code == 200
    ids = [int(line.removeprefix("id: ")) for line in all_events.text.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids)
    replay = client.get(
        f"/api/incidents/{incident['id']}/events?follow=false",
        headers={"Last-Event-ID": str(ids[0])},
    )
    replay_ids = [int(line.removeprefix("id: ")) for line in replay.text.splitlines() if line.startswith("id: ")]
    assert replay_ids == ids[1:]
    query_replay = client.get(
        f"/api/incidents/{incident['id']}/events?follow=false&after={ids[0]}"
    )
    query_ids = [int(line.removeprefix("id: ")) for line in query_replay.text.splitlines() if line.startswith("id: ")]
    assert query_ids == ids[1:]


def test_reject_endpoint_records_reason_and_cancels(tmp_path) -> None:
    client, store = make_runtime(tmp_path)
    incident = client.post("/api/incidents", json={"scenario_id": "gateway-tls-expiry"}).json()
    WorkflowWorker(store, "reject-api-worker").drain()
    waiting = client.get(f"/api/incidents/{incident['id']}").json()
    rejected = client.post(
        f"/api/incidents/{incident['id']}/reject",
        json={
            "action_id": waiting["actions"][0]["id"],
            "rejected_by": "api-operator",
            "reason": "Change window is closed",
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "cancelled"
    assert rejected.json()["approval_rejection_reason"] == "Change window is closed"
    assert not rejected.json()["actions"][0]["executed"]


def test_cancel_and_retry_endpoints(tmp_path) -> None:
    client, _ = make_runtime(tmp_path)
    incident = client.post("/api/incidents", json={"scenario_id": "auth-clock-skew"}).json()
    cancelled = client.post(f"/api/incidents/{incident['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/incidents/{incident['id']}/retry").status_code == 409
