"""SQLAlchemy models.

Every tenant-owned table carries ``tenant_id`` and indexes it first.

Two shapes are specific to this product:

* **A recording session is deletable as a unit.** Cascades run from the session
  down through every action, so "delete this recording" is one operation that
  leaves nothing behind. A consent-based tool where deletion is partial is not
  consent-based.
* **Executions store sanitised step logs.** The log records which variable a
  step used and the shape of the value, never the value — so an audit trail can
  be kept indefinitely without becoming a data liability.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, GUID


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    api_key_hash: Mapped[str] = mapped_column(String(128), index=True)
    #: The consent boundary. Empty allowlist means nothing may be recorded.
    allowed_origins: Mapped[list] = mapped_column(JSON, default=list)
    blocked_origins: Mapped[list] = mapped_column(JSON, default=list)
    extra_sensitive_fields: Mapped[list] = mapped_column(JSON, default=list)
    #: Connectors an api_call action may name. Adding one is an admin act.
    allowed_connectors: Mapped[list] = mapped_column(JSON, default=list)
    #: Hashing salt for captured values. Workspace-wide on purpose: a value has
    #: to hash alike across sessions for "same in every run" to be detectable
    #: at all. Rotating it makes every earlier hash uncorrelatable.
    capture_salt: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(32), default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_email_per_tenant"),)


class RecordingSession(Base):
    """One explicitly started, explicitly stopped recording."""

    __tablename__ = "recording_sessions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(200), index=True)
    user_email: Mapped[str] = mapped_column(String(320), default="")
    device: Mapped[str] = mapped_column(String(200), default="")
    #: recording | completed | deleted
    status: Mapped[str] = mapped_column(String(32), default="recording", index=True)
    #: Which workspace salt this session's hashes were made with, so a
    #: rotation is traceable rather than silently invalidating comparisons.
    salt: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(300), default="")
    action_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    rejection_reasons: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    actions: Mapped[list["RecordedAction"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id", name="uq_session_external_id"),
    )


class RecordedAction(Base):
    __tablename__ = "recorded_actions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("recording_sessions.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    origin: Mapped[str] = mapped_column(String(300), index=True)
    page_title: Mapped[str] = mapped_column(String(500), default="")
    role: Mapped[str] = mapped_column(String(64), default="")
    accessible_name: Mapped[str] = mapped_column(String(500), default="")
    field_name: Mapped[str] = mapped_column(String(200), default="")
    target_path: Mapped[str] = mapped_column(String(500), default="")
    value_class: Mapped[str] = mapped_column(String(32), default="unknown")
    #: Salted hash. Null for sensitive fields, which have no derived value.
    value_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value_shape: Mapped[str] = mapped_column(String(64), default="")

    session: Mapped[RecordingSession] = relationship(back_populates="actions")

    __table_args__ = (Index("ix_action_tenant_session_seq", "tenant_id", "session_id", "sequence"),)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    sessions_considered: Mapped[int] = mapped_column(Integer, default=0)
    actions_considered: Mapped[int] = mapped_column(Integer, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowCandidateRow(Base):
    __tablename__ = "workflow_candidates"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    discovery_run_id: Mapped[uuid.UUID] = mapped_column(
        GUID, ForeignKey("discovery_runs.id"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(600), index=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    median_duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_seconds_saved: Mapped[float] = mapped_column(Float, default=0.0)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    variables: Mapped[list] = mapped_column(JSON, default=list)
    branches: Mapped[list] = mapped_column(JSON, default=list)
    session_ids: Mapped[list] = mapped_column(JSON, default=list)
    #: candidate | accepted | rejected
    status: Mapped[str] = mapped_column(String(32), default="candidate", index=True)
    narrative: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_candidate_tenant_status", "tenant_id", "status"),)


class WorkflowRow(Base):
    """An accepted workflow, compiled into the DSL."""

    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID, ForeignKey("workflow_candidates.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    #: draft | enabled | disabled. Only 'enabled' may run outside a dry run.
    state: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    definition: Mapped[dict] = mapped_column(JSON)
    compilation_notes: Mapped[list] = mapped_column(JSON, default=list)
    risk: Mapped[str] = mapped_column(String(16), default="medium", index=True)
    #: Pre-authorise medium-risk steps. Cannot ever cover high risk.
    autonomous_medium_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("workflows.id"), index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="completed", index=True)
    #: Variable names supplied, never their values.
    supplied_variables: Mapped[list] = mapped_column(JSON, default=list)
    approved_steps: Mapped[list] = mapped_column(JSON, default=list)
    paused_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_approval: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("executions.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, default="")
    approved_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    actor: Mapped[str] = mapped_column(String(200), default="system")
    action: Mapped[str] = mapped_column(String(120), index=True)
    object_type: Mapped[str] = mapped_column(String(64), default="")
    object_id: Mapped[uuid.UUID | None] = mapped_column(GUID, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class AILogEntry(Base):
    __tablename__ = "ai_log"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID, ForeignKey("tenants.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
