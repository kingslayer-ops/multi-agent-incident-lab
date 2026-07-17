from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IncidentStatus(StrEnum):
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    FAILED = "failed"


class StepStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


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


class CreateIncidentRequest(BaseModel):
    scenario_id: str


class ApprovalRequest(BaseModel):
    action_id: str
    approved_by: str = Field(min_length=2, max_length=80)


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
