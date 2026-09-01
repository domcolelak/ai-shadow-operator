"""Tests for workflow discovery, DSL validation, compilation and execution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.capture.model import ActionType, CapturedAction, ElementRef, ValueClass
from app.demo.dataset import DEMO_EXPECTATIONS
from app.dsl.schema import (
    Action,
    ActionKind,
    RiskLevel,
    Selector,
    Variable,
    Workflow,
    parse_workflow,
    validate_workflow,
)
from app.execution.drivers.simulated import SimulatedDriver, demo_portal
from app.execution.runner import RunOptions, RunStatus, StepStatus, execute
from app.mining.discovery import (
    MiningConfig,
    cluster_runs,
    discover_workflows,
    normalised_distance,
    segment_runs,
)
from app.workflows.compiler import compile_candidate

BASE = datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc)


def action(
    session: str,
    sequence: int,
    *,
    kind: ActionType = ActionType.CLICK,
    name: str = "Button",
    field_name: str = "",
    minutes: float = 0.0,
    value_hash: str | None = None,
    value_class: ValueClass = ValueClass.UNKNOWN,
    origin: str = "portal.demo.local",
    path: str = "",
) -> CapturedAction:
    return CapturedAction(
        session_id=session,
        sequence=sequence,
        occurred_at=BASE + timedelta(minutes=minutes),
        action=kind,
        origin=origin,
        element=ElementRef(role="button", accessible_name=name, field_name=field_name),
        target_path=path,
        value_class=value_class,
        value_hash=value_hash,
    )


def run_actions(session: str, names: list[str], *, start_minutes: float = 0.0, **kwargs):
    return [
        action(session, i, name=n, minutes=start_minutes + i * 0.2, **kwargs)
        for i, n in enumerate(names)
    ]


class TestSegmentation:
    def test_a_long_gap_starts_a_new_run(self):
        actions = run_actions("S1", ["A", "B", "C"])
        actions += run_actions("S1", ["A", "B", "C"], start_minutes=60)
        runs = segment_runs(actions)
        assert len(runs) == 2

    def test_a_short_gap_stays_one_run(self):
        runs = segment_runs(run_actions("S1", ["A", "B", "C", "D"]))
        assert len(runs) == 1

    def test_short_runs_are_dropped(self):
        assert segment_runs(run_actions("S1", ["A"])) == []

    def test_noise_actions_are_removed(self):
        actions = run_actions("S1", ["A", "B", "C"])
        actions.append(action("S1", 9, kind=ActionType.SCROLL, name="scroll", minutes=0.7))
        runs = segment_runs(actions)
        assert all(a.action is not ActionType.SCROLL for a in runs[0].actions)

    def test_sessions_are_kept_apart(self):
        actions = run_actions("S1", ["A", "B", "C"]) + run_actions("S2", ["A", "B", "C"])
        runs = segment_runs(actions)
        assert {r.session_id for r in runs} == {"S1", "S2"}


class TestDistance:
    def test_identical_sequences(self):
        assert normalised_distance(("a", "b"), ("a", "b")) == 0.0

    def test_completely_different(self):
        assert normalised_distance(("a", "b"), ("x", "y")) == 1.0

    def test_order_matters(self):
        """Two workflows using the same steps in a different order are different."""
        assert normalised_distance(("a", "b", "c"), ("c", "b", "a")) > 0.0

    def test_one_insertion_is_a_small_distance(self):
        assert 0 < normalised_distance(("a", "b", "c"), ("a", "b", "x", "c")) < 0.4

    def test_empty_inputs(self):
        assert normalised_distance((), ()) == 0.0
        assert normalised_distance(("a",), ()) == 1.0


class TestClustering:
    def test_similar_runs_group_together(self):
        actions = []
        for i in range(4):
            actions += run_actions(f"S{i}", ["A", "B", "C", "D"], start_minutes=i * 60)
        clusters = cluster_runs(segment_runs(actions))
        assert len(clusters) == 1
        assert len(clusters[0]) == 4

    def test_different_workflows_stay_apart(self):
        actions = run_actions("S1", ["A", "B", "C", "D"])
        actions += run_actions("S2", ["X", "Y", "Z", "W"], start_minutes=60)
        assert len(cluster_runs(segment_runs(actions))) == 2

    def test_a_run_with_one_extra_step_still_clusters(self):
        actions = run_actions("S1", ["A", "B", "C", "D", "E"])
        actions += run_actions("S2", ["A", "B", "C", "D", "E", "F"], start_minutes=60)
        assert len(cluster_runs(segment_runs(actions))) == 1


class TestDiscoveryOnDemoData:
    """The planted facts, as assertions. Each maps to a line in DEMO_EXPECTATIONS."""

    def test_the_order_workflow_is_discovered(self, order_workflow):
        assert order_workflow.observation_count >= 20
        assert len(order_workflow.steps) >= 12

    def test_the_expense_workflow_stays_separate(self, demo_candidates):
        assert len(demo_candidates) >= 2
        labels = {s.label for s in demo_candidates[1].steps}
        assert "Approve claim" in labels
        order_labels = {s.label for s in demo_candidates[0].steps}
        assert "Approve claim" not in order_labels

    def test_the_order_number_is_detected_as_a_variable(self, order_workflow):
        variables = {v.label for v in order_workflow.variables}
        assert "Search orders" in variables

    def test_a_variable_reports_its_shape_not_its_values(self, order_workflow):
        variable = next(v for v in order_workflow.variables if v.label == "Search orders")
        assert variable.value_shape.startswith("digits")
        assert variable.distinct_values > 5

    def test_the_signature_is_detected_as_a_constant(self, order_workflow):
        signature = next(s for s in order_workflow.steps if s.label == "Signature")
        assert signature.value_class == ValueClass.CONSTANT.value

    def test_the_optional_detour_is_kept_and_marked(self, order_workflow):
        history = next(s for s in order_workflow.steps if s.label == "Customer history")
        assert history.optional is True
        assert 0.1 < history.presence < 0.6

    def test_optional_steps_stay_in_the_order_the_user_performed_them(self, order_workflow):
        """Click the link, then read the page -- not the other way round."""
        click = next(i for i, s in enumerate(order_workflow.steps) if s.label == "Customer history")
        read = next(i for i, s in enumerate(order_workflow.steps) if s.label == "Previous orders")
        assert click < read

    def test_the_branch_is_reported_not_resolved(self, order_workflow):
        assert order_workflow.branches
        for branch in order_workflow.branches:
            assert len(branch.alternatives) >= 2
            assert abs(sum(a["share"] for a in branch.alternatives) - 1.0) < 0.01

    def test_navigation_paths_are_generalised(self, order_workflow):
        """A concrete order id would send every run to the same order."""
        paths = [s.target_path for s in order_workflow.steps if s.action == "navigate"]
        assert "/orders/{id}/detail" in paths
        assert not any(p and p.strip("/").split("/")[-2:-1] == [] and any(c.isdigit() for c in p)
                       for p in paths if "{id}" not in p)

    def test_confidence_is_capped_below_certainty(self, demo_candidates):
        for candidate in demo_candidates:
            assert 0.0 < candidate.confidence <= 0.92

    def test_discovery_is_deterministic(self, demo_actions):
        first = [c.fingerprint for c in discover_workflows(demo_actions)]
        second = [c.fingerprint for c in discover_workflows(demo_actions)]
        assert first == second

    def test_every_planted_expectation_has_a_test(self):
        assert len(DEMO_EXPECTATIONS) == 8

    def test_raising_the_repetition_threshold_drops_rare_workflows(self, demo_actions):
        strict = discover_workflows(demo_actions, MiningConfig(min_repetitions=20))
        assert len(strict) < len(discover_workflows(demo_actions))


class TestDSLValidation:
    def _workflow(self, **overrides):
        payload = {
            "name": "w",
            "allowed_origins": ["portal.demo.local"],
            "variables": [{"name": "order_id"}],
            "actions": [
                {
                    "kind": "input",
                    "selector": {"role": "textbox", "name": "Search"},
                    "variable": "order_id",
                    "origin": "portal.demo.local",
                }
            ],
        }
        payload.update(overrides)
        return payload

    def test_a_valid_workflow_parses(self):
        assert parse_workflow(self._workflow()).name == "w"

    def test_an_unknown_action_kind_is_refused(self):
        with pytest.raises(Exception):
            parse_workflow(self._workflow(actions=[{"kind": "shell", "value": "rm -rf /"}]))

    def test_there_is_no_script_or_eval_primitive(self):
        kinds = {k.value for k in ActionKind}
        assert not kinds & {"script", "eval", "shell", "exec", "http", "python"}

    def test_navigate_rejects_a_full_url(self):
        """A full URL would let a workflow leave its allowlisted origin."""
        with pytest.raises(Exception, match="path, not a full URL"):
            parse_workflow(
                self._workflow(actions=[{"kind": "navigate", "path": "https://elsewhere.com/x"}])
            )

    def test_an_action_referencing_an_undeclared_variable_is_refused(self):
        with pytest.raises(Exception, match="undeclared variable"):
            parse_workflow(self._workflow(variables=[]))

    def test_an_action_outside_the_allowed_origins_is_refused(self):
        with pytest.raises(Exception, match="allowed_origins"):
            parse_workflow(
                self._workflow(
                    actions=[
                        {
                            "kind": "click",
                            "selector": {"role": "button", "name": "Go"},
                            "origin": "elsewhere.example.com",
                        }
                    ]
                )
            )

    def test_a_click_needs_a_selector(self):
        with pytest.raises(Exception, match="requires a selector"):
            parse_workflow(self._workflow(actions=[{"kind": "click"}]))

    def test_an_api_call_needs_an_allowlisted_connector(self):
        issues = validate_workflow(
            self._workflow(
                actions=[{"kind": "api_call", "connector": "arbitrary_http"}],
                variables=[],
            ),
            allowed_connectors=["crm"],
        )
        assert any(i.severity == "error" and "not allowlisted" in i.message for i in issues)

    def test_an_allowlisted_connector_passes(self):
        issues = validate_workflow(
            self._workflow(actions=[{"kind": "api_call", "connector": "crm"}], variables=[]),
            allowed_connectors=["crm"],
        )
        assert not any(i.severity == "error" for i in issues)

    def test_unused_variables_are_warned_about(self):
        issues = validate_workflow(
            self._workflow(variables=[{"name": "order_id"}, {"name": "unused"}]),
            allowed_connectors=[],
        )
        assert any("never used" in i.message for i in issues)


class TestRiskClassification:
    def _click(self, name: str) -> Action:
        return Action(kind=ActionKind.CLICK, selector=Selector(role="button", name=name))

    @pytest.mark.parametrize(
        "name", ["Send", "Submit order", "Delete record", "Issue refund", "Confirm payment"]
    )
    def test_externally_visible_clicks_are_high_risk(self, name):
        assert self._click(name).risk is RiskLevel.HIGH

    @pytest.mark.parametrize("name", ["Search", "Open", "Next page", "Filter"])
    def test_ordinary_clicks_are_medium_risk(self, name):
        assert self._click(name).risk is RiskLevel.MEDIUM

    def test_reads_are_low_risk(self):
        assert Action(
            kind=ActionKind.READ_TEXT, selector=Selector(role="text", name="Status")
        ).risk is RiskLevel.LOW

    def test_an_api_call_is_always_high_risk(self):
        assert Action(kind=ActionKind.API_CALL, connector="crm").risk is RiskLevel.HIGH

    def test_risk_cannot_be_declared_by_the_workflow(self):
        """The author cannot mark a send as low risk."""
        assert "risk" not in Action.model_fields

    def test_an_api_call_always_requires_approval_even_if_pre_authorised(self):
        workflow = Workflow(
            name="w",
            actions=[Action(kind=ActionKind.API_CALL, connector="crm")],
        )
        assert workflow.requires_approval(approve_high_risk=True) is True


class TestCompilation:
    def test_the_workflow_compiles_and_validates(self, order_workflow):
        compiled = compile_candidate(order_workflow, name="Order lookup")
        issues = validate_workflow(compiled.workflow.model_dump(mode="json"))
        assert not [i for i in issues if i.severity == "error"]

    def test_a_branch_becomes_a_human_decision(self, order_workflow):
        compiled = compile_candidate(order_workflow, name="Order lookup")
        approvals = [a for a in compiled.workflow.actions if a.kind is ActionKind.APPROVAL]
        assert any("diverged" in a.label for a in approvals)

    def test_the_final_send_is_preceded_by_a_draft_and_an_approval(self, order_workflow):
        actions = compile_candidate(order_workflow, name="Order lookup").workflow.actions
        assert actions[-1].risk is RiskLevel.HIGH
        assert actions[-2].kind is ActionKind.APPROVAL
        assert actions[-3].kind is ActionKind.CREATE_DRAFT

    def test_variables_are_declared_for_every_input(self, order_workflow):
        workflow = compile_candidate(order_workflow, name="Order lookup").workflow
        declared = {v.name for v in workflow.variables}
        for act in workflow.actions:
            if act.kind is ActionKind.INPUT:
                assert act.variable in declared

    def test_a_sensitive_step_compiles_to_a_human_hand_off(self):
        from app.mining.discovery import WorkflowCandidate, WorkflowStep

        candidate = WorkflowCandidate(
            fingerprint="f",
            steps=[
                WorkflowStep(
                    position=0,
                    signature="s",
                    action="input",
                    origin="portal.demo.local",
                    role="textbox",
                    label="Password",
                    field_name="password",
                    value_class=ValueClass.SENSITIVE.value,
                    requires_human=True,
                )
            ],
            observation_count=5,
        )
        actions = compile_candidate(candidate, name="w").workflow.actions
        assert actions[0].kind is ActionKind.APPROVAL
        assert not any(a.kind is ActionKind.INPUT for a in actions)

    def test_notes_explain_every_judgement(self, order_workflow):
        compiled = compile_candidate(order_workflow, name="Order lookup")
        assert compiled.notes
        assert any("optional" in n.message for n in compiled.notes)


class TestExecution:
    @pytest.fixture()
    def workflow(self, order_workflow):
        return compile_candidate(order_workflow, name="Order lookup").workflow

    @pytest.fixture()
    def values(self, workflow):
        return {v.name: "10482" for v in workflow.variables}

    def test_a_dry_run_touches_nothing(self, workflow, values):
        """Not 'a run with a flag': the mutating branches are never reached."""
        portal = demo_portal()
        result = execute(workflow, values, SimulatedDriver(portal), RunOptions(dry_run=True))
        assert result.status is RunStatus.DRY_RUN
        assert portal.mutations == []
        assert portal.drafts == []
        assert portal.filled == {}
        assert all(s.status is StepStatus.SIMULATED for s in result.steps)

    def test_a_live_run_stops_at_the_first_approval(self, workflow, values):
        portal = demo_portal()
        result = execute(
            workflow, values, SimulatedDriver(portal),
            RunOptions(dry_run=False, autonomous_medium_risk=True),
        )
        assert result.status is RunStatus.AWAITING_APPROVAL
        assert result.paused_at is not None
        assert not any(m.get("name") == "Send" for m in portal.mutations)

    def test_approving_the_branches_still_stops_before_the_send(self, workflow, values):
        portal = demo_portal()
        approvals = tuple(
            i for i, a in enumerate(workflow.actions) if a.kind is ActionKind.APPROVAL
        )
        result = execute(
            workflow, values, SimulatedDriver(portal),
            RunOptions(dry_run=False, autonomous_medium_risk=True, approved_steps=approvals),
        )
        assert result.status is RunStatus.AWAITING_APPROVAL
        assert "high-risk" in (result.pending_approval or "")
        assert portal.drafts, "the work should be prepared before the human is asked"
        assert not any(m.get("name") == "Send" for m in portal.mutations)

    def test_a_fully_approved_run_completes(self, workflow, values):
        portal = demo_portal()
        approved = tuple(range(len(workflow.actions)))
        result = execute(
            workflow, values, SimulatedDriver(portal),
            RunOptions(dry_run=False, autonomous_medium_risk=True, approved_steps=approved),
        )
        assert result.status is RunStatus.COMPLETED
        assert any(m.get("name") == "Send" for m in portal.mutations)

    def test_medium_risk_requires_approval_unless_pre_authorised(self, workflow, values):
        portal = demo_portal()
        result = execute(
            workflow, values, SimulatedDriver(portal),
            RunOptions(dry_run=False, autonomous_medium_risk=False),
        )
        assert result.status is RunStatus.AWAITING_APPROVAL
        assert result.paused_at is not None

    def test_step_logs_never_contain_a_value(self, workflow, values):
        portal = demo_portal()
        result = execute(
            workflow, values, SimulatedDriver(portal),
            RunOptions(dry_run=False, autonomous_medium_risk=True,
                       approved_steps=tuple(range(len(workflow.actions)))),
        )
        assert "10482" not in str([s.as_dict() for s in result.steps])
        inputs = [s for s in result.steps if s.kind == "input"]
        assert inputs and all("shape" in s.input_summary for s in inputs)

    def test_a_missing_required_variable_fails_before_touching_anything(self, workflow):
        portal = demo_portal()
        result = execute(workflow, {}, SimulatedDriver(portal), RunOptions(dry_run=False))
        assert result.status is RunStatus.FAILED
        assert "missing required variable" in (result.error or "")
        assert portal.mutations == []

    def test_an_action_outside_the_allowlist_is_blocked_at_run_time(self):
        """Validation is not enough: a stored workflow may have been edited."""
        workflow = Workflow(
            name="w",
            allowed_origins=["portal.demo.local"],
            actions=[
                Action(kind=ActionKind.NAVIGATE, path="/orders", origin="portal.demo.local"),
                Action(kind=ActionKind.READ_TEXT, origin="portal.demo.local",
                       selector=Selector(role="text", name="Payment status")),
            ],
        )
        # Edit the stored document after validation, as an attacker or a bug would.
        workflow.actions[1].origin = "elsewhere.example.com"
        portal = demo_portal()
        result = execute(workflow, {}, SimulatedDriver(portal), RunOptions(dry_run=False))
        assert result.status is RunStatus.FAILED
        assert result.steps[-1].status is StepStatus.BLOCKED

    def test_an_api_call_to_a_non_allowlisted_connector_is_blocked(self):
        workflow = Workflow(
            name="w", actions=[Action(kind=ActionKind.API_CALL, connector="arbitrary")]
        )
        result = execute(
            workflow, {}, SimulatedDriver(demo_portal()),
            RunOptions(dry_run=False, approved_steps=(0,), allowed_connectors=("crm",)),
        )
        assert result.status is RunStatus.FAILED
        assert "not allowlisted" in (result.steps[0].error or "")

    def test_a_failing_optional_step_does_not_stop_the_run(self):
        workflow = Workflow(
            name="w",
            allowed_origins=["portal.demo.local"],
            actions=[
                Action(kind=ActionKind.NAVIGATE, path="/orders", origin="portal.demo.local"),
                Action(kind=ActionKind.READ_TEXT, origin="portal.demo.local", optional=True,
                       selector=Selector(role="text", name="Nonexistent")),
                Action(kind=ActionKind.READ_TEXT, origin="portal.demo.local",
                       selector=Selector(role="text", name="Search orders")),
            ],
        )
        result = execute(
            workflow, {}, SimulatedDriver(demo_portal()),
            RunOptions(dry_run=False, autonomous_medium_risk=True),
        )
        assert result.steps[1].status is StepStatus.FAILED
        assert len(result.steps) == 3, "the run should have continued past the optional failure"
