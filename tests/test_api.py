from fastapi.testclient import TestClient

from opspilot.api import create_app
from opspilot.store import InMemoryStore


def make_client() -> TestClient:
    return TestClient(create_app(store=InMemoryStore()))


def test_health_and_scenarios() -> None:
    client = make_client()
    assert client.get("/health").json()["status"] == "ok"
    scenarios = client.get("/api/scenarios")
    assert scenarios.status_code == 200
    assert len(scenarios.json()) == 12


def test_full_incident_api_flow() -> None:
    client = make_client()
    created = client.post("/api/incidents", json={"scenario_id": "payment-pool-exhaustion"})
    assert created.status_code == 201
    incident = created.json()
    assert incident["status"] == "awaiting_approval"

    approved = client.post(
        f"/api/incidents/{incident['id']}/approve",
        json={"action_id": incident["actions"][0]["id"], "approved_by": "api-tester"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "resolved"
    assert client.get("/api/dashboard").json()["incidents_resolved"] == 1
    postmortem = client.get(f"/api/incidents/{incident['id']}/postmortem")
    assert postmortem.status_code == 200
    assert "Root cause" in postmortem.text


def test_invalid_scenario_returns_404() -> None:
    response = make_client().post("/api/incidents", json={"scenario_id": "does-not-exist"})
    assert response.status_code == 404


def test_evaluation_endpoint() -> None:
    response = make_client().post("/api/evaluations/run")
    assert response.status_code == 200
    assert response.json()["root_cause_accuracy"] == 1.0


def test_incident_sse_replays_ordered_agent_events() -> None:
    client = make_client()
    incident = client.post("/api/incidents", json={"scenario_id": "auth-clock-skew"}).json()
    response = client.get(f"/api/incidents/{incident['id']}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: agent.step" in response.text
    assert "event: incident.state" in response.text
