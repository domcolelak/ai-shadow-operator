"""Tests for the capture layer.

These are the privacy guarantees. If any of them fails, the product is
surveillance software, so they are written as assertions about what must *not*
exist in stored data rather than about return values.
"""
from __future__ import annotations

import pytest

from app.capture.model import (
    ActionType,
    CapturePolicy,
    CaptureRejected,
    ElementRef,
    ValueClass,
    _generalise_path,
    capture_action,
    capture_batch,
    redact,
    summarise_capture,
    value_hash,
    value_shape,
)

POLICY = CapturePolicy(allowed_origins=("portal.demo.local", "mail.demo.local"))


def raw(action="click", *, origin="https://portal.demo.local", value=None, **element):
    payload = {
        "action": action,
        "origin": origin,
        "timestamp": "2026-04-06T08:30:00+00:00",
        "page_title": "Orders",
        "element": {
            "role": element.pop("role", "button"),
            "accessible_name": element.pop("name", "Search"),
            "field_name": element.pop("field_name", ""),
            "input_type": element.pop("input_type", ""),
            "autocomplete": element.pop("autocomplete", ""),
        },
    }
    payload.update(element)
    if value is not None:
        payload["value"] = value
    return payload


def capture(payload, *, policy=POLICY):
    return capture_action(payload, policy=policy, session_id="S1", sequence=0, salt="salt")


class TestOriginAllowlist:
    def test_allowlisted_origin_is_captured(self):
        assert capture(raw()).origin == "portal.demo.local"

    def test_subdomains_of_an_allowlisted_host_are_allowed(self):
        policy = CapturePolicy(allowed_origins=("demo.local",))
        assert capture(raw(origin="https://portal.demo.local"), policy=policy)

    def test_a_non_allowlisted_origin_is_refused(self):
        with pytest.raises(CaptureRejected, match="allowlist"):
            capture(raw(origin="https://personal-banking.example.com"))

    def test_an_empty_allowlist_records_nothing(self):
        """The safe default is 'nothing', not 'everything'."""
        with pytest.raises(CaptureRejected):
            capture(raw(), policy=CapturePolicy())

    def test_a_blocked_origin_wins_over_the_allowlist(self):
        policy = CapturePolicy(
            allowed_origins=("demo.local",), blocked_origins=("payroll.demo.local",)
        )
        with pytest.raises(CaptureRejected):
            capture(raw(origin="https://payroll.demo.local"), policy=policy)

    def test_a_missing_origin_is_refused(self):
        with pytest.raises(CaptureRejected):
            capture(raw(origin=""))


class TestSensitiveFields:
    def test_a_password_field_yields_no_value_at_all(self):
        action = capture(
            raw(
                "input",
                value="hunter2",
                role="textbox",
                name="Password",
                field_name="password",
                input_type="password",
            )
        )
        assert action.value_class is ValueClass.SENSITIVE
        assert action.value_hash is None, "a hash of a password is still a password oracle"
        assert action.value_shape == ""
        assert "hunter2" not in str(action.as_dict())

    @pytest.mark.parametrize(
        "field_name",
        ["password", "card_number", "cvv", "iban", "api_key", "otp_code", "national_id"],
    )
    def test_sensitive_field_names_are_recognised(self, field_name):
        action = capture(
            raw("input", value="secret", role="textbox", name=field_name, field_name=field_name)
        )
        assert action.value_class is ValueClass.SENSITIVE
        assert action.value_hash is None

    def test_sensitive_autocomplete_is_recognised(self):
        action = capture(
            raw(
                "input",
                value="123456",
                role="textbox",
                name="Code",
                field_name="code",
                autocomplete="one-time-code",
            )
        )
        assert action.value_class is ValueClass.SENSITIVE

    def test_a_tenant_can_add_its_own_sensitive_fields(self):
        policy = CapturePolicy(
            allowed_origins=("portal.demo.local",), extra_sensitive_fields=("patient",)
        )
        action = capture(
            raw("input", value="x", role="textbox", name="Patient ref", field_name="patient_ref"),
            policy=policy,
        )
        assert action.value_class is ValueClass.SENSITIVE

    def test_the_step_is_still_recorded_so_the_workflow_makes_sense(self):
        action = capture(
            raw("input", value="x", role="textbox", name="Password", field_name="password")
        )
        assert action.action is ActionType.INPUT
        assert action.element.accessible_name == "Password"


