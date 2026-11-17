from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn
from nacl.secret import SecretBox

from .browser import BraveRemoteLauncher
from .cdp import CdpInputController, LoopbackCdpTransport
from .contracts import RemotePrototypeError
from .web import create_remote_app

_DEFAULT_PROFILE = Path("~/.local/share/okay-hermes-realtime/crunchyroll-brave-profile")
_DEFAULT_PAIRING_PORT = 42877
_BRAVE_CANDIDATES = (
    "/opt/brave-origin-bin/brave",
    "/opt/brave.com/brave-origin-nightly/brave",
    "brave-origin-nightly",
    "brave-origin",
    "brave",
)
SocketFactory = Callable[..., socket.socket]


@dataclass(frozen=True, slots=True)
class PrototypeConfiguration:
    brave_binary: str
    profile_path: Path
    lan_host: str | None
    port: int
    debug_port: int
    timeout_seconds: float
    smoke: bool

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> PrototypeConfiguration:
        lan_host = args.lan_host
        if lan_host is not None:
            _require_private_lan(lan_host)
        return cls(
            brave_binary=args.brave,
            profile_path=args.profile.expanduser(),
            lan_host=lan_host,
            port=args.port,
            debug_port=args.debug_port,
            timeout_seconds=args.timeout,
            smoke=args.smoke,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Try phone-assisted login in a dedicated Crunchyroll Brave profile."
    )
    parser.add_argument("--brave", default=_default_brave_binary())
    parser.add_argument("--profile", type=Path, default=_DEFAULT_PROFILE.expanduser())
    parser.add_argument("--lan-host", help="Private IPv4 address visible to the phone")
    parser.add_argument(
        "--port",
        type=_port,
        default=_DEFAULT_PAIRING_PORT,
        help=f"Pairing HTTP port (default: {_DEFAULT_PAIRING_PORT})",
    )
    parser.add_argument(
        "--debug-port", type=_nonzero_port, default=49200, help="Loopback-only Brave CDP port"
    )
    parser.add_argument("--timeout", type=_positive_seconds, default=300.0)
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    return parser


def choose_lan_ipv4(*, socket_factory: SocketFactory = socket.socket) -> str:
    with socket_factory(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("1.1.1.1", 80))
        address = probe.getsockname()[0]
    _require_private_lan(address)
    return address


