from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from realtime_action_spike.config import Settings, build_realtime_session
from realtime_action_spike.gateway import create_app
from realtime_action_spike.providers import (
    UnknownVoiceProviderError,
    get_provider_contract,
    provider_contract_snapshot,
)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"openai_api_key": SecretStr("sk-test")}
    values.update(overrides)
    return Settings.model_validate(values)


def test_provider_contracts_are_explicit_and_provider_fixed() -> None:
    openai = get_provider_contract("openai")
    gemini = get_provider_contract("GEMINI")

    assert openai.id == "openai"
    assert openai.transport == "webrtc"
    assert openai.availability == "active"
    assert openai.model == "gpt-realtime-2.1"

    assert gemini.id == "gemini"
    assert gemini.transport == "gemini_live_websocket"
    assert gemini.availability == "contract_only"

    snapshot = provider_contract_snapshot()
    assert {entry["id"] for entry in snapshot} == {"openai", "gemini"}
    assert all("api_key" not in entry and "credential" not in entry for entry in snapshot)

    with pytest.raises(UnknownVoiceProviderError):
        get_provider_contract("anthropic")


def test_settings_select_provider_between_sessions_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("realtime_action_spike.config.load_dotenv", lambda: None)
    monkeypatch.delenv("VOICE_PROVIDER", raising=False)
    monkeypatch.delenv("REALTIME_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/xdg-runtime-provider-test")

    default = Settings.from_env()
    assert default.voice_provider == "openai"
    assert default.realtime_model == "gpt-realtime-2.1"

    monkeypatch.setenv("VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-live")
    selected = Settings.from_env()
    assert selected.voice_provider == "gemini"
    assert selected.gemini_model == "gemini-3.8-live"
    with pytest.raises(ValueError, match="voice_provider"):
        Settings.model_validate({"voice_provider": "unknown"})


def test_session_contract_uses_selected_openai_model() -> None:
    session = build_realtime_session(settings())
    assert session["model"] == "gpt-realtime-2.1"


def test_contract_only_gemini_selection_fails_explicitly() -> None:
    app = create_app(settings(voice_provider="gemini"))
    client = TestClient(app)
    response = client.post(
        "/session",
        content="v=0\r\nmock-offer",
        headers={"Content-Type": "application/sdp"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Gemini Live transport is contract-only in this build"
    )


def test_health_reports_provider_identity_without_credentials() -> None:
    client = TestClient(create_app(settings()))
    payload = client.get("/health").json()
    assert payload["provider"] == "openai"
    assert payload["provider_model"] == "gpt-realtime-2.1"
    assert "api_key" not in payload
