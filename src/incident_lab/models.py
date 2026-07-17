from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IncidentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    RESUMING = "resuming"
    CANCEL_REQUESTED = "cancel_requested"
    RESOLVED = "resolved"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    WAITING_APPROVAL = "waiting_approval"
    RESUMING = "resuming"
    CANCEL_REQUESTED = "cancel_requested"
    RESOLVED = "resolved"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ScenarioSummary(BaseModel):
    id: str
    title: str
    service: str
    symptom: str
    severity: str
    difficulty: str


class Evidence(BaseModel):
    id: str = Field(default_factory=lambda: f"ev-{uuid4().hex[:10]}")
    kind: str
    source: str
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    collected_at: datetime = Field(default_factory=utc_now)


class Hypothesis(BaseModel):
    cause: str
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    counter_evidence_ids: list[str] = Field(default_factory=list)
    verdict: str


class TraceStep(BaseModel):
    id: str = Field(default_factory=lambda: f"step-{uuid4().hex[:10]}")
    agent: str
    status: StepStatus = StepStatus.COMPLETED
    summary: str
    tool: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    duration_ms: int = Field(ge=0)
    token_estimate: int = Field(ge=0, default=0)
    started_at: datetime = Field(default_factory=utc_now)


class FallbackEvent(BaseModel):
    from_provider: str
    to_provider: str
    reason: str
    occurred_at: datetime = Field(default_factory=utc_now)


class RemediationAction(BaseModel):
    id: str = Field(default_factory=lambda: f"act-{uuid4().hex[:10]}")
    title: str
    description: str
    command_preview: str
    risk: RiskLevel
    requires_approval: bool = True
    approved: bool = False
    executed: bool = False
    execution_result: str | None = None


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: f"inc-{uuid4().hex[:8]}")
    scenario_id: str
    title: str
    service: str
    severity: str
    status: IncidentStatus = IncidentStatus.INVESTIGATING
    symptom: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    root_cause: str | None = None
    confidence: float | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    actions: list[RemediationAction] = Field(default_factory=list)
    postmortem: str | None = None
    total_tokens: int = 0
    estimated_cost_usd: float = 0
    provider: str = "unknown"
    fallback_events: list[FallbackEvent] = Field(default_factory=list)
    workflow_run_id: str | None = None
    current_step_key: str | None = None
    retry_count: int = 0
    approval_rejection_reason: str | None = None


class WorkflowRun(BaseModel):
    id: str
    incident_id: str
    workflow_name: str
    workflow_version: str
    schema_version: int
    engine_version: str
    status: WorkflowStatus
    current_step_key: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime
    cancel_requested_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    version: int = 0


class WorkflowStep(BaseModel):
    id: str
    workflow_run_id: str
    step_key: str
    step_order: int
    agent_name: str
    status: StepStatus
    input_json: dict[str, Any] = Field(default_factory=dict)
    output_json: dict[str, Any] = Field(default_factory=dict)
    policy_json: dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = 0
    max_attempts: int = 3
    available_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_retry_at: datetime | None = None
    timeout_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    idempotency_key: str
    error_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    version: int = 0


class WorkflowEvent(BaseModel):
    id: int
    workflow_run_id: str
    incident_id: str
    event_type: str
    step_key: str | None = None
    sequence_number: int
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CreateIncidentRequest(BaseModel):
    scenario_id: str


class ApprovalRequest(BaseModel):
    action_id: str
    approved_by: str = Field(min_length=2, max_length=80)


class RejectionRequest(BaseModel):
    action_id: str
    rejected_by: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=3, max_length=500)


class EvaluationCase(BaseModel):
    scenario_id: str
    expected_root_cause: str
    predicted_root_cause: str
    correct: bool
    evidence_coverage: float
    unsafe_action_rate: float
    duration_ms: int


class EvaluationReport(BaseModel):
    run_id: str = Field(default_factory=lambda: f"eval-{uuid4().hex[:8]}")
    created_at: datetime = Field(default_factory=utc_now)
    cases: list[EvaluationCase]
    root_cause_accuracy: float
    average_evidence_coverage: float
    unsafe_action_rate: float
    average_duration_ms: float