class TestKeystrokes:
    def test_keystroke_events_are_refused_outright(self):
        with pytest.raises(CaptureRejected, match="never recorded"):
            capture(raw("keypress", value="h"))

    def test_a_batch_drops_keystrokes_and_keeps_the_rest(self):
        result = capture_batch(
            [raw("click"), raw("keypress", value="a"), raw("click")],
            policy=POLICY,
            session_id="S1",
            salt="salt",
        )
        assert result.accepted_count == 2
        assert len(result.rejected) == 1
        assert "keystroke" in result.rejected[0]["reason"]


class TestValueHandling:
    def test_ordinary_values_are_hashed_not_stored(self):
        action = capture(
            raw("input", value="ORDER-10482", role="textbox", name="Search", field_name="order_id")
        )
        assert action.value_hash
        assert "ORDER-10482" not in str(action.as_dict())
        assert action.value_class is ValueClass.UNKNOWN

    def test_the_same_value_hashes_alike_within_a_salt(self):
        """This is what makes constant-versus-variable detection possible."""
        assert value_hash("abc", "s") == value_hash("abc", "s")
        assert value_hash("abc", "s") != value_hash("abd", "s")

    def test_a_different_salt_gives_a_different_hash(self):
        assert value_hash("abc", "s1") != value_hash("abc", "s2")

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("12345", "digits:5-10"),
            ("a@b.com", "email:5-10"),
            ("abcd", "alpha:1-4"),
            ("", "empty"),
        ],
    )
    def test_value_shape_describes_without_revealing(self, value, expected):
        assert value_shape(value) == expected


class TestPathHandling:
    def test_query_strings_are_dropped(self):
        action = capture(
            raw("navigate", target="https://portal.demo.local/orders?customer=jane&token=abc")
        )
        assert "token" not in action.target_path
        assert "jane" not in action.target_path

    def test_identifier_segments_are_generalised(self):
        assert _generalise_path("/orders/10482/detail") == "/orders/{id}/detail"
        assert _generalise_path("/orders/detail") == "/orders/detail"

    def test_two_runs_on_different_orders_share_a_step_signature(self):
        """Without this, no two runs ever look like the same workflow."""
        first = capture(raw("navigate", target="https://portal.demo.local/orders/10482/detail"))
        second = capture(raw("navigate", target="https://portal.demo.local/orders/99999/detail"))
        assert first.step_signature() == second.step_signature()


class TestRedaction:
    def test_emails_and_long_numbers_are_stripped_from_free_text(self):
        assert "@" not in redact("write to jane@example.com")
        assert "123456789012" not in redact("card 123456789012")

    def test_accessible_names_are_redacted(self):
        action = capture(raw(name="Reply to jane@example.com"))
        assert "jane@example.com" not in action.element.accessible_name


class TestScreenshots:
    def test_screenshots_are_off_by_default(self):
        assert CapturePolicy().capture_screenshots is False

    def test_the_summary_states_what_was_not_kept(self):
        summary = summarise_capture([capture(raw())])
        assert summary["values_stored"] is False
        assert summary["screenshots_stored"] is False


class TestElementRef:
    def test_signature_ignores_values_but_not_identity(self):
        left = ElementRef(role="textbox", accessible_name="Search", field_name="order_id")
        right = ElementRef(role="textbox", accessible_name="Search", field_name="order_id")
        other = ElementRef(role="textbox", accessible_name="Filter", field_name="order_id")
        assert left.signature() == right.signature()
        assert left.signature() != other.signature()

    def test_password_input_type_is_sensitive_whatever_it_is_called(self):
        assert ElementRef(role="textbox", accessible_name="Code", input_type="password").is_sensitive


class TestBatchCapture:
    def test_rejections_record_a_reason_and_nothing_else(self):
        result = capture_batch(
            [raw(origin="https://elsewhere.example.com", value="secret")],
            policy=POLICY,
            session_id="S1",
            salt="salt",
        )
        assert result.accepted_count == 0
        assert result.rejected[0]["reason"]
        assert "secret" not in str(result.rejected)

    def test_sequence_numbers_only_advance_for_accepted_events(self):
        result = capture_batch(
            [raw("click"), raw(origin="https://nope.example.com"), raw("click")],
            policy=POLICY,
            session_id="S1",
            salt="salt",
        )
        assert [a.sequence for a in result.actions] == [0, 1]
