"""Service layer: database <-> capture, mining and execution.

Every query is tenant scoped. The capture policy is built from the tenant row,
so a route cannot accidentally record with a wider policy than the tenant
consented to.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.capture.model import (
    ActionType,
    CapturedAction,
    CapturePolicy,
    ElementRef,
    ValueClass,
    capture_batch,
)
from app.mining.discovery import MiningConfig, WorkflowCandidate, discover_workflows
from app.models import (
    DiscoveryRun,
    RecordedAction,
    RecordingSession,
    Tenant,
    WorkflowCandidateRow,
    WorkflowRow,
)


def policy_for(tenant: Tenant) -> CapturePolicy:
    """The tenant's consent boundary, as the capture layer understands it."""
    return CapturePolicy(
        allowed_origins=tuple(tenant.allowed_origins or ()),
        blocked_origins=tuple(tenant.blocked_origins or ()),
        extra_sensitive_fields=tuple(tenant.extra_sensitive_fields or ()),
        capture_screenshots=False,
    )


def new_salt() -> str:
    return secrets.token_hex(16)


def tenant_salt(db: Session, tenant: Tenant) -> str:
    """The workspace's hashing salt, created on first use."""
    if not tenant.capture_salt:
        tenant.capture_salt = new_salt()
        db.flush()
    return tenant.capture_salt


