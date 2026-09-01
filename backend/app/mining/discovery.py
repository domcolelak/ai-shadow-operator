"""Workflow discovery.

Turns recorded sessions into workflow candidates. The pattern detection is
algorithmic throughout — an LLM may later *name* a discovered workflow, but it
never decides that something repeated, because "this happened 34 times" has to
be a fact.

The pipeline:

1. segment each session into task-shaped runs,
2. drop noise actions,
3. fingerprint each run as a sequence of step signatures,
4. cluster runs whose sequences are close enough (edit distance),
5. merge each cluster into one canonical workflow,
6. classify each step's value as constant or variable,
7. mark steps that appear in only some runs as optional,
8. score confidence from repetition count and agreement.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Sequence

from app.capture.model import ActionType, CapturedAction, ValueClass, _generalise_path


@dataclass
class MiningConfig:
    #: A gap longer than this ends a run: the user moved on to something else.
    session_gap_minutes: float = 12.0
    #: Runs shorter than this are not workflows, they are stray clicks.
    min_run_length: int = 3
    #: A workflow must have been observed at least this many times.
    min_repetitions: int = 3
    #: Maximum normalised edit distance for two runs to be the same workflow.
    max_distance: float = 0.34
    #: A step present in at least this share of runs is required, not optional.
    required_step_share: float = 0.8
    #: A step present in at least this share of runs is worth showing even when
    #: it is absent from the most common variant.
    optional_step_share: float = 0.15
    #: Estimated seconds of human time a single step costs, when timings are
    #: unavailable. Only used as a fallback.
    fallback_seconds_per_step: float = 6.0


@dataclass
class Run:
    """One continuous stretch of work by one user."""

    session_id: str
    index: int
    actions: list[CapturedAction] = field(default_factory=list)

    @property
    def signatures(self) -> tuple[str, ...]:
        return tuple(a.step_signature() for a in self.actions)

    @property
    def duration_seconds(self) -> float:
        if len(self.actions) < 2:
            return 0.0
        return max(
            (self.actions[-1].occurred_at - self.actions[0].occurred_at).total_seconds(), 0.0
        )


@dataclass
class StepVariable:
    """A step whose value differs between runs."""

    step_index: int
    field_name: str
    label: str
    value_shape: str
    distinct_values: int
    observations: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorkflowStep:
    position: int
    signature: str
    action: str
    origin: str
    role: str
    label: str
    field_name: str
    target_path: str = ""
    value_class: str = ValueClass.UNKNOWN.value
    #: Share of runs containing this step.
    presence: float = 1.0
    optional: bool = False
    #: Set for sensitive fields: recorded as a step, never automated.
    requires_human: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BranchPoint:
    """A position where runs diverged into different next steps."""

    after_step: int
    alternatives: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"after_step": self.after_step, "alternatives": self.alternatives}


@dataclass
class WorkflowCandidate:
    fingerprint: str
    steps: list[WorkflowStep] = field(default_factory=list)
    variables: list[StepVariable] = field(default_factory=list)
    branches: list[BranchPoint] = field(default_factory=list)
    observation_count: int = 0
    session_ids: list[str] = field(default_factory=list)
    median_duration_seconds: float = 0.0
    confidence: float = 0.0
    #: Steps that must stay with a human because their value was sensitive.
    human_steps: int = 0

    @property
    def estimated_seconds_saved_per_month(self) -> float:
        """Deliberately conservative: only the automatable part counts."""
        automatable = max(len(self.steps) - self.human_steps, 0)
        if not self.steps:
            return 0.0
        share = automatable / len(self.steps)
        # Repetitions observed in the recorded window, projected to a month.
        return round(self.median_duration_seconds * share * self.observation_count, 1)

    def as_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "steps": [s.as_dict() for s in self.steps],
            "variables": [v.as_dict() for v in self.variables],
            "branches": [b.as_dict() for b in self.branches],
            "observation_count": self.observation_count,
            "session_ids": self.session_ids,
            "median_duration_seconds": self.median_duration_seconds,
            "confidence": self.confidence,
            "human_steps": self.human_steps,
            "estimated_seconds_saved_per_month": self.estimated_seconds_saved_per_month,
        }


# --------------------------------------------------------------------------
# 1-2. Segmentation and noise removal
# --------------------------------------------------------------------------


def segment_runs(
    actions: Sequence[CapturedAction], config: MiningConfig | None = None
) -> list[Run]:
    """Split a session's actions into task-shaped runs.

    A long pause is the strongest available signal that one task ended and
    another began. Without segmentation an eight-hour session is a single
    sequence and nothing repeats within it.
    """
    cfg = config or MiningConfig()
    gap = timedelta(minutes=cfg.session_gap_minutes)

    by_session: defaultdict[str, list[CapturedAction]] = defaultdict(list)
    for action in actions:
        if action.is_noise:
            continue
        by_session[action.session_id].append(action)

    runs: list[Run] = []
    for session_id, items in by_session.items():
        ordered = sorted(items, key=lambda a: (a.occurred_at, a.sequence))
        current: list[CapturedAction] = []
        index = 0
        for action in ordered:
            if current and action.occurred_at - current[-1].occurred_at > gap:
                if len(current) >= cfg.min_run_length:
                    runs.append(Run(session_id=session_id, index=index, actions=current))
                    index += 1
                current = []
            current.append(action)
        if len(current) >= cfg.min_run_length:
            runs.append(Run(session_id=session_id, index=index, actions=current))
    return runs


# --------------------------------------------------------------------------
# 3-4. Fingerprinting and clustering
# --------------------------------------------------------------------------


def normalised_distance(left: Sequence[str], right: Sequence[str]) -> float:
    """Levenshtein distance over step signatures, scaled to ``[0, 1]``.

    Sequence-aware rather than set-based: two workflows using the same steps in
    a different order are different workflows, and a set comparison would call
    them identical.
    """
    if not left and not right:
        return 0.0
    if not left or not right:
        return 1.0

    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (0 if a == b else 1),  # substitution
                )
            )
        previous = current
    return previous[-1] / max(len(left), len(right))


def cluster_runs(runs: Sequence[Run], config: MiningConfig | None = None) -> list[list[Run]]:
    """Group runs that are the same workflow performed on different data.

    Greedy single-pass clustering against each cluster's representative. Good
    enough here and, unlike an agglomerative pass, it is deterministic and
    explainable -- a reviewer can be told exactly which run a cluster grew from.
    """
    cfg = config or MiningConfig()
    clusters: list[list[Run]] = []
    representatives: list[tuple[str, ...]] = []

    # Longest first: a long run makes a better representative than a fragment
    # of itself, which would otherwise absorb the full runs into a short cluster.
    for run in sorted(runs, key=lambda r: (-len(r.actions), r.session_id, r.index)):
        signatures = run.signatures
        best_index = -1
        best_distance = cfg.max_distance
        for index, representative in enumerate(representatives):
            distance = normalised_distance(signatures, representative)
            if distance <= best_distance:
                best_distance = distance
                best_index = index
        if best_index >= 0:
            clusters[best_index].append(run)
        else:
            clusters.append([run])
            representatives.append(signatures)
    return clusters


# --------------------------------------------------------------------------
# 5-8. Merging into a canonical workflow
# --------------------------------------------------------------------------


def merge_cluster(runs: Sequence[Run], config: MiningConfig | None = None) -> WorkflowCandidate:
    """Build one canonical workflow from runs of the same shape."""
    cfg = config or MiningConfig()
    if not runs:
        return WorkflowCandidate(fingerprint="")

    # The most common sequence is the canonical skeleton; a rarer variant is a
    # variation of the norm rather than the norm itself.
    counts = Counter(run.signatures for run in runs)
    canonical_signatures = counts.most_common(1)[0][0]
    canonical_run = next(r for r in runs if r.signatures == canonical_signatures)

    presence = _step_presence(runs, canonical_signatures)
    steps: list[WorkflowStep] = []
    for position, action in enumerate(canonical_run.actions):
        signature = action.step_signature()
        share = presence.get(signature, 0.0)
        steps.append(
            WorkflowStep(
                position=position,
                signature=signature,
                action=action.action.value,
                origin=action.origin,
                role=action.element.role,
                label=action.element.accessible_name or action.element.field_name,
                field_name=action.element.field_name,
                # The generalised form, not this recording's concrete path.
                # Baking in '/orders/17306/detail' would send every future run
                # to the one order that happened to be open when the canonical
                # session was recorded.
                target_path=_generalise_path(action.target_path),
                value_class=action.value_class.value,
                presence=round(share, 4),
                optional=share < cfg.required_step_share,
                requires_human=action.value_class is ValueClass.SENSITIVE,
            )
        )

    _insert_minority_steps(runs, canonical_signatures, steps, cfg)

    variables = detect_variables(runs, steps)
    for variable in variables:
        steps[variable.step_index].value_class = ValueClass.VARIABLE.value
    _mark_constants(runs, steps)

    durations = sorted(r.duration_seconds for r in runs)
    median = durations[len(durations) // 2] if durations else 0.0

    candidate = WorkflowCandidate(
        fingerprint="|".join(canonical_signatures),
        steps=steps,
        variables=variables,
        branches=detect_branches(runs, canonical_signatures),
        observation_count=len(runs),
        session_ids=sorted({r.session_id for r in runs}),
        median_duration_seconds=round(median, 1),
        human_steps=sum(1 for s in steps if s.requires_human),
    )
    candidate.confidence = score_confidence(candidate, runs, cfg)
    return candidate


def _insert_minority_steps(
    runs: Sequence[Run],
    canonical: Sequence[str],
    steps: list[WorkflowStep],
    config: MiningConfig,
) -> None:
    """Add steps that a meaningful minority of runs performed.

    The canonical skeleton is the *most common* sequence, so anything done in a
    minority of runs is missing from it entirely -- and a step performed 30% of
    the time is precisely what an optional step is. Dropping it silently would
    hand the reviewer a workflow that does not match what they actually do, and
    an automation that skips a check somebody makes one time in three.

    Each such step is placed after the canonical step it most often followed,
    and marked optional.
    """
    total = len(runs) or 1
    canonical_set = set(canonical)

    #: signature -> (runs containing it, the canonical step it usually follows)
    counts: Counter[str] = Counter()
    predecessors: defaultdict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, CapturedAction] = {}
    #: Order of first appearance, so steps inserted at the same point keep the
    #: sequence the user actually performed them in.
    first_seen: dict[str, int] = {}
    order = 0

    for run in runs:
        seen_in_run: set[str] = set()
        last_canonical: str | None = None
        for action in run.actions:
            signature = action.step_signature()
            if signature in canonical_set:
                last_canonical = signature
                continue
            if signature not in seen_in_run:
                counts[signature] += 1
                seen_in_run.add(signature)
            predecessors[signature][last_canonical or ""] += 1
            if signature not in examples:
                examples[signature] = action
                first_seen[signature] = order
                order += 1

    additions: list[tuple[int, int, WorkflowStep]] = []
    for signature, run_count in counts.items():
        share = run_count / total
        if share < config.optional_step_share:
            continue
        action = examples[signature]
        after = predecessors[signature].most_common(1)[0][0]
        position = next(
            (i for i, step in enumerate(steps) if step.signature == after), -1
        )
        additions.append(
            (
                position + 1,
                first_seen.get(signature, 0),
                WorkflowStep(
                    position=0,  # renumbered below
                    signature=signature,
                    action=action.action.value,
                    origin=action.origin,
                    role=action.element.role,
                    label=action.element.accessible_name or action.element.field_name,
                    field_name=action.element.field_name,
                    target_path=_generalise_path(action.target_path),
                    value_class=action.value_class.value,
                    presence=round(share, 4),
                    optional=True,
                    requires_human=action.value_class is ValueClass.SENSITIVE,
                ),
            )
        )

    # Insert from the back so earlier indices stay valid. Within one insertion
    # point the later-observed step goes in first, so the pair ends up in the
    # order the user performed it -- otherwise "click history" and "read
    # history" come out reversed, which misrepresents the workflow.
    for index, _, step in sorted(additions, key=lambda item: (-item[0], -item[1])):
        steps.insert(min(index, len(steps)), step)
    for position, step in enumerate(steps):
        step.position = position


def _step_presence(runs: Sequence[Run], canonical: Sequence[str]) -> dict[str, float]:
    """Share of runs containing each canonical step."""
    total = len(runs) or 1
    presence: dict[str, float] = {}
    for signature in set(canonical):
        present = sum(1 for run in runs if signature in run.signatures)
        presence[signature] = present / total
    return presence


def detect_variables(runs: Sequence[Run], steps: Sequence[WorkflowStep]) -> list[StepVariable]:
    """Steps whose input differed between runs.

    Works entirely on hashes: the pipeline knows a value changed, never what it
    was. A field with one distinct value across many runs is a constant the
    automation can hold; more than one makes it an input the automation needs.
    """
    by_signature: defaultdict[str, list[CapturedAction]] = defaultdict(list)
    for run in runs:
        for action in run.actions:
            if action.action in (ActionType.INPUT, ActionType.SELECT):
                by_signature[action.step_signature()].append(action)

    variables: list[StepVariable] = []
    for step in steps:
        observations = by_signature.get(step.signature, [])
        if not observations:
            continue
        if any(a.value_class is ValueClass.SENSITIVE for a in observations):
            # A sensitive field is never a variable to be supplied; it stays
            # with the human.
            continue
        hashes = {a.value_hash for a in observations if a.value_hash}
        if len(hashes) <= 1:
            continue
        shapes = Counter(a.value_shape for a in observations if a.value_shape)
        variables.append(
            StepVariable(
                step_index=step.position,
                field_name=step.field_name or step.label,
                label=step.label or step.field_name,
                value_shape=shapes.most_common(1)[0][0] if shapes else "",
                distinct_values=len(hashes),
                observations=len(observations),
            )
        )
    return variables


def _mark_constants(runs: Sequence[Run], steps: Sequence[WorkflowStep]) -> None:
    by_signature: defaultdict[str, set[str]] = defaultdict(set)
    for run in runs:
        for action in run.actions:
            if action.action in (ActionType.INPUT, ActionType.SELECT) and action.value_hash:
                by_signature[action.step_signature()].add(action.value_hash)

    for step in steps:
        if step.value_class != ValueClass.UNKNOWN.value:
            continue
        hashes = by_signature.get(step.signature)
        if hashes and len(hashes) == 1:
            step.value_class = ValueClass.CONSTANT.value


def detect_branches(runs: Sequence[Run], canonical: Sequence[str]) -> list[BranchPoint]:
    """Positions where runs took different next steps.

    A branch is reported, never resolved: what the condition *means* is a
    judgement about the business, and guessing it is how an automation quietly
    starts doing the wrong thing in the case nobody tested.
    """
    branches: list[BranchPoint] = []
    for position in range(len(canonical) - 1):
        prefix = canonical[: position + 1]
        following: Counter[str] = Counter()
        for run in runs:
            signatures = run.signatures
            if len(signatures) <= position + 1:
                continue
            if signatures[: position + 1] != tuple(prefix):
                continue
            following[signatures[position + 1]] += 1

        if len(following) < 2:
            continue
        total = sum(following.values())
        branches.append(
            BranchPoint(
                after_step=position,
                alternatives=[
                    {
                        "signature": signature,
                        "runs": count,
                        "share": round(count / total, 4),
                    }
                    for signature, count in following.most_common()
                ],
            )
        )
    return branches


def score_confidence(
    candidate: WorkflowCandidate, runs: Sequence[Run], config: MiningConfig
) -> float:
    """How much the evidence supports automating this workflow.

    Repetition, agreement between runs, and the absence of unresolved
    branching. Capped below certainty: the system has watched a handful of
    sessions, not audited a process.
    """
    if not runs:
        return 0.0

    repetition = min(len(runs) / (config.min_repetitions * 4), 1.0)

    # Only the required steps. Optional steps are absent from most runs by
    # definition, so including them would make every run look like a poor match
    # for its own workflow and drive confidence down for having found more.
    canonical = tuple(s.signature for s in candidate.steps if not s.optional)
    optional = {s.signature for s in candidate.steps if s.optional}
    distances = [
        normalised_distance(
            tuple(sig for sig in run.signatures if sig not in optional), canonical
        )
        for run in runs
    ]
    agreement = 1.0 - (sum(distances) / len(distances))

    # Every branch is a place the automation could take the wrong path.
    branch_penalty = min(len(candidate.branches) * 0.12, 0.4)

    raw = 0.45 * repetition + 0.55 * max(agreement, 0.0) - branch_penalty
    return round(max(0.0, min(raw, 0.92)), 4)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def discover_workflows(
    actions: Sequence[CapturedAction], config: MiningConfig | None = None
) -> list[WorkflowCandidate]:
    """Full pipeline: actions in, ranked workflow candidates out."""
    cfg = config or MiningConfig()
    runs = segment_runs(actions, cfg)
    if not runs:
        return []

    candidates: list[WorkflowCandidate] = []
    for cluster in cluster_runs(runs, cfg):
        if len(cluster) < cfg.min_repetitions:
            continue
        candidate = merge_cluster(cluster, cfg)
        if len(candidate.steps) < cfg.min_run_length:
            continue
        candidates.append(candidate)

    candidates.sort(key=lambda c: (-c.observation_count, -c.confidence, c.fingerprint))
    return candidates
