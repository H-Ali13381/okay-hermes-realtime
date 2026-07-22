from __future__ import annotations

import asyncio

import pytest

from realtime_action_spike.runtime.session_startup import BrowserStartupDeadline


@pytest.mark.asyncio
async def test_startup_deadline_expires_once() -> None:
    expired = asyncio.Event()
    calls = 0

    async def on_expired() -> None:
        nonlocal calls
        calls += 1
        expired.set()

    deadline = BrowserStartupDeadline(0.01, on_expired)
    deadline.start()

    await asyncio.wait_for(expired.wait(), timeout=0.2)
    await asyncio.sleep(0)

    assert calls == 1


@pytest.mark.asyncio
async def test_startup_deadline_cancel_suppresses_expiration() -> None:
    expired = False

    async def on_expired() -> None:
        nonlocal expired
        expired = True

    deadline = BrowserStartupDeadline(0.01, on_expired)
    deadline.start()
    deadline.cancel()

    await asyncio.sleep(0.03)

    assert expired is False
