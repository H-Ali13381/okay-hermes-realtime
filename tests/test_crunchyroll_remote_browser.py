from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import realtime_action_spike.crunchyroll_remote.cdp as cdp_module
from realtime_action_spike.crunchyroll_remote import (
    BraveRemoteLauncher,
    CdpInputController,
    RemoteCommand,
    RemotePrototypeError,
)
from realtime_action_spike.crunchyroll_remote.cdp import (
    _select_origin_bootstrap_target,
    _select_page_target,
)


@dataclass
class FakeProcess:
    pid: int = 321
    poll_value: int | None = None

    def poll(self) -> int | None:
        return self.poll_value


@dataclass
class RecordingProcessFactory:
    process: FakeProcess = field(default_factory=FakeProcess)
    calls: list[tuple[list[str], dict[str, object]]] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: object) -> FakeProcess:
        self.calls.append((list(args), dict(kwargs)))
        return self.process


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_brave_remote_launcher_uses_dedicated_profile_loopback_cdp_and_fixed_sso_url(
    tmp_path: Path,
) -> None:
    binary = _executable(tmp_path / "brave")
    profile = tmp_path / "crunchyroll-profile"
    factory = RecordingProcessFactory()
    launcher = BraveRemoteLauncher(
        brave_binary=str(binary),
        profile_path=profile,
        debug_port=49200,
        process_factory=factory,
    )

    process = launcher.launch("http://127.0.0.1:49152/host/public-session")

    args, options = factory.calls[0]
    assert process.pid == 321
    assert args == [
        str(binary.resolve()),
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=49200",
        "--no-first-run",
        "--disable-default-apps",
        "--disable-background-mode",
        "--app=http://127.0.0.1:49152/host/public-session",
    ]
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert options["shell"] is False
    assert options["start_new_session"] is True
    assert oct(profile.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.20:49152/host/public-session",
        "http://user:pass@127.0.0.1:49152/host/public-session",
        "http://127.0.0.1:49152/host/public-session?secret=value",
        "file:///tmp/pair.html",
    ],
)
def test_brave_remote_launcher_rejects_unsafe_start_urls(tmp_path: Path, url: str) -> None:
    launcher = BraveRemoteLauncher(
        brave_binary=str(_executable(tmp_path / "brave")),
        profile_path=tmp_path / "profile",
        debug_port=49200,
        process_factory=RecordingProcessFactory(),
    )

    with pytest.raises(RemotePrototypeError):
        launcher.launch(url)


def test_select_page_target_requires_exact_expected_host_url() -> None:
    expected_url = "http://127.0.0.1:49152/host/expected"
    targets = [
        {"type": "page", "url": "https://example.com", "webSocketDebuggerUrl": "ws://wrong"},
        {"type": "page", "url": expected_url, "webSocketDebuggerUrl": "ws://expected"},
    ]

    assert _select_page_target(targets, expected_url=expected_url) == "ws://expected"
    with pytest.raises(StopIteration):
        _select_page_target(targets, expected_url="http://127.0.0.1:49152/host/other")


def test_select_origin_bootstrap_target_allows_only_single_exact_internal_page() -> None:
    bootstrap = {
        "type": "page",
        "url": "chrome://brave-origin-startup/",
        "webSocketDebuggerUrl": "ws://bootstrap",
    }

    assert _select_origin_bootstrap_target([bootstrap]) == "ws://bootstrap"
    with pytest.raises(StopIteration):
        _select_origin_bootstrap_target(
            [
                bootstrap,
                {
                    "type": "page",
                    "url": "https://example.com",
                    "webSocketDebuggerUrl": "ws://other",
                },
            ]
        )
    with pytest.raises(StopIteration):
        _select_origin_bootstrap_target(
            [
                {
                    "type": "page",
                    "url": "chrome://settings/",
                    "webSocketDebuggerUrl": "ws://settings",
                }
            ]
        )


