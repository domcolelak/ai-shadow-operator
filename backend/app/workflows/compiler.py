"""Compiling a discovered workflow candidate into the execution DSL.

The compiler is conservative by design. Its job is not to produce the most
automated workflow it can, but the most automated workflow it can *justify*
from what was observed:

* a step whose value was sensitive becomes an ``approval``, never an ``input`` —
  the value was never captured, so there is nothing to replay even if we wanted
  to;
* a step observed in a minority of runs becomes optional rather than being
  dropped or made mandatory;
* a branch point becomes an ``approval`` asking a human which way to go, because
  the miner knows the runs diverged but not *why*;
* the final externally-visible action is left as a draft plus an approval,
  rather than being sent.

Anything the evidence does not settle is handed back to a person.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.capture.model import ValueClass
from app.dsl.schema import (
    Action,
    ActionKind,
    RiskLevel,
    Selector,
    Variable,
    Workflow,
)
from app.mining.discovery import WorkflowCandidate, WorkflowStep

#: Recorded actions that become DSL actions, and how.
_ACTION_MAP = {
    "navigate": ActionKind.NAVIGATE,
    "click": ActionKind.CLICK,
    "input": ActionKind.INPUT,
    "select": ActionKind.INPUT,
    "read": ActionKind.READ_TEXT,
}


@dataclass
class CompilationNote:
    step_index: int | None
    message: str

    def as_dict(self) -> dict:
        return {"step_index": self.step_index, "message": self.message}


@dataclass
class CompiledWorkflow:
    workflow: Workflow
    notes: list[CompilationNote] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "workflow": self.workflow.model_dump(mode="json"),
            "notes": [n.as_dict() for n in self.notes],
            "risk": self.workflow.risk.value,
            "requires_approval": self.workflow.requires_approval(),
        }


def _variable_name(step: WorkflowStep, used: set[str]) -> str:
    base = (step.field_name or step.label or f"input_{step.position}").strip().lower()
    base = "".join(c if c.isalnum() else "_" for c in base).strip("_") or f"input_{step.position}"
    name = base
    suffix = 2
    while name in used:
        name = f"{base}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def compile_candidate(
    candidate: WorkflowCandidate,
    *,
    name: str,
    description: str = "",
    allowed_origins: list[str] | None = None,
) -> CompiledWorkflow:
    """Turn a mined candidate into a workflow document."""
    notes: list[CompilationNote] = []
    variables: list[Variable] = []
    actions: list[Action] = []
    used_names: set[str] = set()

    variable_by_step = {v.step_index: v for v in candidate.variables}
    branch_after = {b.after_step: b for b in candidate.branches}

    origins = allowed_origins or sorted({s.origin for s in candidate.steps if s.origin})

    for step in candidate.steps:
        kind = _ACTION_MAP.get(step.action)
        if kind is None:
            notes.append(
                CompilationNote(step.position, f"step '{step.action}' has no DSL equivalent; skipped")
            )
            continue

        # A sensitive step was recorded without any value at all, so there is
        # nothing to replay. It becomes a hand-off, not an input.
        if step.requires_human or step.value_class == ValueClass.SENSITIVE.value:
            actions.append(
                Action(
                    kind=ActionKind.APPROVAL,
                    label=f"Human step: {step.label or step.field_name}",
                    optional=step.optional,
                )
            )
            notes.append(
                CompilationNote(
                    step.position,
                    "the value for this field was never captured, so the step stays with a person",
                )
            )
            continue

        selector = (
            Selector(role=step.role or "generic", name=step.label, field_name=step.field_name)
            if step.role or step.label or step.field_name
            else None
        )

        if kind is ActionKind.INPUT:
            variable = variable_by_step.get(step.position)
            if variable is not None:
                variable_name = _variable_name(step, used_names)
                variables.append(
                    Variable(
                        name=variable_name,
                        label=variable.label or step.label,
                        shape=variable.value_shape,
                        required=not step.optional,
                    )
                )
                actions.append(
                    Action(
                        kind=kind,
                        label=step.label or step.field_name,
                        selector=selector,
                        origin=step.origin,
                        variable=variable_name,
                        optional=step.optional,
                    )
                )
            else:
                # Observed to be the same in every run -- but the value itself
                # was never captured, only a hash of it. So this is not a
                # literal the compiler can emit; it is a value the operator has
                # to supply once. Modelling it as a required variable keeps the
                # workflow honestly un-runnable until somebody fills it in,
                # rather than shipping an input that silently types nothing.
                variable_name = _variable_name(step, used_names)
                variables.append(
                    Variable(
                        name=variable_name,
                        label=step.label or step.field_name,
                        shape="",
                        required=not step.optional,
                    )
                )
                actions.append(
                    Action(
                        kind=kind,
                        label=step.label or step.field_name,
                        selector=selector,
                        origin=step.origin,
                        variable=variable_name,
                        optional=step.optional,
                    )
                )
                notes.append(
                    CompilationNote(
                        step.position,
                        "this field held the same value in every run, but the value was never "
                        "recorded; set it once during review",
                    )
                )
        elif kind is ActionKind.NAVIGATE:
            actions.append(
                Action(
                    kind=kind,
                    label=step.label or step.target_path,
                    path=step.target_path or "/",
                    origin=step.origin,
                    optional=step.optional,
                )
            )
        else:
            actions.append(
                Action(
                    kind=kind,
                    label=step.label,
                    selector=selector,
                    origin=step.origin,
                    optional=step.optional,
                )
            )

        if step.optional:
            notes.append(
                CompilationNote(
                    step.position,
                    f"observed in {step.presence:.0%} of runs, so this step is optional",
                )
            )

        # A divergence the miner saw but cannot explain becomes a question.
        branch = branch_after.get(step.position)
        if branch is not None:
            shares = ", ".join(
                f"{alt['share']:.0%}" for alt in branch.alternatives[:3]
            )
            actions.append(
                Action(
                    kind=ActionKind.APPROVAL,
                    label=(
                        f"Runs diverged here ({shares}). Confirm which path applies "
                        f"before continuing."
                    ),
                )
            )
            notes.append(
                CompilationNote(
                    step.position,
                    "runs took different paths from this point; the reason was not observable, "
                    "so a person decides",
                )
            )

    actions = _soften_final_send(actions, notes)

    workflow = Workflow(
        name=name,
        description=description
        or f"Discovered from {candidate.observation_count} observed runs.",
        trigger="manual",
        allowed_origins=origins,
        variables=variables,
        actions=actions or [Action(kind=ActionKind.APPROVAL, label="Nothing automatable")],
    )
    return CompiledWorkflow(workflow=workflow, notes=notes)


def _soften_final_send(actions: list[Action], notes: list[CompilationNote]) -> list[Action]:
    """Turn a trailing high-risk click into draft + approval + the click.

    The last step of most support workflows is "send". Generating that as a
    bare click means the first dry run that is switched to live sends real mail
    to real customers. Preparing the work and stopping for a person is the
    behaviour that makes this product adoptable at all.
    """
    if not actions:
        return actions
    last = actions[-1]
    if last.kind is not ActionKind.CLICK or last.risk is not RiskLevel.HIGH:
        return actions

    notes.append(
        CompilationNote(
            None,
            f"'{last.label or last.describe()}' is externally visible; the workflow "
            f"prepares it and stops for approval rather than performing it unattended",
        )
    )
    return actions[:-1] + [
        Action(
            kind=ActionKind.CREATE_DRAFT,
            label=f"Prepare: {last.label or last.describe()}",
        ),
        Action(
            kind=ActionKind.APPROVAL,
            label=f"Approve before '{last.label or last.describe()}'",
        ),
        last,
    ]
