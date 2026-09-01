from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP_DIR = Path(tempfile.mkdtemp(prefix="ai-shadow-operator-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite+pysqlite:///{_TMP_DIR / 'test.db'}")
os.environ.setdefault("SEED_DEMO_ON_STARTUP", "false")
os.environ.setdefault("AI_PROVIDER", "offline")

from app.capture.model import CapturePolicy, capture_batch  # noqa: E402
from app.demo.dataset import DEMO_POLICY, generate_sessions  # noqa: E402
from app.mining.discovery import discover_workflows  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.demo.seed import ensure_demo_tenant, seed_demo  # noqa: E402
from app.main import app  # noqa: E402

DEMO_SALT = "demo-salt"


@pytest.fixture(scope="session")
def policy():
    return CapturePolicy(allowed_origins=tuple(DEMO_POLICY["allowed_origins"]))


@pytest.fixture(scope="session")
def demo_actions(policy):
    """Every action the demo sessions produce, after capture filtering."""
    actions = []
    for session in generate_sessions(count=30):
        actions += capture_batch(
            session["events"],
            policy=policy,
            session_id=session["session_id"],
            salt=DEMO_SALT,
        ).actions
    return actions


@pytest.fixture(scope="session")
def demo_rejections(policy):
    rejected = []
    for session in generate_sessions(count=30):
        rejected += capture_batch(
            session["events"],
            policy=policy,
            session_id=session["session_id"],
            salt=DEMO_SALT,
        ).rejected
    return rejected


@pytest.fixture(scope="session")
def demo_candidates(demo_actions):
    return discover_workflows(demo_actions)


@pytest.fixture(scope="session")
def order_workflow(demo_candidates):
    """The main discovered workflow: answering 'where is my order'."""
    return demo_candidates[0]


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture()
def tenant(db):
    t = ensure_demo_tenant(db)
    db.commit()
    return t


@pytest.fixture()
def seeded(db, tenant):
    result = seed_demo(db, session_count=20)
    db.commit()
    return result


@pytest.fixture()
def client(seeded):
    with TestClient(app) as test_client:
        yield test_client
