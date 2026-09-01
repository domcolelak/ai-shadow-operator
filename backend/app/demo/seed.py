"""Demo workspace seeding."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import DEMO_TENANT_SLUG, hash_api_key
from app.demo.dataset import DEMO_EXPECTATIONS, DEMO_POLICY, generate_sessions
from app.models import RecordedAction, RecordingSession, Tenant, User
from app.sessions.service import record_events, run_discovery, tenant_salt

DEMO_API_KEY = "pk_demo_ai_shadow_operator"


def ensure_demo_tenant(db: Session) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.slug == DEMO_TENANT_SLUG))
    if tenant is None:
        tenant = Tenant(
            slug=DEMO_TENANT_SLUG,
            name="Demo workspace",
            api_key_hash=hash_api_key(DEMO_API_KEY),
            allowed_origins=list(DEMO_POLICY["allowed_origins"]),
            blocked_origins=list(DEMO_POLICY["blocked_origins"]),
            extra_sensitive_fields=list(DEMO_POLICY["extra_sensitive_fields"]),
            allowed_connectors=[],
        )
        db.add(tenant)
        db.flush()
        db.add(
            User(
                tenant_id=tenant.id,
                email="agent@demo.local",
                display_name="Demo support agent",
                role="operator",
            )
        )
        db.flush()
    return tenant


def seed_demo(db: Session, *, session_count: int = 30, force: bool = False) -> dict:
    """Create the demo tenant, recorded sessions and a first discovery run."""
    tenant = ensure_demo_tenant(db)

    existing = db.scalar(
        select(func.count(RecordingSession.id)).where(RecordingSession.tenant_id == tenant.id)
    )
    if existing and not force:
        return {"tenant_id": str(tenant.id), "created": False, "session_count": existing}

    for payload in generate_sessions(count=session_count):
        session = RecordingSession(
            tenant_id=tenant.id,
            external_id=payload["session_id"],
            user_email="agent@demo.local",
            device=payload["device"],
            salt=tenant_salt(db, tenant),
            status="recording",
            label="Support queue",
        )
        db.add(session)
        db.flush()
        record_events(db, tenant, session, payload["events"])
        session.status = "completed"
    db.flush()

    run = run_discovery(db, tenant.id)
    actions = db.scalar(
        select(func.count(RecordedAction.id)).where(RecordedAction.tenant_id == tenant.id)
    )

    return {
        "tenant_id": str(tenant.id),
        "created": True,
        "session_count": session_count,
        "action_count": actions,
        "discovery_run_id": str(run.id),
        "candidate_count": run.candidate_count,
        "demonstrates": DEMO_EXPECTATIONS,
    }
