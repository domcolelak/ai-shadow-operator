"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)

DESCRIPTION = """
Discovers repeated work from browser sessions the user explicitly recorded, and
turns the ones a person approves into safe, reviewable automations.

Consent is enforced in code, not in policy: only allowlisted origins are
recorded, password and other sensitive fields produce no stored value at all,
keystroke streams are refused outright, and a recording is deletable as a unit.

Repetition is detected algorithmically. Execution runs a restricted DSL with no
scripting primitive, high-risk steps stop for human approval, and a dry run
never reaches a mutating call.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("DEBUG" if settings.debug else "INFO")
    init_db()
    if settings.seed_demo_on_startup:
        from app.demo.seed import seed_demo

        with SessionLocal() as db:
            try:
                result = seed_demo(db)
                db.commit()
                logger.info("demo workspace ready: %s sessions", result.get("session_count"))
            except Exception:  # pragma: no cover - never block startup on demo data
                db.rollback()
                logger.exception("demo seeding failed")
    yield


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix=settings.api_prefix)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "version": app.version}
