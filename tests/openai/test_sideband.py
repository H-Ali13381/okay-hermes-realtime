from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

import pytest

from realtime_action_spike.openai.sideband import (
    MAX_SIDEBAND_EVENT_BYTES,
    RealtimeSidebandClient,
    SidebandEvent,
    build_sideband_url,
)
from realtime_action_spike.runtime.browser import NoopBrowserHandle
from realtime_action_spike.runtime.controller import StaleControlMessage, VoiceSessionController
from realtime_action_spike.runtime.protocol import (
    SessionOutcome,
    TeardownCompleteMessage,
    encode_loopback_message,
)


@dataclass
class _FakeWebSocket:
    messages: deque[str | BaseException]
    received: list[str] = field(default_factory=list)
    closed: bool = False
    active_send_calls: int = 0
    max_active_send_calls: int = 0

    def __post_init__(self) -> None:
        self._close_signal = asyncio.Event()
        self._send_guard = asyncio.Lock()

    async def recv(self) -> str:
        while not self.messages:
            if self.closed:
                raise RuntimeError("closed")
            await self._close_signal.wait()
            if self.closed:
                raise RuntimeError("closed")
        message = self.messages.popleft()
        if isinstance(message, BaseException):
            raise message
        return message

    async def send(self, payload: str) -> None:
        async with self._send_guard:
            self.active_send_calls += 1
            self.max_active_send_calls = max(
                self.max_active_send_calls,
                self.active_send_calls,
            )
            self.received.append(payload)
            await asyncio.sleep(0)
            self.active_send_calls -= 1

    async def close(self) -> None:
        self.closed = True
        self._close_signal.set()


class _Connector:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> _FakeWebSocket:
        self.calls.append((url, headers))
        return self.websocket


async def test_build_sideband_url_encodes_call_id_for_query_param() -> None:
    assert (
        build_sideband_url("call/needs encoding%")
        == "wss://api.openai.com/v1/realtime?call_id=call%2Fneeds%20encoding%25"
    )


async def test_connect_uses_authorized_websocket_url_and_uses_secret_only_header() -> None:
    ws = _FakeWebSocket(messages=deque([RuntimeError("closed")]))
    connector = _Connector(ws)
    events: list[SidebandEvent] = []
    failures: list[tuple[str, Exception]] = []

    async def on_event(event: SidebandEvent) -> None:
        events.append(event)

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-1",
        call_id="call secret/with spaces",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=connector,
    )

    await client.connect()
    await asyncio.sleep(0)

    assert len(connector.calls) == 1
    url, headers = connector.calls[0]
    assert url == "wss://api.openai.com/v1/realtime?call_id=call%20secret%2Fwith%20spaces"
    assert headers == {"Authorization": "Bearer test-api-key"}
    assert not events
    assert len(failures) == 1
    assert failures[0][0] == "session-1"
    assert "test-api-key" not in url


async def test_sideband_event_json_is_decoded_and_dispatched_as_dict_payload() -> None:
    ws = _FakeWebSocket(messages=deque(["{\"type\": \"response.finished\"}"]))
    connector = _Connector(ws)
    events: list[SidebandEvent] = []
    failures: list[tuple[str, Exception]] = []

    async def on_event(event: SidebandEvent) -> None:
        events.append(event)

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-1",
        call_id="call_123",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=connector,
    )

    await client.connect()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert events
    assert events[0] == SidebandEvent(
        local_session_id="session-1",
        payload={"type": "response.finished"},
    )
    assert not failures
    await client.close()


async def test_malformed_sideband_json_triggers_terminal_failure() -> None:
    ws = _FakeWebSocket(messages=deque(["not-json"]))
    connector = _Connector(ws)
    events: list[SidebandEvent] = []
    failures: list[tuple[str, Exception]] = []

    async def on_event(event: SidebandEvent) -> None:
        events.append(event)

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-1",
        call_id="call_123",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=connector,
    )

    await client.connect()
    await asyncio.sleep(0)

    assert not events
    assert len(failures) == 1
    assert failures[0][0] == "session-1"


async def test_send_json_uses_single_writer_lock() -> None:
    ws = _FakeWebSocket(messages=deque())
    connector = _Connector(ws)
    failures: list[tuple[str, Exception]] = []

    async def on_event(event: SidebandEvent) -> None:
        return None

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-1",
        call_id="call_123",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=connector,
    )

    await client.connect()
    first = asyncio.create_task(client.send_json({"index": 1}))
    second = asyncio.create_task(client.send_json({"index": 2}))
    await asyncio.gather(first, second)

    assert ws.max_active_send_calls == 1
    assert len(failures) == 0


async def test_clean_close_cancels_reader_and_marks_closed() -> None:
    ws = _FakeWebSocket(messages=deque())
    connector = _Connector(ws)

    async def on_event(event: SidebandEvent) -> None:
        return None

    async def on_failure(session_id: str, error: Exception) -> None:
        return None

    client = RealtimeSidebandClient(
        local_session_id="session-1",
        call_id="call_123",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=connector,
    )

    await client.connect()
    await client.close()

    assert client.closed
    assert ws.closed


async def test_remote_sideband_close_reports_failure() -> None:
    ws = _FakeWebSocket(messages=deque([RuntimeError("remote close")]))
    connector = _Connector(ws)
    failures: list[tuple[str, Exception]] = []

    async def on_event(event: SidebandEvent) -> None:
        return None

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-1",
        call_id="call_123",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=connector,
    )

    await client.connect()
    await asyncio.sleep(0)

    assert len(failures) == 1
    assert failures[0][0] == "session-1"
    assert isinstance(failures[0][1], Exception)


