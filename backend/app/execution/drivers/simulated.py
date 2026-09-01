"""In-process driver backed by a fake portal.

This exists so execution is actually tested. A Playwright driver needs a
browser, a container and a live site; wiring the test suite to that would mean
the approval gates, the origin enforcement and the step logging are only ever
exercised by hand.

The fake portal is a small state machine with the same shape as the demo site:
pages, controls and fields. It records every mutating call, so a test can
assert not only what the engine returned but what it *did* — and, for a dry
run, that it did nothing at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.execution.runner import Driver, DriverError


@dataclass
class Page:
    path: str
    title: str
    #: role -> accessible names present on the page.
    controls: dict[str, set[str]] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)

    def has(self, role: str, name: str) -> bool:
        return name in self.controls.get(role, set())


@dataclass
class FakePortal:
    origins: dict[str, dict[str, Page]] = field(default_factory=dict)
    current_origin: str = ""
    current_path: str = ""
    #: Everything the driver was asked to change.
    mutations: list[dict[str, Any]] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    filled: dict[str, str] = field(default_factory=dict)

    def add_page(self, origin: str, page: Page) -> None:
        self.origins.setdefault(origin, {})[page.path] = page

    @property
    def page(self) -> Page | None:
        return self.origins.get(self.current_origin, {}).get(self.current_path)


class SimulatedDriver(Driver):
    name = "simulated"

    def __init__(self, portal: FakePortal) -> None:
        self.portal = portal

    def _require_page(self) -> Page:
        page = self.portal.page
        if page is None:
            raise DriverError(
                f"no page at {self.portal.current_origin}{self.portal.current_path}"
            )
        return page

    def navigate(self, origin: str, path: str) -> dict:
        target_origin = origin or self.portal.current_origin
        pages = self.portal.origins.get(target_origin)
        if pages is None:
            raise DriverError(f"unknown origin '{target_origin}'")
        # Paths are matched loosely so a generalised '{id}' segment resolves.
        resolved = path if path in pages else _match_path(path, pages)
        if resolved is None:
            raise DriverError(f"no page at '{path}' on {target_origin}")
        self.portal.current_origin = target_origin
        self.portal.current_path = resolved
        return {"origin": target_origin, "path": resolved, "title": pages[resolved].title}

    def read_text(self, role: str, name: str) -> str:
        page = self._require_page()
        if name not in page.text:
            raise DriverError(f"no text '{name}' on {page.path}")
        return page.text[name]

    def click(self, role: str, name: str) -> dict:
        page = self._require_page()
        if not page.has(role, name):
            raise DriverError(f"no {role} '{name}' on {page.path}")
        self.portal.mutations.append({"type": "click", "role": role, "name": name})
        return {"clicked": name}

    def fill(self, role: str, name: str, field_name: str, value: str) -> dict:
        page = self._require_page()
        if not page.has(role, name):
            raise DriverError(f"no {role} '{name}' on {page.path}")
        key = field_name or name
        self.portal.filled[key] = value
        self.portal.mutations.append({"type": "fill", "field": key})
        return {"filled": key}

    def wait_for(self, role: str, name: str, timeout: float) -> bool:
        page = self.portal.page
        return bool(page and page.has(role, name))

    def create_draft(self, label: str, payload: dict) -> dict:
        self.portal.drafts.append({"label": label, "payload": payload})
        return {"draft_id": f"DRAFT-{len(self.portal.drafts)}"}


def _match_path(path: str, pages: dict[str, Page]) -> str | None:
    """Resolve a path containing a generalised '{id}' segment."""
    wanted = [p for p in path.strip("/").split("/") if p]
    for candidate in pages:
        segments = [p for p in candidate.strip("/").split("/") if p]
        if len(segments) != len(wanted):
            continue
        if all(w == s or w == "{id}" or s == "{id}" for w, s in zip(wanted, segments)):
            return candidate
    return None


def demo_portal() -> FakePortal:
    """A fake portal matching the demo recordings."""
    portal = FakePortal()

    portal.add_page(
        "mail.demo.local",
        Page(
            path="/mail/inbox",
            title="Inbox",
            controls={"listitem": {"Where is my order?"}},
            # A reading pane: opening the message shows its content without
            # changing the URL, which is what the recordings show.
            text={"Order number": "10482"},
        ),
    )
    portal.add_page(
        "mail.demo.local",
        Page(
            path="/mail/message",
            title="Message",
            controls={"button": {"Reply", "Send"}, "combobox": {"Template"},
                      "textbox": {"Tracking number", "Amount due", "Signature"}},
            text={"Order number": "10482"},
        ),
    )
    portal.add_page(
        "portal.demo.local",
        Page(
            path="/login",
            title="Sign in",
            # Present because recorded sessions start here often enough that the
            # miner can put it in a workflow's canonical path. A driver that
            # cannot reach a page the workflow legitimately visits fails the run
            # for the wrong reason.
            controls={"textbox": {"Email", "Password"}, "button": {"Sign in"}},
        ),
    )
    portal.add_page(
        "portal.demo.local",
        Page(
            path="/orders",
            title="Orders",
            controls={"textbox": {"Search orders"}, "button": {"Search"}},
        ),
    )
    portal.add_page(
        "portal.demo.local",
        Page(
            path="/orders/{id}/detail",
            title="Order detail",
            controls={"link": {"Customer history"}},
            text={"Payment status": "paid", "Shipment status": "in transit",
                  "Previous orders": "3"},
        ),
    )
    return portal
