from __future__ import annotations

import asyncio
from dataclasses import dataclass

from realtime_action_spike.runtime.controller import (
    ActivationResult,
    TerminalSessionResult,
    VoiceSessionController,
)


@dataclass(frozen=True, slots=True)
class ActivationTranscript:
    activation: ActivationResult
    terminal: TerminalSessionResult | None


class FakeActivationClient:
    """Model the native client: activation returns only after the session is terminal."""

    def __init__(self, controller: VoiceSessionController, voice_page_url: str) -> None:
        self.controller = controller
        self.voice_page_url = voice_page_url
        self.activation_started = asyncio.Event()
        self.activation: ActivationResult | None = None
        self._task: asyncio.Task[ActivationTranscript] | None = None

    def start(self) -> asyncio.Task[ActivationTranscript]:
        if self._task is not None:
            raise RuntimeError("activation client already started")
        self._task = asyncio.create_task(self._run())
        return self._task

    async def _run(self) -> ActivationTranscript:
        activation = await self.controller.activate(self.voice_page_url)
        self.activation = activation
        self.activation_started.set()
        if activation.status != "opened" or activation.session_id is None:
            return ActivationTranscript(activation=activation, terminal=None)
        terminal = await self.controller.wait_for_terminal_result(activation.session_id)
        return ActivationTranscript(activation=activation, terminal=terminal)