def get_session(
    db: Session, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> RecordingSession | None:
    return db.scalar(
        select(RecordingSession).where(
            RecordingSession.tenant_id == tenant_id, RecordingSession.id == session_id
        )
    )


def record_events(
    db: Session,
    tenant: Tenant,
    session: RecordingSession,
    raw_events: Sequence[dict],
) -> dict:
    """Filter a batch of raw events through the policy and store what survives."""
    if session.status != "recording":
        raise ValueError("this session is not recording")

    # The workspace salt, not a per-session one: a constant is only
    # detectable if the same value hashes alike across sessions, and a
    # per-session salt makes every run look like it used a different value.
    result = capture_batch(
        raw_events,
        policy=policy_for(tenant),
        session_id=str(session.id),
        salt=tenant_salt(db, tenant),
        start_sequence=session.action_count,
    )

    for item in result.actions:
        db.add(
            RecordedAction(
                tenant_id=tenant.id,
                session_id=session.id,
                sequence=item.sequence,
                occurred_at=item.occurred_at,
                action=item.action.value,
                origin=item.origin,
                page_title=item.page_title,
                role=item.element.role,
                accessible_name=item.element.accessible_name,
                field_name=item.element.field_name,
                target_path=item.target_path,
                value_class=item.value_class.value,
                value_hash=item.value_hash,
                value_shape=item.value_shape,
            )
        )

    session.action_count += result.accepted_count
    session.rejected_count += len(result.rejected)
    # Reasons are kept so the user can see what was refused and why; the
    # events themselves are not.
    reasons = list(session.rejection_reasons or [])
    for rejection in result.rejected:
        reason = rejection["reason"]
        if reason not in reasons:
            reasons.append(reason)
    session.rejection_reasons = reasons[:20]
    db.flush()

    return {
        "accepted": result.accepted_count,
        "rejected": len(result.rejected),
        "rejection_reasons": [r["reason"] for r in result.rejected][:20],
    }


def load_actions(
    db: Session, tenant_id: uuid.UUID, *, session_ids: Sequence[uuid.UUID] | None = None
) -> list[CapturedAction]:
    """Rehydrate stored actions into the shape the miner works on."""
    stmt = select(RecordedAction).where(RecordedAction.tenant_id == tenant_id)
    if session_ids:
        stmt = stmt.where(RecordedAction.session_id.in_(session_ids))
    stmt = stmt.order_by(RecordedAction.session_id, RecordedAction.sequence)

    actions: list[CapturedAction] = []
    for row in db.scalars(stmt):
        occurred = row.occurred_at
        actions.append(
            CapturedAction(
                session_id=str(row.session_id),
                sequence=row.sequence,
                occurred_at=occurred if occurred.tzinfo else occurred.replace(tzinfo=timezone.utc),
                action=ActionType(row.action),
                origin=row.origin,
                page_title=row.page_title,
                element=ElementRef(
                    role=row.role,
                    accessible_name=row.accessible_name,
                    field_name=row.field_name,
                ),
                target_path=row.target_path,
                value_class=ValueClass(row.value_class),
                value_hash=row.value_hash,
                value_shape=row.value_shape,
            )
        )
    return actions


def delete_session(db: Session, tenant_id: uuid.UUID, session_id: uuid.UUID) -> int:
    """Delete a recording and everything derived from it.

    One operation, nothing left behind: a consent-based tool where deletion is
    partial is not consent-based. Actions cascade; the session row goes with
    them.
    """
    session = get_session(db, tenant_id, session_id)
    if session is None:
        raise LookupError("session not found for this tenant")

    removed = db.scalar(
        select(RecordedAction).where(
            RecordedAction.tenant_id == tenant_id, RecordedAction.session_id == session_id
        )
    )
    count = len(session.actions)
    db.execute(
        delete(RecordedAction).where(
            RecordedAction.tenant_id == tenant_id, RecordedAction.session_id == session_id
        )
    )
    db.delete(session)
    db.flush()
    return count


def run_discovery(
    db: Session,
    tenant_id: uuid.UUID,
    *,
    config: MiningConfig | None = None,
) -> DiscoveryRun:
    """Mine every recorded session for repeated workflows."""
    cfg = config or MiningConfig()
    run = DiscoveryRun(
        tenant_id=tenant_id,
        status="running",
        config={
            "session_gap_minutes": cfg.session_gap_minutes,
            "min_repetitions": cfg.min_repetitions,
            "max_distance": cfg.max_distance,
            "min_run_length": cfg.min_run_length,
        },
    )
    db.add(run)
    db.flush()

    try:
        actions = load_actions(db, tenant_id)
        candidates = discover_workflows(actions, cfg)

        # Candidates belong to a run; a stale one from an earlier corpus would
        # be misleading.
        db.execute(
            delete(WorkflowCandidateRow).where(
                WorkflowCandidateRow.tenant_id == tenant_id,
                WorkflowCandidateRow.status == "candidate",
            )
        )
        for candidate in candidates:
            _persist_candidate(db, tenant_id, run.id, candidate)

        run.status = "completed"
        run.sessions_considered = len({a.session_id for a in actions})
        run.actions_considered = len(actions)
        run.candidate_count = len(candidates)
    except Exception as exc:  # pragma: no cover - surfaced through the API
        run.status = "failed"
        run.error = str(exc)[:2000]
        raise
    finally:
        run.finished_at = datetime.now(timezone.utc)
        db.flush()

    return run


def _persist_candidate(
    db: Session, tenant_id: uuid.UUID, run_id: uuid.UUID, candidate: WorkflowCandidate
) -> WorkflowCandidateRow:
    payload = candidate.as_dict()
    row = WorkflowCandidateRow(
        tenant_id=tenant_id,
        discovery_run_id=run_id,
        fingerprint=candidate.fingerprint[:600],
        name=_default_name(candidate),
        observation_count=candidate.observation_count,
        confidence=candidate.confidence,
        median_duration_seconds=candidate.median_duration_seconds,
        estimated_seconds_saved=candidate.estimated_seconds_saved_per_month,
        steps=payload["steps"],
        variables=payload["variables"],
        branches=payload["branches"],
        session_ids=candidate.session_ids,
    )
    db.add(row)
    db.flush()
    return row


def _default_name(candidate: WorkflowCandidate) -> str:
    """A deterministic name, so the queue is readable with no AI configured."""
    labelled = [s.label for s in candidate.steps if s.label]
    if not labelled:
        return f"Workflow of {len(candidate.steps)} steps"
    return f"{labelled[0]} → {labelled[-1]} ({len(candidate.steps)} steps)"


def candidate_to_dict(row: WorkflowCandidateRow) -> dict:
    return {
        "observation_count": row.observation_count,
        "confidence": row.confidence,
        "median_duration_seconds": row.median_duration_seconds,
        "steps": row.steps or [],
        "variables": row.variables or [],
        "branches": row.branches or [],
    }


def rehydrate_candidate(row: WorkflowCandidateRow) -> WorkflowCandidate:
    """Rebuild the mining dataclass from a stored candidate."""
    from app.mining.discovery import BranchPoint, StepVariable, WorkflowStep

    candidate = WorkflowCandidate(
        fingerprint=row.fingerprint,
        steps=[WorkflowStep(**step) for step in (row.steps or [])],
        variables=[StepVariable(**variable) for variable in (row.variables or [])],
        branches=[BranchPoint(**branch) for branch in (row.branches or [])],
        observation_count=row.observation_count,
        session_ids=list(row.session_ids or []),
        median_duration_seconds=row.median_duration_seconds,
        confidence=row.confidence,
    )
    candidate.human_steps = sum(1 for s in candidate.steps if s.requires_human)
    return candidate


def get_workflow(db: Session, tenant_id: uuid.UUID, workflow_id: uuid.UUID) -> WorkflowRow | None:
    return db.scalar(
        select(WorkflowRow).where(
            WorkflowRow.tenant_id == tenant_id, WorkflowRow.id == workflow_id
        )
    )
