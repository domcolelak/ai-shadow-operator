"""Pydantic request/response models."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- consent and sessions --------------------------------------------------


class ConsentPolicyOut(BaseModel):
    """What this workspace has consented to record."""

    allowed_origins: list[str]
    blocked_origins: list[str]
    extra_sensitive_fields: list[str]
    allowed_connectors: list[str]
    screenshots_captured: bool = False
    keystrokes_captured: bool = False
    values_stored: bool = False
    note: str


class ConsentPolicyUpdate(BaseModel):
    allowed_origins: list[str] | None = None
    blocked_origins: list[str] | None = None
    extra_sensitive_fields: list[str] | None = None
    allowed_connectors: list[str] | None = None


class SessionStart(BaseModel):
    external_id: str = Field(min_length=1, max_length=200)
    user_email: str = ""
    device: str = ""
    label: str = ""


class SessionOut(ORMModel):
    id: uuid.UUID
    external_id: str
    user_email: str
    device: str
    status: str
    label: str
    action_count: int
    rejected_count: int
    rejection_reasons: list[Any] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime | None


class EventBatch(BaseModel):
    events: list[dict[str, Any]] = Field(min_length=1, max_length=5_000)


class EventBatchResponse(BaseModel):
    session_id: uuid.UUID
    accepted: int
    rejected: int
    rejection_reasons: list[str] = Field(default_factory=list)
    note: str


class SessionSummary(BaseModel):
    session_id: uuid.UUID
    action_count: int
    by_action: dict[str, int]
    origins: list[str]
    sensitive_steps_recorded_without_values: int
    values_stored: bool
    screenshots_stored: bool


# --- discovery -------------------------------------------------------------


class DiscoveryRequest(BaseModel):
    min_repetitions: int = Field(default=3, ge=2, le=100)
    session_gap_minutes: float = Field(default=12.0, gt=0, le=600)
    max_distance: float = Field(default=0.34, gt=0, le=1.0)


class DiscoveryRunOut(ORMModel):
    id: uuid.UUID
    status: str
    config: dict[str, Any]
    sessions_considered: int
    actions_considered: int
    candidate_count: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


class CandidateOut(ORMModel):
    id: uuid.UUID
    fingerprint: str
    name: str
    observation_count: int
    confidence: float
    median_duration_seconds: float
    estimated_seconds_saved: float
    steps: list[Any]
    variables: list[Any]
    branches: list[Any]
    session_ids: list[Any]
    status: str
    narrative: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AcceptCandidate(BaseModel):
    name: str | None = None
    description: str = ""


# --- workflows -------------------------------------------------------------


class WorkflowOut(ORMModel):
    id: uuid.UUID
    candidate_id: uuid.UUID | None
    name: str
    description: str
    state: str
    definition: dict[str, Any]
    compilation_notes: list[Any] = Field(default_factory=list)
    risk: str
    autonomous_medium_risk: bool
    created_at: datetime
    updated_at: datetime


class WorkflowDetail(WorkflowOut):
    validation_issues: list[dict[str, Any]] = Field(default_factory=list)
    requires_approval: bool = True
    high_risk_steps: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowStateUpdate(BaseModel):
    state: Literal["draft", "enabled", "disabled"]
    #: Pre-authorise medium-risk steps. It can never cover high risk.
    autonomous_medium_risk: bool = False


class WorkflowEdit(BaseModel):
    definition: dict[str, Any]


# --- execution -------------------------------------------------------------


class RunRequest(BaseModel):
    #: Dry by default: a caller has to ask for a live run explicitly.
    dry_run: bool = True
    variables: dict[str, str] = Field(default_factory=dict)
    started_by: str = ""


class ResumeRequest(BaseModel):
    #: The step being approved, and the decision.
    step_index: int = Field(ge=0)
    decision: Literal["approve", "reject"]
    reason: str = ""
    approved_by: str = ""
    variables: dict[str, str] = Field(default_factory=dict)


class ExecutionOut(ORMModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    dry_run: bool
    status: str
    supplied_variables: list[Any] = Field(default_factory=list)
    approved_steps: list[Any] = Field(default_factory=list)
    paused_at: int | None
    pending_approval: str | None
    steps: list[Any] = Field(default_factory=list)
    error: str | None
    started_by: str
    created_at: datetime


class OverviewResponse(BaseModel):
    session_count: int
    action_count: int
    rejected_count: int
    candidate_count: int
    workflow_count: int
    enabled_workflows: int
    executions: int
    live_executions: int
    awaiting_approval: int
    estimated_seconds_saved: float
    consent: ConsentPolicyOut
    top_candidates: list[CandidateOut] = Field(default_factory=list)
