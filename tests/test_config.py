from __future__ import annotations

import pytest

from realtime_action_spike.config import DEFAULT_INSTRUCTIONS, Settings


def test_default_instructions_define_voice_to_hermes_routing_policy() -> None:
    instructions = " ".join(DEFAULT_INSTRUCTIONS.casefold().split())

    assert "explicitly asks" in instructions
    assert "kanban" in instructions
    assert "hermes agent" in instructions
    assert "consequential" in instructions
    assert "ask one short confirmation" in instructions
    assert "wait for explicit consent" in instructions
    assert "impossible or unsafe" in instructions
    assert "do not delegate" in instructions
    assert "task argument" in instructions
    assert "routing language" in instructions
    assert "preserve every user constraint" in instructions
    assert "media_prepare_youtube" in instructions
    assert "media_prepare_anime" in instructions
    assert "media_choose" in instructions
    assert "awaiting_choice" in instructions
    assert "its result lists the exact numbered choices" in instructions
    assert "say those option numbers and titles once" in instructions
    assert "source_unavailable" in instructions
    assert "do not retry it or" in instructions
    assert "media_prepare_crunchyroll" in instructions
    assert "explicitly asks for crunchyroll" in instructions
    assert "do not silently use media_prepare_anime" in instructions
    assert "media_decide" in instructions
    assert "interpret conversational replies" in instructions
    assert "yep, that's it" in instructions
    assert "no, play the next episode" in instructions
    assert "strict" in instructions
    assert "rejects without corrected criteria" in instructions
    assert "ask one short follow-up" in instructions
    assert "do not claim playback is audible" in instructions


def test_settings_defaults_for_browser_automation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAVE_BIN", raising=False)
    monkeypatch.delenv("VOICE_BROWSER_PROFILE", raising=False)
    monkeypatch.delenv("VOICE_PAGE_URL", raising=False)
    monkeypatch.delenv("VOICE_BROWSER_START_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("KDE_TASK_NOTIFICATIONS", raising=False)
    monkeypatch.delenv("MPV_BIN", raising=False)
    monkeypatch.delenv("YT_DLP_BIN", raising=False)
    monkeypatch.delenv("YOUTUBE_RESOLVE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("YOUTUBE_BROWSER_COOKIES", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    settings = Settings.from_env()

    assert settings.brave_bin == "brave-origin"
    assert settings.voice_browser_profile == "~/.local/share/okay-hermes-realtime/brave-profile"
    assert settings.voice_page_url == "http://127.0.0.1:8765/voice"
    assert settings.voice_browser_start_timeout_seconds == 20.0
    assert settings.kde_task_notifications is True
    assert settings.mpv_bin == "mpv"
    assert settings.yt_dlp_bin == "yt-dlp"
    assert settings.youtube_resolve_timeout_seconds == 12.0
    assert settings.youtube_browser_cookies is None
    assert settings.resolved_activation_socket_path.startswith("/run/user/")
    assert settings.resolved_activation_socket_path.endswith(
        "/okay-hermes-realtime/activation.sock"
    )


def test_settings_reads_custom_browser_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_BIN", "/tmp/brave-nightly")
    monkeypatch.setenv("VOICE_BROWSER_PROFILE", "/tmp/custom-profile")
    monkeypatch.setenv("VOICE_PAGE_URL", "http://127.0.0.1:9000/voice")
    monkeypatch.setenv("VOICE_BROWSER_START_TIMEOUT_SECONDS", "12.75")
    monkeypatch.setenv("KDE_TASK_NOTIFICATIONS", "false")
    monkeypatch.setenv("MPV_BIN", "/tmp/mpv")
    monkeypatch.setenv("YT_DLP_BIN", "/tmp/yt-dlp")
    monkeypatch.setenv("YOUTUBE_RESOLVE_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("YOUTUBE_BROWSER_COOKIES", "brave:Profile 1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/xdg-runtime-custom")

    settings = Settings.from_env()

    assert settings.brave_bin == "/tmp/brave-nightly"
    assert settings.voice_browser_profile == "/tmp/custom-profile"
    assert settings.voice_page_url == "http://127.0.0.1:9000/voice"
    assert settings.voice_browser_start_timeout_seconds == 12.75
    assert settings.kde_task_notifications is False
    assert settings.mpv_bin == "/tmp/mpv"
    assert settings.yt_dlp_bin == "/tmp/yt-dlp"
    assert settings.youtube_resolve_timeout_seconds == 7.5
    assert settings.youtube_browser_cookies == "brave:Profile 1"


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
