from __future__ import annotations

import pytest

from realtime_action_spike.config import Settings


def test_settings_defaults_for_browser_automation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAVE_BIN", raising=False)
    monkeypatch.delenv("VOICE_BROWSER_PROFILE", raising=False)
    monkeypatch.delenv("VOICE_PAGE_URL", raising=False)
    monkeypatch.delenv("VOICE_BROWSER_START_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    settings = Settings.from_env()

    assert settings.brave_bin == "/usr/bin/brave-origin-nightly"
    assert settings.voice_browser_profile == "~/.local/share/okay-hermes-realtime/brave-profile"
    assert settings.voice_page_url == "http://127.0.0.1:8765/voice"
    assert settings.voice_browser_start_timeout_seconds == 10.0
    assert settings.resolved_activation_socket_path.startswith("/run/user/")
    assert settings.resolved_activation_socket_path.endswith(
        "/okay-hermes-realtime/activation.sock"
    )


def test_settings_reads_custom_browser_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_BIN", "/tmp/brave-nightly")
    monkeypatch.setenv("VOICE_BROWSER_PROFILE", "/tmp/custom-profile")
    monkeypatch.setenv("VOICE_PAGE_URL", "http://127.0.0.1:9000/voice")
    monkeypatch.setenv("VOICE_BROWSER_START_TIMEOUT_SECONDS", "12.75")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/xdg-runtime-custom")

    settings = Settings.from_env()

    assert settings.brave_bin == "/tmp/brave-nightly"
    assert settings.voice_browser_profile == "/tmp/custom-profile"
    assert settings.voice_page_url == "http://127.0.0.1:9000/voice"
    assert settings.voice_browser_start_timeout_seconds == 12.75


def test_settings_rejects_non_loopback_voice_page_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_PAGE_URL", "http://192.168.1.1:8765/voice")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/xdg-runtime-invalid")

    with pytest.raises(ValueError, match="loopback"):
        Settings.from_env()


def test_settings_rejects_relative_activation_socket_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACTIVATION_SOCKET_PATH", "relative/activation.sock")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/xdg-runtime-invalid")

    with pytest.raises(ValueError, match="absolute"):
        Settings.from_env()
