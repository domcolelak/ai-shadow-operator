"""The capture model.

This module decides what the product is. Everything downstream — mining,
workflow generation, execution — operates on what this layer allows through,
so the privacy guarantees have to be enforced *here*, at ingestion, and not
left to a policy document.

Four rules, enforced in code and covered by tests:

1. **Password and sensitive fields never produce a value**, not even a hashed
   one. A hash of a password is still a password oracle.
2. **Keystrokes are never persisted.** An input action records the *field* and
   a classification of what was typed, never the text.
3. **Only allowlisted origins are recorded.** An event from anywhere else is
   dropped at the door, with a reason.
4. **Selectors are semantic.** Role and accessible name, not CSS paths — brittle
   selectors make automation that breaks silently, and a CSS path can itself
   leak content.

The value classification is the interesting part: to discover that a workflow
has a variable, you need to know that "the thing typed into the order field
differs every run". You do *not* need to know what it was. A stable
per-session-salt hash gives exactly that and nothing else.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse


class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    INPUT = "input"
    SELECT = "select"
    READ = "read"
    SUBMIT = "submit"
    KEYPRESS = "keypress"
    SCROLL = "scroll"
    WAIT = "wait"


#: Actions that carry no workflow meaning on their own. Kept out of the mined
#: sequence, because a workflow that includes every scroll is not a workflow.
NOISE_ACTIONS = frozenset({ActionType.SCROLL, ActionType.WAIT})


class ValueClass(str, Enum):
    """What was typed, without what was typed."""

    #: Same value in every observed run -- a constant the automation can hold.
    CONSTANT = "constant"
    #: Differs between runs -- a variable the automation must be given.
    VARIABLE = "variable"
    #: Recognised as sensitive. Never hashed, never stored, never automated.
    SENSITIVE = "sensitive"
    #: Not enough observations to tell yet.
    UNKNOWN = "unknown"


#: Field names and types that must never be captured, in any form.
SENSITIVE_FIELD_PATTERNS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "otp",
    "mfa",
    "2fa",
    "cvv",
    "cvc",
    "card",
    "iban",
    "ssn",
    "national_id",
    "pin",
    "security_answer",
    "api_key",
    "private_key",
)

SENSITIVE_INPUT_TYPES = frozenset({"password"})

SENSITIVE_AUTOCOMPLETE = frozenset(
    {
        "current-password",
        "new-password",
        "cc-number",
        "cc-csc",
        "cc-exp",
        "one-time-code",
    }
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{9,}(?!\d)")


class CaptureRejected(Exception):
    """An event that must not be stored, with the reason why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ElementRef:
    """A semantic reference to an element.

    Deliberately not a CSS selector: a selector generated from a live DOM binds
    the automation to markup that will change, and can embed page content in
    the stored path.
    """

    role: str = ""
    accessible_name: str = ""
    field_name: str = ""
    input_type: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def is_sensitive(self) -> bool:
        if self.input_type.lower() in SENSITIVE_INPUT_TYPES:
            return True
        haystack = f"{self.field_name} {self.accessible_name}".lower()
        return any(pattern in haystack for pattern in SENSITIVE_FIELD_PATTERNS)

    def signature(self) -> str:
        """Identity used when matching the same step across sessions."""
        return f"{self.role}|{self.accessible_name.strip().lower()}|{self.field_name.strip().lower()}"


@dataclass
class CapturedAction:
    """One normalised, privacy-filtered user action."""

    session_id: str
    sequence: int
    occurred_at: datetime
    action: ActionType
    origin: str = ""
    page_title: str = ""
    element: ElementRef = field(default_factory=ElementRef)
    #: Where a navigation went. Path only -- query strings carry identifiers.
    target_path: str = ""
    value_class: ValueClass = ValueClass.UNKNOWN
    #: Salted hash of the typed value, or None for sensitive fields.
    value_hash: str | None = None
    #: Length bucket, useful for spotting a field that is always an order id.
    value_shape: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["occurred_at"] = self.occurred_at.isoformat()
        payload["action"] = self.action.value
        payload["value_class"] = self.value_class.value
        return payload

    @property
    def is_noise(self) -> bool:
        return self.action in NOISE_ACTIONS

    def step_signature(self) -> str:
        """What makes two actions "the same step" across different sessions.

        Values are excluded on purpose -- that is exactly what varies between
        runs of the same workflow.
        """
        parts = [self.action.value, self.origin, self.element.signature()]
        if self.action is ActionType.NAVIGATE:
            parts.append(_generalise_path(self.target_path))
        return "::".join(parts)


