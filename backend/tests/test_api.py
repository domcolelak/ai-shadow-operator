"""End-to-end API tests: consent, recording, discovery, approval and isolation."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.security import hash_api_key
from app.demo.seed import DEMO_API_KEY
from app.models import RecordedAction, RecordingSession, Tenant, WorkflowCandidateRow


def order_candidate(client) -> dict:
    """The demo's order-lookup workflow, identified by its final 'Send' step.

    Not `candidates[0]`: other tests record their own sessions, so a later
    discovery run can legitimately rank a different workflow first. The
    execution tests are specifically about the one that ends in an externally
    visible action.
    """
    for _ in range(2):
        for candidate in client.get("/v1/workflow-candidates").json():
            if any(step["label"] == "Send" for step in candidate["steps"]):
                return candidate
        client.post("/v1/discovery-runs", json={})
    raise AssertionError("the order-lookup workflow was not discovered")


def top_candidate(client) -> dict:
    """The most-repeated open candidate.

    Accepting or rejecting a candidate removes it from the pool, and the demo
    corpus only yields a couple, so a test that runs after those would find an
    empty list. Re-running discovery restores the open candidates -- which is
    also what a real workspace does after more recording.
    """
    candidates = client.get("/v1/workflow-candidates").json()
    if not candidates:
        client.post("/v1/discovery-runs", json={})
        candidates = client.get("/v1/workflow-candidates").json()
    assert candidates, "the demo should have discovered candidates"
    return candidates[0]


class TestHealthAndOverview:
    def test_health(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_openapi(self, client):
        assert client.get("/openapi.json").status_code == 200

    def test_overview(self, client):
        body = client.get("/v1/overview").json()
        assert body["session_count"] > 0
        assert body["action_count"] > 0
        assert body["rejected_count"] > 0, "the demo includes events that must be refused"
        assert body["candidate_count"] > 0
        assert body["consent"]["values_stored"] is False


class TestConsent:
    def test_consent_states_what_is_not_captured(self, client):
        body = client.get("/v1/consent").json()
        assert body["allowed_origins"]
        assert body["screenshots_captured"] is False
        assert body["keystrokes_captured"] is False
        assert body["values_stored"] is False
        assert "sensitive" in body["note"]

    def test_consent_can_be_narrowed(self, client):
        original = client.get("/v1/consent").json()["allowed_origins"]
        updated = client.patch(
            "/v1/consent", json={"blocked_origins": ["payroll.demo.local"]}
        ).json()
        assert "payroll.demo.local" in updated["blocked_origins"]
        client.patch("/v1/consent", json={"allowed_origins": original, "blocked_origins": []})


class TestRecording:
    def _start(self, client, external_id: str) -> str:
        response = client.post(
            "/v1/sessions", json={"external_id": external_id, "device": "ws-1"}
        )
        assert response.status_code == 201
        return response.json()["id"]

    def _event(self, **overrides) -> dict:
        payload = {
            "action": "click",
            "origin": "https://portal.demo.local",
            "timestamp": "2026-04-06T09:00:00+00:00",
            "page_title": "Orders",
            "element": {"role": "button", "accessible_name": "Search"},
        }
        payload.update(overrides)
        return payload

    def test_a_session_must_be_started_before_anything_is_recorded(self, client):
        response = client.post(
            f"/v1/sessions/{uuid.uuid4()}/events", json={"events": [self._event()]}
        )
        assert response.status_code == 404

    def test_events_are_accepted_and_counted(self, client):
        session_id = self._start(client, f"S-{uuid.uuid4().hex[:8]}")
        body = client.post(
            f"/v1/sessions/{session_id}/events", json={"events": [self._event()] * 3}
        ).json()
        assert body["accepted"] == 3
        assert body["rejected"] == 0

    def test_a_non_allowlisted_origin_is_refused_with_a_reason(self, client):
        session_id = self._start(client, f"S-{uuid.uuid4().hex[:8]}")
        body = client.post(
            f"/v1/sessions/{session_id}/events",
            json={"events": [self._event(origin="https://personal.example.com")]},
        ).json()
        assert body["accepted"] == 0
        assert body["rejected"] == 1
        assert "allowlist" in body["rejection_reasons"][0]

    def test_a_password_leaves_no_stored_value(self, client, db):
        session_id = self._start(client, f"S-{uuid.uuid4().hex[:8]}")
        client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "events": [
                    self._event(
                        action="input",
                        value="hunter2-should-never-be-stored",
                        element={
                            "role": "textbox",
                            "accessible_name": "Password",
                            "field_name": "password",
                            "input_type": "password",
                        },
                    )
                ]
            },
        )
        db.expire_all()
        rows = db.scalars(
            select(RecordedAction).where(RecordedAction.session_id == uuid.UUID(session_id))
        ).all()
        assert rows
        assert all(r.value_hash is None for r in rows)
        assert not any("hunter2" in str(r.__dict__) for r in rows)

    def test_keystrokes_are_refused(self, client):
        session_id = self._start(client, f"S-{uuid.uuid4().hex[:8]}")
        body = client.post(
            f"/v1/sessions/{session_id}/events",
            json={"events": [self._event(action="keypress", value="h")]},
        ).json()
        assert body["accepted"] == 0
        assert "keystroke" in body["rejection_reasons"][0]

    def test_a_completed_session_stops_accepting_events(self, client):
        session_id = self._start(client, f"S-{uuid.uuid4().hex[:8]}")
        client.post(f"/v1/sessions/{session_id}/complete")
        response = client.post(
            f"/v1/sessions/{session_id}/events", json={"events": [self._event()]}
        )
        assert response.status_code == 409

    def test_the_summary_reports_what_was_kept(self, client):
        session_id = self._start(client, f"S-{uuid.uuid4().hex[:8]}")
        client.post(f"/v1/sessions/{session_id}/events", json={"events": [self._event()] * 2})
        summary = client.get(f"/v1/sessions/{session_id}/summary").json()
        assert summary["action_count"] == 2
        assert summary["values_stored"] is False
        assert summary["screenshots_stored"] is False

    def test_deleting_a_session_removes_every_action(self, client, db):
        session_id = self._start(client, f"S-{uuid.uuid4().hex[:8]}")
        client.post(f"/v1/sessions/{session_id}/events", json={"events": [self._event()] * 4})

        body = client.delete(f"/v1/sessions/{session_id}").json()
        assert body["deleted"] is True
        assert body["actions_removed"] == 4

        db.expire_all()
        assert (
            db.scalars(
                select(RecordedAction).where(
                    RecordedAction.session_id == uuid.UUID(session_id)
                )
            ).all()
            == []
        )
        assert db.get(RecordingSession, uuid.UUID(session_id)) is None

    def test_duplicate_external_id_is_refused(self, client):
        identifier = f"S-{uuid.uuid4().hex[:8]}"
        self._start(client, identifier)
        assert client.post("/v1/sessions", json={"external_id": identifier}).status_code == 409


class TestDiscoveryApi:
    def test_candidates_are_ranked_by_repetition(self, client):
        candidates = client.get("/v1/workflow-candidates").json()
        counts = [c["observation_count"] for c in candidates]
        assert counts == sorted(counts, reverse=True)

    def test_a_candidate_carries_its_evidence(self, client):
        candidate = top_candidate(client)
        assert candidate["observation_count"] >= 3
        assert candidate["steps"]
        assert candidate["session_ids"]
        assert 0 < candidate["confidence"] <= 0.92

    def test_a_rerun_replaces_rather_than_duplicates(self, client):
        client.post("/v1/discovery-runs", json={})
        first = client.get("/v1/workflow-candidates").json()
        client.post("/v1/discovery-runs", json={})
        second = client.get("/v1/workflow-candidates").json()

        assert len(second) == len(first)
        fingerprints = [c["fingerprint"] for c in second]
        assert len(fingerprints) == len(set(fingerprints)), "a rerun must not duplicate"

    def test_a_constant_is_detected_across_sessions(self, client, db):
        """The bug this guards: a per-session salt made every constant look
        like a variable, because the same value hashed differently in each
        recording."""
        candidate = order_candidate(client)
        signature = next(
            (s for s in candidate["steps"] if s["label"] == "Signature"), None
        )
        assert signature is not None, "the demo workflow includes a fixed signature"
        assert signature["value_class"] == "constant", (
            "a value typed identically in every run must not read as a variable"
        )

    def test_the_hashing_salt_is_shared_across_sessions(self, db):
        sessions = db.scalars(select(RecordingSession)).all()
        assert len(sessions) > 1
        assert len({s.salt for s in sessions}) == 1

    def test_describe_uses_the_offline_provider(self, client):
        candidate = top_candidate(client)
        body = client.post(f"/v1/workflow-candidates/{candidate['id']}/describe").json()
        assert body["narrative"]["name"]
        assert body["narrative"]["summary"]

    def test_reject_marks_the_candidate(self, client):
        top_candidate(client)  # ensure the pool is populated
        candidates = client.get("/v1/workflow-candidates").json()
        target = candidates[-1]
        assert (
            client.post(f"/v1/workflow-candidates/{target['id']}/reject").json()["status"]
            == "rejected"
        )


class TestAcceptanceAndSafety:
    def _accept(self, client) -> dict:
        candidate = top_candidate(client)
        response = client.post(
            f"/v1/workflow-candidates/{candidate['id']}/accept",
            json={"name": "Answer order status enquiry"},
        )
        assert response.status_code == 200
        return response.json()

    def test_an_accepted_workflow_starts_as_a_draft(self, client):
        workflow = self._accept(client)
        assert workflow["state"] == "draft", "acceptance is a review step, not a deployment"

    def test_the_compiled_workflow_validates(self, client):
        workflow = self._accept(client)
        assert not [i for i in workflow["validation_issues"] if i["severity"] == "error"]

    def test_high_risk_steps_are_listed_for_the_reviewer(self, client):
        workflow = self._accept(client)
        assert workflow["requires_approval"] is True
        assert workflow["high_risk_steps"]

    def test_compilation_notes_explain_the_judgements(self, client):
        workflow = self._accept(client)
        assert workflow["compilation_notes"]

    def test_a_draft_workflow_cannot_run_live(self, client):
        workflow = self._accept(client)
        response = client.post(
            f"/v1/workflows/{workflow['id']}/run", json={"dry_run": False}
        )
        assert response.status_code == 409
        assert "enabled" in response.json()["detail"]

    def test_a_draft_workflow_can_be_dry_run(self, client):
        workflow = self._accept(client)
        body = client.post(
            f"/v1/workflows/{workflow['id']}/run", json={"dry_run": True}
        ).json()
        assert body["status"] == "dry_run"
        assert body["steps"]

    def test_editing_an_enabled_workflow_returns_it_to_draft(self, client):
        workflow = self._accept(client)
        client.post(
            f"/v1/workflows/{workflow['id']}/state",
            json={"state": "enabled", "autonomous_medium_risk": True},
        )
        edited = client.put(
            f"/v1/workflows/{workflow['id']}", json={"definition": workflow["definition"]}
        ).json()
        assert edited["state"] == "draft", "an edit invalidates the earlier approval"

    def test_an_invalid_edit_is_refused(self, client):
        workflow = self._accept(client)
        response = client.put(
            f"/v1/workflows/{workflow['id']}",
            json={"definition": {"name": "x", "actions": [{"kind": "shell"}]}},
        )
        assert response.status_code == 422


class TestExecutionApi:
    def _enabled(self, client) -> dict:
        candidate = order_candidate(client)
        workflow = client.post(
            f"/v1/workflow-candidates/{candidate['id']}/accept", json={}
        ).json()
        client.post(
            f"/v1/workflows/{workflow['id']}/state",
            json={"state": "enabled", "autonomous_medium_risk": True},
        )
        return client.get(f"/v1/workflows/{workflow['id']}").json()

    def _values(self, workflow) -> dict:
        return {v["name"]: "10482" for v in workflow["definition"]["variables"]}

    def test_a_live_run_pauses_for_approval(self, client):
        workflow = self._enabled(client)
        body = client.post(
            f"/v1/workflows/{workflow['id']}/run",
            json={"dry_run": False, "variables": self._values(workflow)},
        ).json()
        assert body["status"] == "awaiting_approval", (
            f"error={body.get('error')} steps={[(x['index'], x['kind'], x['status'], x.get('error')) for x in body['steps'][-3:]]}"
        )
        assert body["paused_at"] is not None
        assert body["pending_approval"]

    def test_stored_executions_never_hold_a_value(self, client):
        workflow = self._enabled(client)
        body = client.post(
            f"/v1/workflows/{workflow['id']}/run",
            json={"dry_run": False, "variables": self._values(workflow)},
        ).json()
        assert "10482" not in str(body)
        assert all(isinstance(v, str) for v in body["supplied_variables"])

    def test_approving_advances_the_run(self, client):
        workflow = self._enabled(client)
        values = self._values(workflow)
        run = client.post(
            f"/v1/workflows/{workflow['id']}/run",
            json={"dry_run": False, "variables": values},
        ).json()

        approved = client.post(
            f"/v1/executions/{run['id']}/approve",
            json={
                "step_index": run["paused_at"],
                "decision": "approve",
                "approved_by": "tester",
                "variables": values,
            },
        ).json()
        assert approved["paused_at"] != run["paused_at"] or approved["status"] != "awaiting_approval"

    def test_rejecting_stops_the_run(self, client):
        workflow = self._enabled(client)
        run = client.post(
            f"/v1/workflows/{workflow['id']}/run",
            json={"dry_run": False, "variables": self._values(workflow)},
        ).json()
        rejected = client.post(
            f"/v1/executions/{run['id']}/approve",
            json={"step_index": run["paused_at"], "decision": "reject", "reason": "not now"},
        ).json()
        assert rejected["status"] == "rejected"

    def test_approving_the_wrong_step_is_refused(self, client):
        workflow = self._enabled(client)
        run = client.post(
            f"/v1/workflows/{workflow['id']}/run",
            json={"dry_run": False, "variables": self._values(workflow)},
        ).json()
        response = client.post(
            f"/v1/executions/{run['id']}/approve",
            json={"step_index": run["paused_at"] + 5, "decision": "approve"},
        )
        assert response.status_code == 409

    def test_approving_a_finished_run_is_refused(self, client):
        workflow = self._enabled(client)
        run = client.post(
            f"/v1/workflows/{workflow['id']}/run", json={"dry_run": True}
        ).json()
        response = client.post(
            f"/v1/executions/{run['id']}/approve", json={"step_index": 0, "decision": "approve"}
        )
        assert response.status_code == 409

    def test_explain_uses_the_offline_provider(self, client):
        workflow = self._enabled(client)
        run = client.post(f"/v1/workflows/{workflow['id']}/run", json={"dry_run": True}).json()
        body = client.post(f"/v1/executions/{run['id']}/explain").json()
        assert body["narrative"]["headline"]


class TestTenantIsolation:
    def test_other_tenant_sees_nothing(self, client, db):
        db.add(
            Tenant(
                slug="other",
                name="Other",
                api_key_hash=hash_api_key("pk_other_key"),
                allowed_origins=["portal.demo.local"],
            )
        )
        db.commit()
        headers = {"X-API-Key": "pk_other_key"}
        assert client.get("/v1/sessions", headers=headers).json() == []
        assert client.get("/v1/workflow-candidates", headers=headers).json() == []
        assert client.get("/v1/overview", headers=headers).json()["action_count"] == 0

    def test_cross_tenant_access_is_404(self, client, db):
        db.add(
            Tenant(
                slug="snoop",
                name="Snoop",
                api_key_hash=hash_api_key("pk_snoop_key"),
                allowed_origins=["portal.demo.local"],
            )
        )
        db.commit()
        headers = {"X-API-Key": "pk_snoop_key"}
        session = db.scalar(select(RecordingSession))
        candidate = db.scalar(select(WorkflowCandidateRow))
        assert client.get(f"/v1/sessions/{session.id}/summary", headers=headers).status_code == 404
        assert (
            client.get(f"/v1/workflow-candidates/{candidate.id}", headers=headers).status_code
            == 404
        )

    def test_cross_tenant_deletion_is_refused(self, client, db):
        db.add(
            Tenant(
                slug="deleter",
                name="Deleter",
                api_key_hash=hash_api_key("pk_deleter_key"),
                allowed_origins=["portal.demo.local"],
            )
        )
        db.commit()
        session = db.scalar(select(RecordingSession))
        assert (
            client.delete(
                f"/v1/sessions/{session.id}", headers={"X-API-Key": "pk_deleter_key"}
            ).status_code
            == 404
        )

    def test_a_tenant_with_no_allowlist_cannot_record(self, client, db):
        db.add(
            Tenant(
                slug="noconsent",
                name="No consent",
                api_key_hash=hash_api_key("pk_noconsent_key"),
                allowed_origins=[],
            )
        )
        db.commit()
        response = client.post(
            "/v1/sessions",
            json={"external_id": "X-1"},
            headers={"X-API-Key": "pk_noconsent_key"},
        )
        assert response.status_code == 409
        assert "consent" in response.json()["detail"]

    def test_invalid_key_is_401(self, client):
        assert client.get("/v1/sessions", headers={"X-API-Key": "pk_nope"}).status_code == 401

    def test_demo_key_works(self, client):
        assert client.get("/v1/sessions", headers={"X-API-Key": DEMO_API_KEY}).status_code == 200

    def test_all_actions_belong_to_the_demo_tenant(self, db):
        demo = db.scalar(select(Tenant).where(Tenant.slug == "demo"))
        actions = db.scalars(select(RecordedAction)).all()
        assert actions
        assert all(a.tenant_id == demo.id for a in actions)