def _require_private_lan(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError("a private LAN IPv4 address is required") from exc
    is_unsafe = (
        not isinstance(address, ipaddress.IPv4Address)
        or not address.is_private
        or address.is_loopback
    )
    if is_unsafe:
        raise ValueError("a private LAN IPv4 address is required")


def _port(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return number


def _nonzero_port(value: str) -> int:
    number = _port(value)
    if number == 0:
        raise argparse.ArgumentTypeError("port must be nonzero")
    return number


def _positive_seconds(value: str) -> float:
    number = float(value)
    if not 1 <= number <= 3_600:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 3600 seconds")
    return number


def _default_brave_binary() -> str:
    for candidate in _BRAVE_CANDIDATES:
        path = Path(candidate)
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return _BRAVE_CANDIDATES[0]


def _require_unused_loopback_port(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ValueError(f"loopback debug port {port} is already in use") from exc
    finally:
        probe.close()


def _listening_sockets(lan_host: str, requested_port: int) -> tuple[list[socket.socket], int]:
    sockets: list[socket.socket] = []
    try:
        lan_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lan_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lan_socket.bind((lan_host, requested_port))
        lan_socket.listen(128)
        lan_socket.setblocking(False)
        sockets.append(lan_socket)
        port = lan_socket.getsockname()[1]

        loopback_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        loopback_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        loopback_socket.bind(("127.0.0.1", port))
        loopback_socket.listen(128)
        loopback_socket.setblocking(False)
        sockets.append(loopback_socket)
        return sockets, port
    except Exception:
        for listening_socket in sockets:
            listening_socket.close()
        raise


async def _run(config: PrototypeConfiguration) -> int:
    ready = asyncio.Event()
    await _run_broker(
        config,
        session_id=secrets.token_urlsafe(16),
        ready=ready,
        announce=True,
    )
    return 0


async def run_login_broker(
    *,
    session_id: str,
    brave_binary: str,
    profile_path: str,
    timeout_seconds: float,
    ready: asyncio.Event,
) -> None:
    """Run one controller-owned login broker until completion or cancellation."""

    config = PrototypeConfiguration(
        brave_binary=brave_binary,
        profile_path=Path(profile_path).expanduser(),
        lan_host=None,
        port=_DEFAULT_PAIRING_PORT,
        debug_port=49200,
        timeout_seconds=timeout_seconds,
        smoke=False,
    )
    try:
        await _run_broker(config, session_id=session_id, ready=ready, announce=False)
    except RemotePrototypeError:
        raise
    except (OSError, ValueError) as exc:
        raise RemotePrototypeError(str(exc)) from None


async def _run_broker(
    config: PrototypeConfiguration,
    *,
    session_id: str,
    ready: asyncio.Event,
    announce: bool,
) -> None:
    lan_host = config.lan_host or choose_lan_ipv4()
    if not config.smoke:
        _require_unused_loopback_port(config.debug_port)
    sockets, port = _listening_sockets(lan_host, config.port)
    pairing_key = secrets.token_bytes(SecretBox.KEY_SIZE)
    cancelled = asyncio.Event()
    host_url = f"http://127.0.0.1:{port}/host/{session_id}"
    cdp = LoopbackCdpTransport(
        debug_port=config.debug_port,
        expected_url=host_url,
    )
    controller = CdpInputController(transport=cdp, paired=False, cancelled=cancelled)
    app = create_remote_app(
        session_id=session_id,
        pairing_key=pairing_key,
        public_base_url=f"http://{lan_host}:{port}",
        controller=controller,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=sockets))
    browser: subprocess.Popen | None = None
    try:
        await _await_server(server, server_task)
        if config.smoke:
            await _smoke_http_surface(host_url, session_id)
            ready.set()
            return
        browser = BraveRemoteLauncher(
            brave_binary=config.brave_binary,
            profile_path=config.profile_path,
            debug_port=config.debug_port,
        ).launch(host_url)
        await cdp.call("Runtime.enable")
        ready.set()
        if announce:
            print("Crunchyroll phone login prototype is running.")
            print(f"Host window: {host_url}")
            print("Scan the QR shown in Brave. Press Ctrl+C here to stop.")
        await _wait_for_end(browser, cancelled, config.timeout_seconds)
    finally:
        pairing_key = bytes(SecretBox.KEY_SIZE)
        await cdp.close()
        if browser is not None:
            await asyncio.to_thread(_close_browser, browser)
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(server_task, timeout=3.0)
        if not server_task.done():
            server_task.cancel()
        for listening_socket in sockets:
            listening_socket.close()


async def _await_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    while not server.started:
        if task.done():
            exception = task.exception()
            if exception is not None:
                raise exception
            raise RemotePrototypeError("pairing server stopped during startup")
        if asyncio.get_running_loop().time() >= deadline:
            raise RemotePrototypeError("pairing server did not become ready")
        await asyncio.sleep(0.01)


async def _smoke_http_surface(host_url: str, session_id: str) -> None:
    async with httpx.AsyncClient(timeout=3.0) as client:
        host, qr, phone, script = await asyncio.gather(
            client.get(host_url),
            client.get(f"{host_url}/qr.svg"),
            client.get(host_url.replace(f"/host/{session_id}", f"/pair/{session_id}")),
            client.get(host_url.replace(f"/host/{session_id}", "/assets/phone.js")),
        )
    if any(response.status_code != 200 for response in (host, qr, phone, script)):
        raise RemotePrototypeError("prototype HTTP smoke check failed")
    if "Sign in with your phone" not in host.text or "Crunchyroll TV remote" not in phone.text:
        raise RemotePrototypeError("prototype pages failed content verification")


async def _wait_for_end(
    browser: subprocess.Popen,
    cancelled: asyncio.Event,
    timeout_seconds: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        if cancelled.is_set() or browser.poll() is not None:
            return
        if asyncio.get_running_loop().time() >= deadline:
            return
        await asyncio.sleep(0.1)


def _close_browser(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = PrototypeConfiguration.from_args(build_parser().parse_args(argv))
        return asyncio.run(_run(config))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RemotePrototypeError) as exc:
        print(f"Crunchyroll remote failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
