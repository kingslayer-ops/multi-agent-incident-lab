from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from incident_lab import __version__
from incident_lab.models import Incident, IncidentStatus
from incident_lab.postgres import PostgresIncidentStore, PostgresWorkflowStore
from incident_lab.scenarios import get_scenario
from incident_lab.workflow import WorkflowStepExecutor, WorkflowWorker


pytestmark = pytest.mark.skipif(
    not os.getenv("INCIDENT_LAB_TEST_POSTGRES_URL"),
    reason="PostgreSQL integration URL is not configured",
)


def test_postgres_multi_worker_claim_and_reopen() -> None:
    dsn = os.environ["INCIDENT_LAB_TEST_POSTGRES_URL"]
    incidents = PostgresIncidentStore(dsn)
    incidents.clear()
    store = PostgresWorkflowStore(dsn, __version__)
    summary = get_scenario("order-deadlock")["summary"]
    incident = store.create_run(Incident(
        scenario_id=summary.id,
        title=summary.title,
        service=summary.service,
        severity=summary.severity,
        symptom=summary.symptom,
        status=IncidentStatus.QUEUED,
    ))
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(store.claim_next, ["worker-a", "worker-b", "worker-c", "worker-d"]))
    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1

    executor = WorkflowStepExecutor(store)
    checkpoint, output, pause = executor.execute(claimed[0])
    store.complete_step(claimed[0], checkpoint, output, pause=pause)
    worker = WorkflowWorker(store, "worker-e")
    assert worker.drain() == 6
    waiting = incidents.get_incident(incident.id)
    approved = store.approve(incident.id, waiting.actions[0].id, "integration-test")
    assert approved.status == IncidentStatus.RESUMING
    assert worker.drain() == 3
    assert incidents.get_incident(incident.id).status == IncidentStatus.RESOLVED

    reopened = PostgresWorkflowStore(dsn, __version__)
    assert reopened.get_run_for_incident(incident.id).id == claimed[0].run.id
    events = reopened.list_events(claimed[0].run.id)
    assert [event.sequence_number for event in events] == list(range(1, len(events) + 1))