@pytest.mark.asyncio
async def test_cdp_transport_bootstraps_origin_startup_to_exact_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from realtime_action_spike.crunchyroll_remote import LoopbackCdpTransport

    expected_url = "http://127.0.0.1:49152/host/expected"
    responses = iter(
        [
            [
                {
                    "type": "page",
                    "url": "chrome://brave-origin-startup/",
                    "webSocketDebuggerUrl": "ws://bootstrap",
                }
            ],
            [
                {
                    "type": "page",
                    "url": expected_url,
                    "webSocketDebuggerUrl": "ws://expected",
                }
            ],
        ]
    )
    navigations: list[tuple[str, str]] = []

    class Response:
        def __init__(self, payload: object) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return self._payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str) -> Response:
            return Response(next(responses))

    class BootstrapSocket:
        async def send(self, payload: str) -> None:
            request = __import__("json").loads(payload)
            navigations.append((request["method"], request["params"]["url"]))

        async def recv(self) -> str:
            return '{"id":1,"result":{"frameId":"owned"}}'

        async def close(self) -> None:
            return None

    expected_socket = object()

    async def fake_connect(url: str, **_kwargs: object):
        return BootstrapSocket() if url == "ws://bootstrap" else expected_socket

    monkeypatch.setattr(cdp_module.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(cdp_module, "connect", fake_connect)

    transport = LoopbackCdpTransport(debug_port=49200, expected_url=expected_url)
    assert await transport._connect() is expected_socket
    assert navigations == [("Page.navigate", expected_url)]


@dataclass
class FailingWebSocket:
    async def send(self, _payload: str) -> None:
        raise OSError("connection reset")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_cdp_transport_normalizes_disconnect_and_drops_broken_socket() -> None:
    from realtime_action_spike.crunchyroll_remote import LoopbackCdpTransport

    transport = LoopbackCdpTransport(
        debug_port=49200,
        expected_url="http://127.0.0.1:49152/host/expected",
    )
    transport._websocket = FailingWebSocket()

    with pytest.raises(RemotePrototypeError, match="connection"):
        await transport.call("Input.insertText", text="private")

    assert transport._websocket is None


@dataclass
class FakeCdpTransport:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.calls.append((method, params))
        return {}


@pytest.mark.asyncio
async def test_cdp_controller_pairs_by_navigating_to_genuine_crunchyroll_sso() -> None:
    transport = FakeCdpTransport()
    controller = CdpInputController(transport=transport)

    await controller.apply(RemoteCommand(version=1, sequence=1, kind="pair", value=None))

    method, params = transport.calls[0]
    assert method == "Page.navigate"
    assert params == {"url": "https://sso.crunchyroll.com/login"}


@pytest.mark.asyncio
async def test_cdp_controller_maps_only_allowlisted_input_commands() -> None:
    transport = FakeCdpTransport()
    controller = CdpInputController(transport=transport, paired=False)
    await controller.apply(RemoteCommand(version=1, sequence=1, kind="pair", value=None))
    transport.calls.clear()

    await controller.apply(RemoteCommand(version=1, sequence=2, kind="text", value="private"))
    await controller.apply(RemoteCommand(version=1, sequence=3, kind="key", value="Tab"))
    await controller.apply(RemoteCommand(version=1, sequence=4, kind="key", value="Enter"))

    assert transport.calls == [
        ("Input.insertText", {"text": "private"}),
        (
            "Input.dispatchKeyEvent",
            {"type": "keyDown", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
        ),
        (
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
        ),
        (
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
            },
        ),
        (
            "Input.dispatchKeyEvent",
            {"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
        ),
    ]


@pytest.mark.asyncio
async def test_cdp_controller_rejects_input_before_pairing_navigation() -> None:
    controller = CdpInputController(transport=FakeCdpTransport(), paired=False)

    with pytest.raises(RemotePrototypeError, match="paired"):
        await controller.apply(RemoteCommand(version=1, sequence=1, kind="text", value="private"))