def _generalise_path(path: str) -> str:
    """Replace identifier-looking path segments with a placeholder.

    ``/orders/10482/detail`` and ``/orders/10917/detail`` are the same step in
    a workflow. Without this, every run looks like a different sequence and
    nothing is ever discovered as repeated.
    """
    segments = []
    for segment in (path or "").strip("/").split("/"):
        if not segment:
            continue
        if segment.isdigit() or _looks_like_id(segment):
            segments.append("{id}")
        else:
            segments.append(segment.lower())
    return "/" + "/".join(segments)


def _looks_like_id(segment: str) -> bool:
    if len(segment) < 6:
        return False
    digits = sum(c.isdigit() for c in segment)
    return digits >= len(segment) // 2 or "-" in segment and len(segment) >= 20


def value_hash(value: str, salt: str) -> str:
    """Salted hash, for telling values apart without knowing them.

    The salt is per session-group, so the same order number in two sessions
    hashes alike (which is what makes constant-vs-variable detection work)
    while the hash is useless outside this workspace.
    """
    return hmac.new(salt.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def value_shape(value: str) -> str:
    """A coarse description of a value: its type and length band.

    Enough to say "this field always receives a 6-8 digit number"; not enough
    to reconstruct anything.
    """
    if not value:
        return "empty"
    stripped = value.strip()
    if _EMAIL.fullmatch(stripped):
        kind = "email"
    elif stripped.isdigit():
        kind = "digits"
    elif stripped.replace(".", "", 1).replace(",", "", 1).isdigit():
        kind = "number"
    elif stripped.isalpha():
        kind = "alpha"
    else:
        kind = "mixed"
    length = len(stripped)
    band = "1-4" if length <= 4 else "5-10" if length <= 10 else "11-40" if length <= 40 else "40+"
    return f"{kind}:{band}"


def redact(text: str) -> str:
    """Strip direct identifiers from anything free-form we do keep."""
    if not text:
        return text
    return _LONG_DIGITS.sub("[number]", _EMAIL.sub("[email]", text))


@dataclass
class CapturePolicy:
    """The consent boundary, expressed as data."""

    #: Only these origins are recorded. Empty means nothing is recorded --
    #: a deliberately safe default rather than "record everything".
    allowed_origins: tuple[str, ...] = ()
    blocked_origins: tuple[str, ...] = ()
    #: Extra field-name fragments this tenant considers sensitive.
    extra_sensitive_fields: tuple[str, ...] = ()
    #: Off by default. Screenshots are the single most invasive thing we could
    #: keep, and nothing in the pipeline needs them.
    capture_screenshots: bool = False

    def origin_allowed(self, origin: str) -> bool:
        host = _host_of(origin)
        if not host:
            return False
        if any(_host_matches(host, b) for b in self.blocked_origins):
            return False
        return any(_host_matches(host, a) for a in self.allowed_origins)

    def is_sensitive_field(self, element: ElementRef) -> bool:
        if element.is_sensitive:
            return True
        haystack = f"{element.field_name} {element.accessible_name}".lower()
        return any(extra.lower() in haystack for extra in self.extra_sensitive_fields)


def _host_of(origin: str) -> str:
    if not origin:
        return ""
    parsed = urlparse(origin if "//" in origin else f"//{origin}", scheme="https")
    return (parsed.hostname or "").lower()


def _host_matches(host: str, pattern: str) -> bool:
    pattern = _host_of(pattern) or pattern.lower().lstrip(".")
    if not pattern:
        return False
    return host == pattern or host.endswith("." + pattern)


def capture_action(
    raw: dict[str, Any],
    *,
    policy: CapturePolicy,
    session_id: str,
    sequence: int,
    salt: str,
) -> CapturedAction:
    """Turn a raw browser event into a storable action, or refuse it.

    Raises :class:`CaptureRejected` rather than returning something partial:
    a rejected event must leave no trace beyond a counter and a reason.
    """
    origin = str(raw.get("origin") or raw.get("url_origin") or "")
    if not policy.origin_allowed(origin):
        raise CaptureRejected(f"origin '{origin or '(none)'}' is not on the allowlist")

    try:
        action = ActionType(str(raw.get("action", "")).lower())
    except ValueError as exc:
        raise CaptureRejected(f"unknown action '{raw.get('action')}'") from exc

    element_raw = raw.get("element") or {}
    element = ElementRef(
        role=str(element_raw.get("role", "")),
        accessible_name=redact(str(element_raw.get("accessible_name", ""))),
        field_name=str(element_raw.get("field_name", "")),
        input_type=str(element_raw.get("input_type", "")),
    )

    # A raw keystroke stream can reconstruct anything typed, including things
    # the field-level filter would have caught. There is no version of this we
    # keep.
    if action is ActionType.KEYPRESS:
        raise CaptureRejected("keystroke events are never recorded")

    value_klass = ValueClass.UNKNOWN
    hashed: str | None = None
    shape = ""

    if action in (ActionType.INPUT, ActionType.SELECT):
        autocomplete = str(element_raw.get("autocomplete", "")).lower()
        sensitive = policy.is_sensitive_field(element) or autocomplete in SENSITIVE_AUTOCOMPLETE
        if sensitive:
            # Recorded as a step so the workflow still makes sense, but with
            # nothing derived from the value at all.
            value_klass = ValueClass.SENSITIVE
        else:
            raw_value = raw.get("value")
            if raw_value is not None:
                text = str(raw_value)
                hashed = value_hash(text, salt)
                shape = value_shape(text)

    return CapturedAction(
        session_id=session_id,
        sequence=sequence,
        occurred_at=_timestamp(raw.get("timestamp")),
        action=action,
        origin=_host_of(origin),
        page_title=redact(str(raw.get("page_title", ""))),
        element=element,
        target_path=_path_of(raw.get("target") or raw.get("url") or ""),
        value_class=value_klass,
        value_hash=hashed,
        value_shape=shape,
        metadata={},
    )


def _path_of(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    # The query string is dropped, not stored: it routinely carries customer
    # identifiers, tokens and search terms.
    return parsed.path or url if parsed.scheme or parsed.netloc else url.split("?")[0]


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


@dataclass
class CaptureResult:
    actions: list[CapturedAction] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.actions)


def capture_batch(
    raw_events: Iterable[dict[str, Any]],
    *,
    policy: CapturePolicy,
    session_id: str,
    salt: str,
    start_sequence: int = 0,
) -> CaptureResult:
    """Filter and normalise a batch of raw events."""
    result = CaptureResult()
    sequence = start_sequence
    for index, raw in enumerate(raw_events):
        try:
            result.actions.append(
                capture_action(
                    raw, policy=policy, session_id=session_id, sequence=sequence, salt=salt
                )
            )
            sequence += 1
        except CaptureRejected as exc:
            # The reason is kept; the event is not.
            result.rejected.append({"index": str(index), "reason": exc.reason})
    return result


def summarise_capture(actions: Sequence[CapturedAction]) -> dict:
    """What was recorded, for showing the user exactly what was kept."""
    by_action: dict[str, int] = {}
    origins: set[str] = set()
    sensitive_steps = 0
    for item in actions:
        by_action[item.action.value] = by_action.get(item.action.value, 0) + 1
        if item.origin:
            origins.add(item.origin)
        if item.value_class is ValueClass.SENSITIVE:
            sensitive_steps += 1
    return {
        "action_count": len(actions),
        "by_action": by_action,
        "origins": sorted(origins),
        "sensitive_steps_recorded_without_values": sensitive_steps,
        "values_stored": False,
        "screenshots_stored": False,
    }
