"""The execution engine.

Driver-agnostic: the runner interprets the DSL and calls a :class:`Driver` for
anything that touches the outside world. That separation is what makes
execution testable — the simulated driver runs the real engine against an
in-process portal, so the approval gates, the origin enforcement and the step
logging are all exercised for real.

Guarantees the engine enforces, regardless of driver:

* **A dry run never calls a mutating driver method.** It is not "a run with a
  flag set"; the mutating branches are not reached.
* **High-risk actions stop and wait.** Execution pauses at an approval and is
  resumable; it does not continue optimistically and undo later, because most
  of these actions cannot be undone.
* **Every action is checked against the workflow's origins at run time**, not
  only at validation. A stored workflow could have been edited.
* **Step logs are sanitised.** Inputs are recorded as the variable name and a
  shape, never the value.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Sequence

from app.capture.model import value_shape
from app.dsl.schema import Action, ActionKind, RiskLevel, Workflow


class RunStatus(str, Enum):
    COMPLETED = "completed"
    #: Stopped at an approval and can be resumed once granted.
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"
    #: A dry run: nothing outside the process was touched.
    DRY_RUN = "dry_run"


class StepStatus(str, Enum):
    OK = "ok"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    SIMULATED = "simulated"


class DriverError(Exception):
    """Something went wrong in the outside world."""


class Driver(ABC):
    """Everything the engine is allowed to do outside itself."""

    name = "driver"

    @abstractmethod
    def navigate(self, origin: str, path: str) -> dict: ...

    @abstractmethod
    def read_text(self, role: str, name: str) -> str: ...

    @abstractmethod
    def click(self, role: str, name: str) -> dict: ...

    @abstractmethod
    def fill(self, role: str, name: str, field_name: str, value: str) -> dict: ...

    @abstractmethod
    def wait_for(self, role: str, name: str, timeout: float) -> bool: ...

    @abstractmethod
    def create_draft(self, label: str, payload: dict) -> dict: ...

    def call_connector(self, connector: str, payload: dict) -> dict:
        raise DriverError(f"connector '{connector}' is not available in this driver")

    def notify(self, label: str, payload: dict) -> dict:
        return {"delivered": False, "reason": "no notification channel configured"}


@dataclass
class StepLog:
    index: int
    kind: str
    label: str
    status: StepStatus
    started_at: datetime
    finished_at: datetime
    risk: str
    #: Sanitised: variable names and shapes, never values.
    input_summary: dict[str, Any] = field(default_factory=dict)
    output_summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["started_at"] = self.started_at.isoformat()
        payload["finished_at"] = self.finished_at.isoformat()
        return payload


@dataclass
class RunResult:
    status: RunStatus
    steps: list[StepLog] = field(default_factory=list)
    #: Index of the action execution stopped at, if it did.
    paused_at: int | None = None
    pending_approval: str | None = None
    extracted: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "steps": [s.as_dict() for s in self.steps],
            "paused_at": self.paused_at,
            "pending_approval": self.pending_approval,
            "extracted": self.extracted,
            "error": self.error,
        }


@dataclass
class RunOptions:
    dry_run: bool = True
    #: Indices already approved by a human, for resuming a paused run.
    approved_steps: tuple[int, ...] = ()
    #: Connectors this tenant has allowlisted.
    allowed_connectors: tuple[str, ...] = ()
    #: Pre-authorise medium-risk actions. Never covers high risk.
    autonomous_medium_risk: bool = False
    start_at: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _summarise_input(action: Action, variables: dict[str, str]) -> dict[str, Any]:
    """What went into a step, without what went into it."""
    if action.kind is not ActionKind.INPUT:
        return {}
    if action.variable:
        supplied = variables.get(action.variable)
        return {
            "variable": action.variable,
            "provided": supplied is not None,
            "shape": value_shape(supplied) if supplied else "",
        }
    return {"constant": True, "shape": value_shape(action.value or "")}


def execute(
    workflow: Workflow,
    variables: dict[str, str],
    driver: Driver,
    options: RunOptions | None = None,
) -> RunResult:
    """Run a workflow. Dry by default."""
    opts = options or RunOptions()
    result = RunResult(status=RunStatus.DRY_RUN if opts.dry_run else RunStatus.COMPLETED)
    extracted: dict[str, Any] = {}

    missing = [
        v.name for v in workflow.variables if v.required and not variables.get(v.name)
    ]
    if missing and not opts.dry_run:
        result.status = RunStatus.FAILED
        result.error = f"missing required variable(s): {', '.join(missing)}"
        return result

    for index, action in enumerate(workflow.actions):
        if index < opts.start_at:
            continue

        started = _now()

        def log(status: StepStatus, *, output: dict | None = None, error: str | None = None) -> None:
            result.steps.append(
                StepLog(
                    index=index,
                    kind=action.kind.value,
                    label=action.label or action.describe(),
                    status=status,
                    started_at=started,
                    finished_at=_now(),
                    risk=action.risk.value,
                    input_summary=_summarise_input(action, variables),
                    output_summary=output or {},
                    error=error,
                )
            )

        # Origin enforcement at run time: a stored workflow may have been
        # edited since it was validated.
        if action.origin and workflow.allowed_origins and action.origin not in workflow.allowed_origins:
            log(StepStatus.BLOCKED, error=f"origin '{action.origin}' is outside the workflow's allowlist")
            result.status = RunStatus.FAILED
            result.error = f"action {index} targets a disallowed origin"
            result.paused_at = index
            return result

        if action.kind is ActionKind.API_CALL and action.connector.lower() not in {
            c.lower() for c in opts.allowed_connectors
        }:
            log(StepStatus.BLOCKED, error=f"connector '{action.connector}' is not allowlisted")
            result.status = RunStatus.FAILED
            result.error = f"action {index} uses a connector that is not allowlisted"
            result.paused_at = index
            return result

        if action.kind is ActionKind.APPROVAL:
            if opts.dry_run:
                log(StepStatus.SIMULATED, output={"would_pause": True})
                continue
            if index in opts.approved_steps:
                log(StepStatus.OK, output={"approved": True})
                continue
            log(StepStatus.BLOCKED, output={"awaiting": True})
            result.status = RunStatus.AWAITING_APPROVAL
            result.paused_at = index
            result.pending_approval = action.label or "approval required"
            result.extracted = extracted
            return result

        needs_approval = action.risk is RiskLevel.HIGH or (
            action.risk is RiskLevel.MEDIUM and not opts.autonomous_medium_risk
        )
        if needs_approval and not opts.dry_run and index not in opts.approved_steps:
            log(StepStatus.BLOCKED, output={"awaiting": True, "risk": action.risk.value})
            result.status = RunStatus.AWAITING_APPROVAL
            result.paused_at = index
            result.pending_approval = (
                f"{action.risk.value}-risk step: {action.label or action.describe()}"
            )
            result.extracted = extracted
            return result

        if opts.dry_run:
            # The mutating branches below are never reached in a dry run. This
            # is the point: a dry run cannot accidentally do anything.
            log(StepStatus.SIMULATED, output={"would_run": action.describe()})
            continue

        if action.optional and action.kind is ActionKind.INPUT and action.variable:
            if not variables.get(action.variable):
                log(StepStatus.SKIPPED, output={"reason": "optional, no value supplied"})
                continue

        try:
            output = _perform(action, variables, driver, extracted)
            log(StepStatus.OK, output=output)
        except DriverError as exc:
            log(StepStatus.FAILED, error=str(exc)[:300])
            if action.optional:
                continue
            result.status = RunStatus.FAILED
            result.error = str(exc)[:300]
            result.paused_at = index
            result.extracted = extracted
            return result

    result.extracted = extracted
    return result


def _perform(
    action: Action, variables: dict[str, str], driver: Driver, extracted: dict[str, Any]
) -> dict:
    """Translate one DSL action into driver calls.

    An unrecognised kind raises rather than being passed through: the driver
    must never receive something the engine does not understand.
    """
    if action.kind is ActionKind.NAVIGATE:
        return driver.navigate(action.origin, action.path)

    if action.kind is ActionKind.CLICK:
        return driver.click(action.selector.role, action.selector.name)

    if action.kind is ActionKind.INPUT:
        value = variables.get(action.variable, "") if action.variable else (action.value or "")
        driver.fill(
            action.selector.role, action.selector.name, action.selector.field_name, value
        )
        return {"filled": True, "shape": value_shape(value)}

    if action.kind in (ActionKind.READ_TEXT, ActionKind.EXTRACT):
        text = driver.read_text(action.selector.role, action.selector.name)
        if action.output:
            extracted[action.output] = text
        return {"read": True, "length": len(text or "")}

    if action.kind is ActionKind.WAIT_FOR:
        found = driver.wait_for(
            action.selector.role, action.selector.name, action.timeout_seconds
        )
        if not found:
            raise DriverError(f"timed out waiting for {action.selector.describe()}")
        return {"found": True}

    if action.kind is ActionKind.CREATE_DRAFT:
        return driver.create_draft(action.label, action.payload)

    if action.kind is ActionKind.NOTIFY:
        return driver.notify(action.label, action.payload)

    if action.kind is ActionKind.API_CALL:
        return driver.call_connector(action.connector, action.payload)

    if action.kind is ActionKind.TRANSFORM:
        source = action.payload.get("from")
        if action.output and source in extracted:
            extracted[action.output] = str(extracted[source]).strip()
        return {"transformed": bool(action.output)}

    if action.kind is ActionKind.CONDITION:
        # Conditions are recorded but not auto-resolved: the compiler emits an
        # approval alongside every branch it found.
        return {"evaluated": False, "reason": "branch resolution is a human decision"}

    raise DriverError(f"unsupported action kind '{action.kind}'")


def summarise_run(result: RunResult) -> dict:
    """A compact account of what a run did, for the UI and the audit trail."""
    by_status: dict[str, int] = {}
    for step in result.steps:
        by_status[step.status.value] = by_status.get(step.status.value, 0) + 1
    return {
        "status": result.status.value,
        "steps": len(result.steps),
        "by_status": by_status,
        "paused_at": result.paused_at,
        "pending_approval": result.pending_approval,
        "error": result.error,
    }
