"""Playwright driver.

Used for real execution against a live site, inside the isolated executor
container. It is deliberately thin: every decision about *whether* an action may
run has already been made by the engine, so this file only translates DSL
primitives into browser calls.

Selectors are semantic throughout — ``get_by_role`` with an accessible name,
never a CSS path. That is the same representation the capture layer records, so
a workflow does not silently depend on markup that will change.

**On verification.** This driver is not exercised by the test suite, which runs
against the simulated driver instead. Testing it needs a browser binary and a
live portal, and a driver whose every call is mocked is not tested at all. The
engine's behaviour — approval gates, origin enforcement, dry-run isolation,
sanitised step logging — is covered in full against the simulator, because that
is where the decisions live.
"""
from __future__ import annotations

from typing import Any

from app.execution.runner import Driver, DriverError


class PlaywrightDriver(Driver):
    name = "playwright"

    def __init__(self, page: Any, *, base_urls: dict[str, str] | None = None) -> None:
        #: A Playwright ``Page``. Injected rather than created here, so the
        #: container owns the browser lifecycle.
        self.page = page
        self.base_urls = base_urls or {}

    def _url(self, origin: str, path: str) -> str:
        base = self.base_urls.get(origin) or f"https://{origin}"
        return base.rstrip("/") + "/" + path.lstrip("/")

    def navigate(self, origin: str, path: str) -> dict:
        try:
            self.page.goto(self._url(origin, path), wait_until="domcontentloaded")
        except Exception as exc:
            raise DriverError(f"navigation failed: {exc}") from exc
        return {"origin": origin, "path": path, "title": self.page.title()}

    def read_text(self, role: str, name: str) -> str:
        try:
            locator = (
                self.page.get_by_role(role, name=name) if role else self.page.get_by_text(name)
            )
            return locator.first.inner_text()
        except Exception as exc:
            raise DriverError(f"could not read {role} '{name}': {exc}") from exc

    def click(self, role: str, name: str) -> dict:
        try:
            self.page.get_by_role(role, name=name).first.click()
        except Exception as exc:
            raise DriverError(f"could not click {role} '{name}': {exc}") from exc
        return {"clicked": name}

    def fill(self, role: str, name: str, field_name: str, value: str) -> dict:
        try:
            locator = (
                self.page.get_by_role(role, name=name)
                if name
                else self.page.get_by_label(field_name)
            )
            locator.first.fill(value)
        except Exception as exc:
            raise DriverError(f"could not fill {role} '{name or field_name}': {exc}") from exc
        # The value never enters the return value, and so never enters a log.
        return {"filled": field_name or name}

    def wait_for(self, role: str, name: str, timeout: float) -> bool:
        try:
            self.page.get_by_role(role, name=name).first.wait_for(timeout=timeout * 1000)
            return True
        except Exception:
            return False

    def create_draft(self, label: str, payload: dict) -> dict:
        # A draft is prepared in the target system, never sent. How depends on
        # the connector, and there is deliberately no generic "send" path here:
        # a driver that could send anything would undo the engine's gating.
        raise DriverError(
            "draft creation requires a configured connector for the target system"
        )
