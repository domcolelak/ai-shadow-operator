"""The execution DSL.

A workflow compiles into a restricted, declarative document. Nothing here
executes, compiles, imports or evaluates anything from the stored form — the
runner translates known action types into driver calls, and an action type it
does not recognise is refused rather than passed through.

There is deliberately no `script`, `eval`, `shell` or `http` primitive. An
outbound call can only name a connector the tenant has allowlisted, so adding a
new destination is an administrative act, not something a generated workflow
can do to itself.

Risk is a property of the action type, not a field somebody sets. A `send`
cannot be marked low-risk by whoever wrote the workflow.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field, model_validator


class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    READ_TEXT = "read_text"
    INPUT = "input"
    CLICK = "click"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    TRANSFORM = "transform"
    CONDITION = "condition"
    API_CALL = "api_call"
    CREATE_DRAFT = "create_draft"
    APPROVAL = "approval"
    NOTIFY = "notify"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: Risk per action kind. Fixed here, never carried on the action itself.
ACTION_RISK: dict[ActionKind, RiskLevel] = {
    ActionKind.NAVIGATE: RiskLevel.LOW,
    ActionKind.READ_TEXT: RiskLevel.LOW,
    ActionKind.WAIT_FOR: RiskLevel.LOW,
    ActionKind.EXTRACT: RiskLevel.LOW,
    ActionKind.TRANSFORM: RiskLevel.LOW,
    ActionKind.CONDITION: RiskLevel.LOW,
    ActionKind.CREATE_DRAFT: RiskLevel.LOW,
    ActionKind.APPROVAL: RiskLevel.LOW,
    ActionKind.INPUT: RiskLevel.MEDIUM,
    ActionKind.NOTIFY: RiskLevel.MEDIUM,
    # A click can submit, send, refund or delete. The DSL cannot tell from the
    # selector which it is, so the safe classification is the pessimistic one,
    # narrowed by the button's own name below.
    ActionKind.CLICK: RiskLevel.MEDIUM,
    ActionKind.API_CALL: RiskLevel.HIGH,
}

#: Words in a control's accessible name that make a click externally visible
#: or hard to undo.
HIGH_RISK_NAME_FRAGMENTS = (
    "send",
    "submit",
    "publish",
    "post",
    "confirm",
    "pay",
    "payment",
    "refund",
    "delete",
    "remove",
    "cancel order",
    "issue credit",
    "approve",
    "transfer",
    "archive",
)

#: Actions that always require a human before they run, whatever the policy.
ALWAYS_APPROVED_KINDS = frozenset({ActionKind.API_CALL})


class Selector(BaseModel):
    """A semantic selector. No CSS, no XPath."""

    role: str = Field(min_length=1, max_length=64)
    name: str = ""
    field_name: str = ""

    def describe(self) -> str:
        label = self.name or self.field_name or "(unnamed)"
        return f"{self.role} '{label}'"


class Action(BaseModel):
    kind: ActionKind
    #: Human-readable, shown in the review screen and the run log.
    label: str = ""
    selector: Selector | None = None
    #: Path only. A full URL would let a workflow leave the allowlisted origin.
    path: str = ""
    origin: str = ""
    #: For input: the variable to take the value from, or a literal constant.
    variable: str | None = None
    value: str | None = None
    #: For extract/transform: where to put the result.
    output: str | None = None
    #: For condition: which variable to test, and the branches.
    condition: dict[str, Any] | None = None
    #: For api_call: the allowlisted connector name.
    connector: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    optional: bool = False
    timeout_seconds: float = Field(default=15.0, gt=0, le=300)

    @model_validator(mode="after")
    def _check_shape(self) -> Action:
        if self.kind in (ActionKind.CLICK, ActionKind.INPUT, ActionKind.READ_TEXT,
                         ActionKind.WAIT_FOR, ActionKind.EXTRACT):
            if self.selector is None:
                raise ValueError(f"'{self.kind.value}' requires a selector")
        if self.kind is ActionKind.NAVIGATE and not self.path:
            raise ValueError("'navigate' requires a path")
        if self.kind is ActionKind.NAVIGATE and "://" in self.path:
            raise ValueError("'navigate' takes a path, not a full URL")
        if self.kind is ActionKind.INPUT and not (self.variable or self.value):
            raise ValueError("'input' requires either a variable or a literal value")
        if self.kind is ActionKind.API_CALL and not self.connector:
            raise ValueError("'api_call' requires an allowlisted connector name")
        if self.kind is ActionKind.CONDITION and not self.condition:
            raise ValueError("'condition' requires a condition body")
        return self

    @property
    def risk(self) -> RiskLevel:
        base = ACTION_RISK[self.kind]
        if self.kind is ActionKind.CLICK and self.selector is not None:
            haystack = f"{self.selector.name} {self.label}".lower()
            if any(fragment in haystack for fragment in HIGH_RISK_NAME_FRAGMENTS):
                return RiskLevel.HIGH
        return base

    def describe(self) -> str:
        if self.selector is not None:
            return f"{self.kind.value} {self.selector.describe()}"
        if self.path:
            return f"{self.kind.value} {self.path}"
        if self.connector:
            return f"{self.kind.value} via {self.connector}"
        return self.kind.value


class Variable(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    label: str = ""
    shape: str = ""
    required: bool = True
    #: Never populated from a recorded value; the operator supplies it.
    example: str = ""


class Workflow(BaseModel):
    """A compiled, executable workflow."""

    name: str = Field(min_length=1)
    description: str = ""
    trigger: Literal["manual", "scheduled"] = "manual"
    #: Origins this workflow may touch. Enforced at run time, not just here.
    allowed_origins: list[str] = Field(default_factory=list)
    variables: list[Variable] = Field(default_factory=list)
    actions: list[Action] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_workflow(self) -> Workflow:
        names = {v.name for v in self.variables}
        for index, action in enumerate(self.actions):
            if action.variable and action.variable not in names:
                raise ValueError(
                    f"action {index} references undeclared variable '{action.variable}'"
                )
            if action.origin and self.allowed_origins and action.origin not in self.allowed_origins:
                raise ValueError(
                    f"action {index} targets '{action.origin}', which is not in "
                    f"allowed_origins"
                )
        return self

    @property
    def risk(self) -> RiskLevel:
        levels = [a.risk for a in self.actions]
        if RiskLevel.HIGH in levels:
            return RiskLevel.HIGH
        if RiskLevel.MEDIUM in levels:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def high_risk_actions(self) -> list[tuple[int, Action]]:
        return [(i, a) for i, a in enumerate(self.actions) if a.risk is RiskLevel.HIGH]

    def requires_approval(self, *, approve_high_risk: bool = False) -> bool:
        """Whether a run needs a human before the risky steps execute.

        ``approve_high_risk`` lets a tenant pre-authorise, but it cannot cover
        the kinds in :data:`ALWAYS_APPROVED_KINDS`.
        """
        if any(a.kind in ALWAYS_APPROVED_KINDS for a in self.actions):
            return True
        if approve_high_risk:
            return False
        return bool(self.high_risk_actions())


class ValidationIssue(BaseModel):
    index: int | None = None
    severity: Literal["error", "warning"]
    message: str


def validate_workflow(payload: dict[str, Any], *, allowed_connectors: Sequence[str] = ()) -> list[ValidationIssue]:
    """Check an untrusted workflow document.

    Returns issues rather than raising, so a review screen can show everything
    wrong at once instead of one problem per attempt.
    """
    issues: list[ValidationIssue] = []
    try:
        workflow = Workflow.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError and friends
        return [ValidationIssue(severity="error", message=str(exc)[:500])]

    allowed = {c.lower() for c in allowed_connectors}
    for index, action in enumerate(workflow.actions):
        if action.kind is ActionKind.API_CALL and action.connector.lower() not in allowed:
            issues.append(
                ValidationIssue(
                    index=index,
                    severity="error",
                    message=(
                        f"connector '{action.connector}' is not allowlisted for this "
                        f"tenant; add it before this workflow can run"
                    ),
                )
            )
        if action.risk is RiskLevel.HIGH:
            issues.append(
                ValidationIssue(
                    index=index,
                    severity="warning",
                    message=(
                        f"{action.describe()} is high risk and will require approval "
                        f"before it runs"
                    ),
                )
            )

    if workflow.allowed_origins:
        for index, action in enumerate(workflow.actions):
            if action.origin and action.origin not in workflow.allowed_origins:
                issues.append(
                    ValidationIssue(
                        index=index,
                        severity="error",
                        message=f"action targets '{action.origin}', outside the workflow's origins",
                    )
                )

    declared = {v.name for v in workflow.variables}
    used = {a.variable for a in workflow.actions if a.variable}
    for unused in sorted(declared - used):
        issues.append(
            ValidationIssue(
                severity="warning", message=f"variable '{unused}' is declared but never used"
            )
        )
    return issues


def parse_workflow(payload: dict[str, Any]) -> Workflow:
    """Validate an untrusted payload into a :class:`Workflow`."""
    return Workflow.model_validate(payload)
