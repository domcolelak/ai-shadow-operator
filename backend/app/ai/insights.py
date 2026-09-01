"""AI layer.

The model names and describes what the miner already found. It never decides
that something repeated, never sets a risk level, and never authorises a step.

The prompt receives step signatures, counts and shapes -- never a recorded
value, because none exist to send.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.provider import AICallResult, get_provider
from app.models import AILogEntry

SYSTEM_NAME = (
    "You are naming and describing a workflow that a deterministic miner "
    "discovered from recorded browser sessions. You are given the sequence of "
    "steps, how often it was observed, which fields varied and where runs "
    "diverged. Use only that. Never claim a step is safe to automate: risk is "
    "decided elsewhere."
)

SYSTEM_FAILURE = (
    "You are explaining why an automated workflow run stopped. You are given "
    "the step log, with inputs described only by variable name and shape. "
    "Explain what happened and what a person should check."
)


class WorkflowNarrative(BaseModel):
    name: str = Field(description="Short imperative name, e.g. 'Answer order status enquiry'")
    summary: str
    what_it_does: list[str] = Field(default_factory=list)
    what_stays_manual: list[str] = Field(default_factory=list)
    risks_to_review: list[str] = Field(default_factory=list)


class FailureNarrative(BaseModel):
    headline: str
    explanation: str
    likely_causes: list[str] = Field(default_factory=list)
    suggested_checks: list[str] = Field(default_factory=list)


def describe_candidate(
    db: Session, tenant_id: uuid.UUID, *, candidate: dict
) -> WorkflowNarrative | None:
    """Name and describe a mined candidate."""
    evidence = {
        "observed_runs": candidate.get("observation_count"),
        "confidence": candidate.get("confidence"),
        "median_duration_seconds": candidate.get("median_duration_seconds"),
        "steps": [
            {
                "position": s.get("position"),
                "action": s.get("action"),
                "label": s.get("label"),
                "origin": s.get("origin"),
                "optional": s.get("optional"),
                "value_class": s.get("value_class"),
                "requires_human": s.get("requires_human"),
            }
            for s in candidate.get("steps", [])
        ],
        "variables": [
            {"label": v.get("label"), "shape": v.get("value_shape")}
            for v in candidate.get("variables", [])
        ],
        "branch_points": len(candidate.get("branches", [])),
        "caveat": (
            "No recorded values are available: the capture layer stores hashes and "
            "shapes only. Do not invent example values."
        ),
    }
    result = get_provider().structured(
        system=SYSTEM_NAME,
        evidence=evidence,
        output_model=WorkflowNarrative,
        prompt_version="workflow-namer-v1",
    )
    _log(db, tenant_id, "describe_candidate", result)
    return result.output if result.ok else None


def explain_failure(
    db: Session, tenant_id: uuid.UUID, *, steps: list[dict], error: str | None
) -> FailureNarrative | None:
    result = get_provider().structured(
        system=SYSTEM_FAILURE,
        evidence={"steps": steps[-8:], "error": error},
        output_model=FailureNarrative,
        prompt_version="failure-explainer-v1",
    )
    _log(db, tenant_id, "explain_failure", result)
    return result.output if result.ok else None


def _log(db: Session, tenant_id: uuid.UUID, purpose: str, result: AICallResult) -> None:
    db.add(
        AILogEntry(
            tenant_id=tenant_id,
            purpose=purpose,
            model=result.model,
            prompt_version=result.prompt_version,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            error=result.error,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()
