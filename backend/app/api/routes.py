"""HTTP API.

Every endpoint resolves a :class:`TenantContext` first and filters every query
by ``ctx.tenant_id``. Cross-tenant access returns 404, not 403.

Literal paths are declared before parameterised ones on the same prefix:
FastAPI matches in declaration order.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.insights import describe_candidate, explain_failure
from app.audit.log import record_audit
from app.capture.model import summarise_capture
from app.core.db import get_db
from app.core.security import TenantContext, current_tenant
from app.dsl.schema import RiskLevel, parse_workflow, validate_workflow
from app.execution.drivers.simulated import SimulatedDriver, demo_portal
from app.execution.runner import RunOptions, RunStatus, execute
from app.mining.discovery import MiningConfig
from app.models import (
    Approval,
    DiscoveryRun,
    Execution,
    RecordedAction,
    RecordingSession,
    Tenant,
    WorkflowCandidateRow,
    WorkflowRow,
)
from app.schemas import (
    AcceptCandidate,
    CandidateOut,
    ConsentPolicyOut,
    ConsentPolicyUpdate,
    DiscoveryRequest,
    DiscoveryRunOut,
    EventBatch,
    EventBatchResponse,
    ExecutionOut,
    OverviewResponse,
    ResumeRequest,
    RunRequest,
    SessionOut,
    SessionStart,
    SessionSummary,
    WorkflowDetail,
    WorkflowEdit,
    WorkflowOut,
    WorkflowStateUpdate,
)
from app.sessions.service import (
    delete_session,
    get_session,
    get_workflow,
    load_actions,
    tenant_salt,
    record_events,
    rehydrate_candidate,
    run_discovery,
)
from app.workflows.compiler import compile_candidate

router = APIRouter()

CAPTURE_NOTE = (
    "Values are stored as salted hashes and shape descriptions only. Password and "
    "other sensitive fields produce no value at all, keystroke events are refused, "
    "and screenshots are never captured."
)


# --------------------------------------------------------------------------
# Consent
# --------------------------------------------------------------------------


def _consent(tenant: Tenant) -> ConsentPolicyOut:
    return ConsentPolicyOut(
        allowed_origins=list(tenant.allowed_origins or []),
        blocked_origins=list(tenant.blocked_origins or []),
        extra_sensitive_fields=list(tenant.extra_sensitive_fields or []),
        allowed_connectors=list(tenant.allowed_connectors or []),
        note=CAPTURE_NOTE,
    )


@router.get("/consent", response_model=ConsentPolicyOut)
def get_consent(
    ctx: TenantContext = Depends(current_tenant), db: Session = Depends(get_db)
) -> ConsentPolicyOut:
    """Exactly what this workspace records. Nothing outside this is captured."""
    return _consent(_tenant(db, ctx))


@router.patch("/consent", response_model=ConsentPolicyOut)
def update_consent(
    body: ConsentPolicyUpdate,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> ConsentPolicyOut:
    tenant = _tenant(db, ctx)
    for name, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(tenant, name, value)
    db.flush()
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="consent.updated",
        object_type="tenant",
        object_id=tenant.id,
        payload=body.model_dump(exclude_unset=True),
    )
    return _consent(tenant)


# --------------------------------------------------------------------------
# Recording sessions
# --------------------------------------------------------------------------


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(
    ctx: TenantContext = Depends(current_tenant), db: Session = Depends(get_db)
) -> list[RecordingSession]:
    return list(
        db.scalars(
            select(RecordingSession)
            .where(RecordingSession.tenant_id == ctx.tenant_id)
            .order_by(RecordingSession.started_at.desc())
        )
    )


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def start_session(
    body: SessionStart,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> RecordingSession:
    """Start a recording. Nothing is captured until this is called."""
    tenant = _tenant(db, ctx)
    if not tenant.allowed_origins:
        raise HTTPException(
            status_code=409,
            detail=(
                "no origins are allowlisted for this workspace, so nothing may be "
                "recorded; set the consent policy first"
            ),
        )
    clash = db.scalar(
        select(RecordingSession).where(
            RecordingSession.tenant_id == ctx.tenant_id,
            RecordingSession.external_id == body.external_id,
        )
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail="external_id already used")

    session = RecordingSession(
        tenant_id=ctx.tenant_id,
        external_id=body.external_id,
        user_email=body.user_email,
        device=body.device,
        label=body.label,
        salt=tenant_salt(db, tenant),
        status="recording",
    )
    db.add(session)
    db.flush()
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="session.started",
        object_type="session",
        object_id=session.id,
    )
    return session


@router.post("/sessions/{session_id}/events", response_model=EventBatchResponse)
def push_events(
    session_id: uuid.UUID,
    body: EventBatch,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> EventBatchResponse:
    """Send captured events. Filtered against the consent policy on arrival."""
    session = _require_session(db, ctx, session_id)
    try:
        result = record_events(db, _tenant(db, ctx), session, body.events)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return EventBatchResponse(session_id=session_id, note=CAPTURE_NOTE, **result)


@router.post("/sessions/{session_id}/complete", response_model=SessionOut)
def complete_session(
    session_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> RecordingSession:
    session = _require_session(db, ctx, session_id)
    session.status = "completed"
    session.completed_at = datetime.now(timezone.utc)
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="session.completed",
        object_type="session",
        object_id=session_id,
        payload={"actions": session.action_count},
    )
    return session


@router.get("/sessions/{session_id}/summary", response_model=SessionSummary)
def session_summary(
    session_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> SessionSummary:
    """Exactly what this session kept, for the person who recorded it."""
    _require_session(db, ctx, session_id)
    actions = load_actions(db, ctx.tenant_id, session_ids=[session_id])
    return SessionSummary(session_id=session_id, **summarise_capture(actions))


@router.delete("/sessions/{session_id}", status_code=status.HTTP_200_OK)
def remove_session(
    session_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    """Delete a recording and every action derived from it."""
    try:
        removed = delete_session(db, ctx.tenant_id, session_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="session.deleted",
        object_type="session",
        object_id=session_id,
        payload={"actions_removed": removed},
    )
    return {"deleted": True, "actions_removed": removed}


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@router.post("/discovery-runs", response_model=DiscoveryRunOut, status_code=status.HTTP_201_CREATED)
def create_discovery_run(
    body: DiscoveryRequest,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> DiscoveryRun:
    run = run_discovery(
        db,
        ctx.tenant_id,
        config=MiningConfig(
            min_repetitions=body.min_repetitions,
            session_gap_minutes=body.session_gap_minutes,
            max_distance=body.max_distance,
        ),
    )
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="discovery.run",
        object_type="discovery_run",
        object_id=run.id,
        payload={"candidates": run.candidate_count},
    )
    return run


@router.get("/workflow-candidates", response_model=list[CandidateOut])
def list_candidates(
    candidate_status: str = Query("candidate", alias="status"),
    limit: int = Query(50, ge=1, le=200),
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> list[WorkflowCandidateRow]:
    stmt = select(WorkflowCandidateRow).where(WorkflowCandidateRow.tenant_id == ctx.tenant_id)
    if candidate_status:
        stmt = stmt.where(WorkflowCandidateRow.status == candidate_status)
    return list(
        db.scalars(stmt.order_by(WorkflowCandidateRow.observation_count.desc()).limit(limit))
    )


@router.get("/workflow-candidates/{candidate_id}", response_model=CandidateOut)
def get_candidate(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> WorkflowCandidateRow:
    return _require_candidate(db, ctx, candidate_id)


@router.post("/workflow-candidates/{candidate_id}/describe", response_model=CandidateOut)
def describe(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> WorkflowCandidateRow:
    candidate = _require_candidate(db, ctx, candidate_id)
    narrative = describe_candidate(
        db,
        ctx.tenant_id,
        candidate={
            "observation_count": candidate.observation_count,
            "confidence": candidate.confidence,
            "median_duration_seconds": candidate.median_duration_seconds,
            "steps": candidate.steps,
            "variables": candidate.variables,
            "branches": candidate.branches,
        },
    )
    if narrative is not None:
        candidate.narrative = narrative.model_dump()
    db.flush()
    return candidate


@router.post("/workflow-candidates/{candidate_id}/accept", response_model=WorkflowDetail)
def accept_candidate(
    candidate_id: uuid.UUID,
    body: AcceptCandidate,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> WorkflowDetail:
    """Compile a candidate into a workflow. It starts as a draft, never enabled."""
    candidate = _require_candidate(db, ctx, candidate_id)
    compiled = compile_candidate(
        rehydrate_candidate(candidate),
        name=body.name or candidate.name or "Discovered workflow",
        description=body.description,
    )
    definition = compiled.workflow.model_dump(mode="json")

    row = WorkflowRow(
        tenant_id=ctx.tenant_id,
        candidate_id=candidate.id,
        name=compiled.workflow.name,
        description=compiled.workflow.description,
        # Acceptance is a review step, not a deployment.
        state="draft",
        definition=definition,
        compilation_notes=[n.as_dict() for n in compiled.notes],
        risk=compiled.workflow.risk.value,
    )
    db.add(row)
    candidate.status = "accepted"
    db.flush()

    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="workflow.accepted",
        object_type="workflow",
        object_id=row.id,
        payload={"candidate_id": str(candidate_id), "risk": row.risk},
    )
    return _workflow_detail(db, ctx, row)


@router.post("/workflow-candidates/{candidate_id}/reject", response_model=CandidateOut)
def reject_candidate(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> WorkflowCandidateRow:
    candidate = _require_candidate(db, ctx, candidate_id)
    candidate.status = "rejected"
    return candidate


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------


@router.get("/workflows", response_model=list[WorkflowOut])
def list_workflows(
    ctx: TenantContext = Depends(current_tenant), db: Session = Depends(get_db)
) -> list[WorkflowRow]:
    return list(
        db.scalars(
            select(WorkflowRow)
            .where(WorkflowRow.tenant_id == ctx.tenant_id)
            .order_by(WorkflowRow.created_at.desc())
        )
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetail)
def get_workflow_detail(
    workflow_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> WorkflowDetail:
    return _workflow_detail(db, ctx, _require_workflow(db, ctx, workflow_id))


@router.put("/workflows/{workflow_id}", response_model=WorkflowDetail)
def edit_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowEdit,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> WorkflowDetail:
    workflow = _require_workflow(db, ctx, workflow_id)
    try:
        parsed = parse_workflow(body.definition)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid workflow: {exc}") from exc

    workflow.definition = parsed.model_dump(mode="json")
    workflow.name = parsed.name
    workflow.risk = parsed.risk.value
    # An edit invalidates any earlier approval of this workflow's text.
    if workflow.state == "enabled":
        workflow.state = "draft"
    db.flush()
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="workflow.edited",
        object_type="workflow",
        object_id=workflow_id,
    )
    return _workflow_detail(db, ctx, workflow)


@router.post("/workflows/{workflow_id}/state", response_model=WorkflowOut)
def set_workflow_state(
    workflow_id: uuid.UUID,
    body: WorkflowStateUpdate,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> WorkflowRow:
    workflow = _require_workflow(db, ctx, workflow_id)
    workflow.state = body.state
    workflow.autonomous_medium_risk = body.autonomous_medium_risk
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="workflow.state_changed",
        object_type="workflow",
        object_id=workflow_id,
        payload={
            "state": body.state,
            "autonomous_medium_risk": body.autonomous_medium_risk,
        },
    )
    return workflow


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/run", response_model=ExecutionOut, status_code=201)
def run_workflow(
    workflow_id: uuid.UUID,
    body: RunRequest,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Execution:
    """Run a workflow. Dry by default; a live run needs the workflow enabled."""
    workflow = _require_workflow(db, ctx, workflow_id)
    tenant = _tenant(db, ctx)

    if not body.dry_run and workflow.state != "enabled":
        raise HTTPException(
            status_code=409,
            detail=f"workflow is '{workflow.state}'; only an enabled workflow may run live",
        )

    parsed = parse_workflow(workflow.definition)
    result = execute(
        parsed,
        body.variables,
        SimulatedDriver(demo_portal()),
        RunOptions(
            dry_run=body.dry_run,
            allowed_connectors=tuple(tenant.allowed_connectors or ()),
            autonomous_medium_risk=workflow.autonomous_medium_risk,
        ),
    )
    return _persist_execution(db, ctx, workflow, body, result, [])


@router.post("/executions/{execution_id}/approve", response_model=ExecutionOut)
def approve_step(
    execution_id: uuid.UUID,
    body: ResumeRequest,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Execution:
    """Approve or reject the step a paused run stopped at, then continue."""
    execution = _require_execution(db, ctx, execution_id)
    if execution.status != RunStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=409, detail="this run is not waiting for approval")
    if execution.paused_at != body.step_index:
        raise HTTPException(
            status_code=409,
            detail=f"this run is paused at step {execution.paused_at}, not {body.step_index}",
        )

    db.add(
        Approval(
            tenant_id=ctx.tenant_id,
            execution_id=execution_id,
            step_index=body.step_index,
            decision=body.decision,
            reason=body.reason,
            approved_by=body.approved_by,
        )
    )
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="execution.approval",
        object_type="execution",
        object_id=execution_id,
        payload={"step": body.step_index, "decision": body.decision, "by": body.approved_by},
    )

    if body.decision == "reject":
        execution.status = "rejected"
        execution.pending_approval = None
        db.flush()
        return execution

    workflow = _require_workflow(db, ctx, execution.workflow_id)
    tenant = _tenant(db, ctx)
    approved = sorted({*(execution.approved_steps or []), body.step_index})

    result = execute(
        parse_workflow(workflow.definition),
        body.variables,
        SimulatedDriver(demo_portal()),
        RunOptions(
            dry_run=execution.dry_run,
            approved_steps=tuple(approved),
            allowed_connectors=tuple(tenant.allowed_connectors or ()),
            autonomous_medium_risk=workflow.autonomous_medium_risk,
        ),
    )

    execution.status = result.status.value
    execution.paused_at = result.paused_at
    execution.pending_approval = result.pending_approval
    execution.steps = [s.as_dict() for s in result.steps]
    execution.error = result.error
    execution.approved_steps = approved
    db.flush()
    return execution


@router.get("/executions", response_model=list[ExecutionOut])
def list_executions(
    workflow_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> list[Execution]:
    stmt = select(Execution).where(Execution.tenant_id == ctx.tenant_id)
    if workflow_id:
        stmt = stmt.where(Execution.workflow_id == workflow_id)
    return list(db.scalars(stmt.order_by(Execution.created_at.desc()).limit(limit)))


@router.get("/executions/{execution_id}", response_model=ExecutionOut)
def get_execution(
    execution_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> Execution:
    return _require_execution(db, ctx, execution_id)


@router.post("/executions/{execution_id}/explain")
def explain_execution(
    execution_id: uuid.UUID,
    ctx: TenantContext = Depends(current_tenant),
    db: Session = Depends(get_db),
) -> dict:
    execution = _require_execution(db, ctx, execution_id)
    narrative = explain_failure(
        db, ctx.tenant_id, steps=execution.steps or [], error=execution.error
    )
    return {
        "execution_id": str(execution_id),
        "narrative": narrative.model_dump() if narrative else None,
    }


@router.get("/overview", response_model=OverviewResponse)
def overview(
    ctx: TenantContext = Depends(current_tenant), db: Session = Depends(get_db)
) -> OverviewResponse:
    def count(model, *conditions):
        return (
            db.scalar(
                select(func.count(model.id)).where(model.tenant_id == ctx.tenant_id, *conditions)
            )
            or 0
        )

    saved = (
        db.scalar(
            select(func.sum(WorkflowCandidateRow.estimated_seconds_saved)).where(
                WorkflowCandidateRow.tenant_id == ctx.tenant_id,
                WorkflowCandidateRow.status == "candidate",
            )
        )
        or 0.0
    )
    rejected = (
        db.scalar(
            select(func.sum(RecordingSession.rejected_count)).where(
                RecordingSession.tenant_id == ctx.tenant_id
            )
        )
        or 0
    )
    top = list(
        db.scalars(
            select(WorkflowCandidateRow)
            .where(
                WorkflowCandidateRow.tenant_id == ctx.tenant_id,
                WorkflowCandidateRow.status == "candidate",
            )
            .order_by(WorkflowCandidateRow.observation_count.desc())
            .limit(5)
        )
    )

    return OverviewResponse(
        session_count=count(RecordingSession),
        action_count=count(RecordedAction),
        rejected_count=int(rejected),
        candidate_count=count(WorkflowCandidateRow, WorkflowCandidateRow.status == "candidate"),
        workflow_count=count(WorkflowRow),
        enabled_workflows=count(WorkflowRow, WorkflowRow.state == "enabled"),
        executions=count(Execution),
        live_executions=count(Execution, Execution.dry_run.is_(False)),
        awaiting_approval=count(Execution, Execution.status == "awaiting_approval"),
        estimated_seconds_saved=round(float(saved), 1),
        consent=_consent(_tenant(db, ctx)),
        top_candidates=[CandidateOut.model_validate(c) for c in top],
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _persist_execution(db, ctx, workflow, body, result, approved) -> Execution:
    execution = Execution(
        tenant_id=ctx.tenant_id,
        workflow_id=workflow.id,
        dry_run=body.dry_run,
        status=result.status.value,
        # Names only. The values never reach storage.
        supplied_variables=sorted(body.variables),
        approved_steps=approved,
        paused_at=result.paused_at,
        pending_approval=result.pending_approval,
        steps=[s.as_dict() for s in result.steps],
        error=result.error,
        started_by=body.started_by,
    )
    db.add(execution)
    db.flush()
    record_audit(
        db,
        tenant_id=ctx.tenant_id,
        action="execution.started",
        object_type="execution",
        object_id=execution.id,
        payload={"dry_run": body.dry_run, "status": execution.status},
    )
    return execution


def _workflow_detail(db: Session, ctx: TenantContext, row: WorkflowRow) -> WorkflowDetail:
    tenant = _tenant(db, ctx)
    issues = validate_workflow(
        row.definition, allowed_connectors=tuple(tenant.allowed_connectors or ())
    )
    parsed = parse_workflow(row.definition)
    return WorkflowDetail(
        **WorkflowOut.model_validate(row).model_dump(),
        validation_issues=[i.model_dump() for i in issues],
        requires_approval=parsed.requires_approval(
            approve_high_risk=False
        ),
        high_risk_steps=[
            {"index": index, "label": action.label or action.describe()}
            for index, action in parsed.high_risk_actions()
        ],
    )


def _tenant(db: Session, ctx: TenantContext) -> Tenant:
    tenant = db.get(Tenant, ctx.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant


def _require_session(
    db: Session, ctx: TenantContext, session_id: uuid.UUID
) -> RecordingSession:
    session = get_session(db, ctx.tenant_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


def _require_candidate(
    db: Session, ctx: TenantContext, candidate_id: uuid.UUID
) -> WorkflowCandidateRow:
    candidate = db.scalar(
        select(WorkflowCandidateRow).where(
            WorkflowCandidateRow.tenant_id == ctx.tenant_id,
            WorkflowCandidateRow.id == candidate_id,
        )
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="workflow candidate not found")
    return candidate


def _require_workflow(db: Session, ctx: TenantContext, workflow_id: uuid.UUID) -> WorkflowRow:
    workflow = get_workflow(db, ctx.tenant_id, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="workflow not found")
    return workflow


def _require_execution(db: Session, ctx: TenantContext, execution_id: uuid.UUID) -> Execution:
    execution = db.scalar(
        select(Execution).where(
            Execution.tenant_id == ctx.tenant_id, Execution.id == execution_id
        )
    )
    if execution is None:
        raise HTTPException(status_code=404, detail="execution not found")
    return execution
