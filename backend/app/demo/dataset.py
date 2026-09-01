"""Deterministic demo: recorded sessions in a fake order-management portal.

Generates raw browser events of the kind a consented recorder would emit, for
the workflow described in the brief:

    open a support email -> copy the order number -> look up the order ->
    check payment -> check shipping -> pick a reply template -> edit it -> send

Planted so the miner has something real to find:

* the same workflow repeated ~28 times with different order numbers,
* a branch: unpaid orders take a different reply path,
* an occasional extra step (checking the customer's history) that appears in
  only some runs, so optional-step detection has something to detect,
* a handful of unrelated sessions (expense approvals) that must **not** be
  merged into the main workflow,
* a password field and a raw keystroke event that must never be stored,
* events from a non-allowlisted origin that must be rejected at the door.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

PORTAL = "https://portal.demo.local"
MAILBOX = "https://mail.demo.local"
#: Deliberately not allowlisted -- used to prove the boundary holds.
PERSONAL = "https://personal-banking.example.com"

START = datetime(2026, 4, 6, 8, 30, tzinfo=timezone.utc)

ALLOWED_ORIGINS = ("portal.demo.local", "mail.demo.local")


def _event(
    action: str,
    *,
    origin: str,
    offset: float,
    page_title: str = "",
    role: str = "",
    name: str = "",
    field_name: str = "",
    input_type: str = "",
    value: str | None = None,
    target: str = "",
    base: datetime,
    autocomplete: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "origin": origin,
        "timestamp": (base + timedelta(seconds=offset)).isoformat(),
        "page_title": page_title,
        "element": {
            "role": role,
            "accessible_name": name,
            "field_name": field_name,
            "input_type": input_type,
            "autocomplete": autocomplete,
        },
    }
    if value is not None:
        payload["value"] = value
    if target:
        payload["target"] = target
    return payload


def _order_lookup_run(
    base: datetime, order_id: str, *, paid: bool, check_history: bool
) -> list[dict[str, Any]]:
    """One pass of the workflow the product is supposed to discover."""
    events: list[dict[str, Any]] = []
    clock = 0.0

    def step(**kwargs) -> None:
        nonlocal clock
        clock += kwargs.pop("gap", 4.0)
        events.append(_event(base=base, offset=clock, **kwargs))

    step(action="navigate", origin=MAILBOX, page_title="Inbox",
         target=f"{MAILBOX}/mail/inbox", gap=2.0)
    step(action="click", origin=MAILBOX, page_title="Inbox",
         role="listitem", name="Where is my order?")
    step(action="read", origin=MAILBOX, page_title="Message",
         role="text", name="Order number")

    step(action="navigate", origin=PORTAL, page_title="Orders",
         target=f"{PORTAL}/orders", gap=6.0)
    step(action="input", origin=PORTAL, page_title="Orders",
         role="textbox", name="Search orders", field_name="order_id", value=order_id)
    step(action="click", origin=PORTAL, page_title="Orders",
         role="button", name="Search")
    # The order id lands in the path; the generaliser turns it into {id} so
    # every run looks like the same step.
    step(action="navigate", origin=PORTAL, page_title="Order detail",
         target=f"{PORTAL}/orders/{order_id}/detail", gap=3.0)
    step(action="read", origin=PORTAL, page_title="Order detail",
         role="text", name="Payment status")
    step(action="read", origin=PORTAL, page_title="Order detail",
         role="text", name="Shipment status")

    if check_history:
        # Optional step: only some runs bother.
        step(action="click", origin=PORTAL, page_title="Order detail",
             role="link", name="Customer history")
        step(action="read", origin=PORTAL, page_title="Customer history",
             role="text", name="Previous orders")

    step(action="navigate", origin=MAILBOX, page_title="Message",
         target=f"{MAILBOX}/mail/message", gap=5.0)
    step(action="click", origin=MAILBOX, page_title="Message",
         role="button", name="Reply")

    # The branch: paid and unpaid orders take different templates.
    if paid:
        step(action="select", origin=MAILBOX, page_title="Reply",
             role="combobox", name="Template", field_name="template",
             value="shipping_confirmation")
        step(action="input", origin=MAILBOX, page_title="Reply",
             role="textbox", name="Tracking number", field_name="tracking_number",
             value=f"TRK{order_id[-5:]}")
    else:
        step(action="select", origin=MAILBOX, page_title="Reply",
             role="combobox", name="Template", field_name="template",
             value="payment_request")
        step(action="input", origin=MAILBOX, page_title="Reply",
             role="textbox", name="Amount due", field_name="amount_due", value="129.00")

    step(action="input", origin=MAILBOX, page_title="Reply",
         role="textbox", name="Signature", field_name="signature", value="Customer Care")
    step(action="click", origin=MAILBOX, page_title="Reply",
         role="button", name="Send", gap=3.0)
    return events


def _expense_run(base: datetime, claim_id: str) -> list[dict[str, Any]]:
    """An unrelated workflow. Must not be merged with the order one."""
    events: list[dict[str, Any]] = []
    clock = 0.0

    def step(**kwargs) -> None:
        nonlocal clock
        clock += kwargs.pop("gap", 5.0)
        events.append(_event(base=base, offset=clock, **kwargs))

    step(action="navigate", origin=PORTAL, page_title="Expenses",
         target=f"{PORTAL}/expenses", gap=2.0)
    step(action="input", origin=PORTAL, page_title="Expenses",
         role="textbox", name="Claim reference", field_name="claim_id", value=claim_id)
    step(action="click", origin=PORTAL, page_title="Expenses",
         role="button", name="Open claim")
    step(action="read", origin=PORTAL, page_title="Claim detail",
         role="text", name="Receipt total")
    step(action="click", origin=PORTAL, page_title="Claim detail",
         role="button", name="Approve claim")
    return events


def _login_events(base: datetime) -> list[dict[str, Any]]:
    """A login. The password and the keystrokes must never survive capture."""
    return [
        _event("navigate", origin=PORTAL, base=base, offset=0.0,
               page_title="Sign in", target=f"{PORTAL}/login"),
        _event("input", origin=PORTAL, base=base, offset=2.0, page_title="Sign in",
               role="textbox", name="Email", field_name="email",
               value="agent@demo.local"),
        _event("input", origin=PORTAL, base=base, offset=4.0, page_title="Sign in",
               role="textbox", name="Password", field_name="password",
               input_type="password", autocomplete="current-password",
               value="hunter2-should-never-be-stored"),
        _event("keypress", origin=PORTAL, base=base, offset=4.5, page_title="Sign in",
               role="textbox", name="Password", value="h"),
        _event("click", origin=PORTAL, base=base, offset=6.0, page_title="Sign in",
               role="button", name="Sign in"),
    ]


def _off_limits_events(base: datetime) -> list[dict[str, Any]]:
    """Activity on a personal site. Must be rejected: it is not allowlisted."""
    return [
        _event("navigate", origin=PERSONAL, base=base, offset=0.0,
               page_title="Personal banking", target=f"{PERSONAL}/accounts"),
        _event("read", origin=PERSONAL, base=base, offset=3.0,
               page_title="Personal banking", role="text", name="Balance"),
    ]


def generate_sessions(*, count: int = 30, seed: int = 20260830) -> list[dict[str, Any]]:
    """Build recorded sessions. Pure function, no database.

    Returns ``[{session_id, started_at, device, events: [...]}, ...]`` in the
    shape the capture API accepts.
    """
    rng = random.Random(seed)
    sessions: list[dict[str, Any]] = []
    base = START

    for index in range(count):
        session_id = f"SES-{100 + index}"
        events: list[dict[str, Any]] = []

        # Roughly every fifth session starts with a sign-in.
        if index % 5 == 0:
            events += _login_events(base)
            base += timedelta(minutes=2)

        # One session in six wanders onto a non-allowlisted site.
        if index % 6 == 3:
            events += _off_limits_events(base)
            base += timedelta(minutes=1)

        if index % 7 == 5:
            # An unrelated task.
            events += _expense_run(base, f"CLM-{4000 + index}")
            base += timedelta(minutes=25)
        else:
            runs_in_session = rng.choice([1, 1, 2])
            for _ in range(runs_in_session):
                order_id = str(10_000 + rng.randint(0, 8_000))
                events += _order_lookup_run(
                    base,
                    order_id,
                    paid=rng.random() < 0.72,
                    check_history=rng.random() < 0.3,
                )
                # A clear gap so segmentation sees two separate runs.
                base += timedelta(minutes=rng.randint(20, 40))

        sessions.append(
            {
                "session_id": session_id,
                "started_at": (base - timedelta(minutes=30)).isoformat(),
                "device": f"workstation-{(index % 3) + 1}",
                "events": events,
            }
        )
        base += timedelta(hours=rng.randint(1, 4))

    return sessions


#: What the demo tenant consents to record.
DEMO_POLICY = {
    "allowed_origins": list(ALLOWED_ORIGINS),
    "blocked_origins": [],
    "extra_sensitive_fields": [],
    "capture_screenshots": False,
}

#: Facts the demo is built to demonstrate, asserted by the tests.
DEMO_EXPECTATIONS = [
    "The order-lookup workflow is discovered as a repeated sequence.",
    "The reply template is detected as a branch, not resolved into one path.",
    "The order number is detected as a variable; the signature as a constant.",
    "The customer-history detour is marked optional.",
    "The expense workflow is kept separate from the order workflow.",
    "No password value, in any form, is ever stored.",
    "Keystroke events are refused outright.",
    "Events from a non-allowlisted origin are rejected at capture.",
]
