from __future__ import annotations

from typing import Protocol


class BrowserLaunchError(RuntimeError):
    """Raised when the dedicated browser cannot be launched or closed cleanly."""


class BrowserHandle(Protocol):
    """Minimal lifecycle contract returned by a browser launcher."""

    def close(self) -> None: ...


class NoopBrowserHandle:
    """Explicit non-allocating handle used by diagnostics and tests."""

    def __init__(self) -> None:
        self.closed_calls = 0

    def close(self) -> None:
        self.closed_calls += 1
