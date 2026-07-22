from __future__ import annotations

from dataclasses import dataclass

from realtime_action_spike.runtime.browser import BrowserHandle


@dataclass(slots=True)
class FakeBrowserHandle:
    close_error: Exception | None = None
    close_calls: int = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeBrowserLauncher:
    def __init__(self, *, launch_error: Exception | None = None) -> None:
        self.launch_error = launch_error
        self.launched_urls: list[str] = []
        self.handles: list[FakeBrowserHandle] = []

    def launch(self, loopback_url: str) -> BrowserHandle:
        self.launched_urls.append(loopback_url)
        if self.launch_error is not None:
            raise self.launch_error
        handle = FakeBrowserHandle()
        self.handles.append(handle)
        return handle
