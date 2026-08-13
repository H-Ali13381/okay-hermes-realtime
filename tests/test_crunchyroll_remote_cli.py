from __future__ import annotations

from pathlib import Path

import pytest

from realtime_action_spike.crunchyroll_remote.cli import (
    PrototypeConfiguration,
    _listening_sockets,
    _require_unused_loopback_port,
    build_parser,
    choose_lan_ipv4,
)


def test_choose_lan_ipv4_uses_route_probe_and_rejects_loopback() -> None:
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def connect(self, address: tuple[str, int]) -> None:
            assert address == ("1.1.1.1", 80)

        def getsockname(self) -> tuple[str, int]:
            return ("192.168.50.12", 40000)

    assert choose_lan_ipv4(socket_factory=lambda *_args: FakeSocket()) == "192.168.50.12"


def test_choose_lan_ipv4_requires_private_non_loopback_address() -> None:
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def connect(self, _address: tuple[str, int]) -> None:
            return None

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 40000)

    with pytest.raises(ValueError, match="private LAN"):
        choose_lan_ipv4(socket_factory=lambda *_args: FakeSocket())


def test_cli_defaults_to_isolated_profile_and_installed_brave_candidate() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    config = PrototypeConfiguration.from_args(args)

    assert (
        config.profile_path
        == Path("~/.local/share/okay-hermes-realtime/crunchyroll-brave-profile").expanduser()
    )
    assert config.timeout_seconds == 300
    assert config.brave_binary
    assert config.smoke is False


def test_cli_accepts_explicit_smoke_configuration(tmp_path: Path) -> None:
    config = PrototypeConfiguration.from_args(
        build_parser().parse_args(
            [
                "--smoke",
                "--lan-host",
                "192.168.50.12",
                "--port",
                "49152",
                "--debug-port",
                "49200",
                "--profile",
                str(tmp_path / "profile"),
                "--timeout",
                "10",
            ]
        )
    )

    assert config.smoke is True
    assert config.lan_host == "192.168.50.12"
    assert config.port == 49152
    assert config.debug_port == 49200
    assert config.timeout_seconds == 10


def test_require_unused_loopback_port_rejects_an_existing_listener() -> None:
    listener = __import__("socket").socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        with pytest.raises(ValueError, match="already in use"):
            _require_unused_loopback_port(port)
    finally:
        listener.close()


def test_listening_sockets_bind_only_selected_lan_and_loopback() -> None:
    sockets, port = _listening_sockets("127.0.0.2", 0)
    try:
        assert port > 0
        assert {sock.getsockname() for sock in sockets} == {
            ("127.0.0.2", port),
            ("127.0.0.1", port),
        }
    finally:
        for sock in sockets:
            sock.close()