async def test_oversized_sideband_event_reports_terminal_failure() -> None:
    oversized = "x" * (MAX_SIDEBAND_EVENT_BYTES + 1)
    ws = _FakeWebSocket(messages=deque([oversized]))
    failures: list[tuple[str, Exception]] = []

    async def on_event(_event: SidebandEvent) -> None:
        raise AssertionError("oversized event must not be dispatched")

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-large",
        call_id="call_large",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=_Connector(ws),
    )

    await client.connect()
    await asyncio.sleep(0)

    assert len(failures) == 1
    assert isinstance(failures[0][1], ValueError)
    assert ws.closed


async def test_cancelled_connect_does_not_report_terminal_failure() -> None:
    release = asyncio.Event()
    failures: list[tuple[str, Exception]] = []

    async def blocked_connector(_url: str, _headers: dict[str, str]) -> _FakeWebSocket:
        await release.wait()
        return _FakeWebSocket(messages=deque())

    async def on_event(_event: SidebandEvent) -> None:
        return None

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-cancel",
        call_id="call_cancel",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=blocked_connector,
    )
    connect_task = asyncio.create_task(client.connect())
    await asyncio.sleep(0)
    connect_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await connect_task

    assert not failures
    await client.close()


async def test_close_during_connect_closes_late_socket_without_terminal_failure() -> None:
    connect_started = asyncio.Event()
    release_connect = asyncio.Event()
    websocket = _FakeWebSocket(messages=deque())
    failures: list[tuple[str, Exception]] = []

    async def delayed_connector(_url: str, _headers: dict[str, str]) -> _FakeWebSocket:
        connect_started.set()
        await release_connect.wait()
        return websocket

    async def on_event(_event: SidebandEvent) -> None:
        return None

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-connect-close",
        call_id="call_connect_close",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=delayed_connector,
    )
    connect_task = asyncio.create_task(client.connect())
    await connect_started.wait()
    await client.close()
    release_connect.set()

    try:
        with pytest.raises(RuntimeError, match="closed during connect"):
            await connect_task
        assert websocket.closed
        assert not failures
    finally:
        await client.close()


async def test_connect_failure_notifies_terminal_failure_once() -> None:
    failures: list[tuple[str, Exception]] = []

    async def failing_connector(_url: str, _headers: dict[str, str]) -> _FakeWebSocket:
        raise OSError("connect failed")

    async def on_event(_event: SidebandEvent) -> None:
        return None

    async def on_failure(session_id: str, error: Exception) -> None:
        failures.append((session_id, error))

    client = RealtimeSidebandClient(
        local_session_id="session-connect-failure",
        call_id="call_connect_failure",
        api_key="test-api-key",
        on_event=on_event,
        on_terminal_failure=on_failure,
        websocket_connect=failing_connector,
    )

    with pytest.raises(OSError, match="connect failed"):
        await client.connect()

    assert client.closed
    assert len(failures) == 1
    assert failures[0][0] == "session-connect-failure"


class _Launcher:
    def launch(self, loopback_url: str) -> NoopBrowserHandle:
        del loopback_url
        return NoopBrowserHandle()


def _controller(*session_ids: str) -> VoiceSessionController:
    sequence = iter(session_ids)
    return VoiceSessionController(_Launcher(), session_id_factory=lambda: next(sequence))


async def _finish_session(controller: VoiceSessionController, session_id: str) -> None:
    await controller.process_control_message(
        session_id,
        encode_loopback_message(
            TeardownCompleteMessage(type="teardown_complete", session_id=session_id)
        ),
    )


async def test_controller_closes_sideband_and_rejects_stale_session_events() -> None:
    controller = _controller("local-first-01", "local-second-02")
    first = await controller.activate("http://127.0.0.1:8765/voice")
    assert first.session_id == "local-first-01"
    first_ws = _FakeWebSocket(messages=deque())

    await controller.start_realtime_sideband(
        local_session_id="local-first-01",
        call_id="call_first",
        api_key="server-secret",
        websocket_connect=_Connector(first_ws),
    )
    await controller.send_sideband_event(
        "local-first-01",
        {"type": "session.update"},
    )
    assert first_ws.received == ['{"type":"session.update"}']
    await _finish_session(controller, "local-first-01")

    assert first_ws.closed
    second = await controller.activate("http://127.0.0.1:8765/voice")
    assert second.session_id == "local-second-02"
    with pytest.raises(StaleControlMessage):
        await controller.process_sideband_event("local-first-01", {"type": "response.done"})


async def test_controller_sideband_failure_resolves_sanitized_terminal_result() -> None:
    controller = _controller("local-failure-01")
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    assert activation.session_id == "local-failure-01"
    ws = _FakeWebSocket(messages=deque([RuntimeError("call_secret provider failure")]))

    await controller.start_realtime_sideband(
        local_session_id="local-failure-01",
        call_id="call_secret",
        api_key="api-secret",
        websocket_connect=_Connector(ws),
    )
    result = await asyncio.wait_for(
        controller.wait_for_terminal_result("local-failure-01"),
        timeout=1.0,
    )

    assert result.outcome is SessionOutcome.FAILED
    assert result.error == "sideband connection failed"
    assert "call_secret" not in result.error
    assert "api-secret" not in result.error
    assert ws.closed
