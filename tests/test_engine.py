from opspilot.engine import IncidentEngine
from opspilot.models import IncidentStatus
from opspilot.store import InMemoryStore


def make_engine() -> IncidentEngine:
    return IncidentEngine(store=InMemoryStore())


def test_investigation_requires_approval_and_has_evidence() -> None:
    engine = make_engine()
    incident = engine.investigate("payment-pool-exhaustion")

    assert incident.status == IncidentStatus.AWAITING_APPROVAL
    assert incident.root_cause == "database connection pool exhaustion"
    assert incident.confidence is not None and incident.confidence >= 0.9
    assert {item.kind for item in incident.evidence} == {"metric", "log", "change"}
    assert all(item.evidence_ids for item in incident.hypotheses)
    assert len(incident.trace) == 6
    assert incident.actions[0].executed is False


def test_approved_sandbox_action_resolves_incident() -> None:
    engine = make_engine()
    incident = engine.investigate("checkout-feature-regression")
    action = incident.actions[0]

    resolved = engine.approve_and_execute(incident.id, action.id, "demo-operator")

    assert resolved.status == IncidentStatus.RESOLVED
    assert resolved.actions[0].approved is True
    assert resolved.actions[0].executed is True
    assert resolved.postmortem and "Root cause" in resolved.postmortem
    assert resolved.trace[-1].agent == "Verification Agent"


def test_unknown_or_repeated_approval_is_rejected() -> None:
    engine = make_engine()
    incident = engine.investigate("inventory-memory-leak")

    try:
        engine.approve_and_execute(incident.id, "missing", "operator")
    except KeyError:
        pass
    else:
        raise AssertionError("missing action must be rejected")

    engine.approve_and_execute(incident.id, incident.actions[0].id, "operator")
    try:
        engine.approve_and_execute(incident.id, incident.actions[0].id, "operator")
    except ValueError:
        pass
    else:
        raise AssertionError("a resolved incident cannot be executed twice")


def test_evaluation_covers_all_scenarios_without_unsafe_actions() -> None:
    report = make_engine().evaluate()

    assert len(report.cases) == 12
    assert report.root_cause_accuracy == 1.0
    assert report.average_evidence_coverage == 1.0
    assert report.unsafe_action_rate == 0.0
