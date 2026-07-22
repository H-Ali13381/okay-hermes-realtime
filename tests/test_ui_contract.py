from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from realtime_action_spike.config import Settings
from realtime_action_spike.gateway import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/index.html"
CSS_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/voice.css"
JS_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/voice.js"
INTERRUPTION_JS_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/interruption_state.mjs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _client() -> TestClient:
    return TestClient(create_app(Settings(openai_api_key=SecretStr("test-secret-key"))))


def test_voice_route_serves_visible_html() -> None:
    response = _client().get("/voice")

    assert response.status_code == 200
    assert response.text.startswith("<!doctype html>")
    assert "Start conversation" in response.text
    assert "data-testid" not in response.text


def test_assets_routes_serve_voice_css_and_js() -> None:
    client = _client()

    css = client.get("/assets/voice.css")
    js = client.get("/assets/voice.js")
    interruption_js = client.get("/assets/interruption_state.mjs")

    assert css.status_code == 200
    assert css.text
    assert css.headers["content-type"].startswith("text/css")

    assert js.status_code == 200
    assert js.text
    assert js.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert interruption_js.status_code == 200
    assert interruption_js.headers["content-type"].startswith(
        ("text/javascript", "application/javascript")
    )


def test_page_loads_static_assets_via_relative_routes() -> None:
    response = _client().get("/voice")
    html = response.text

    assert '<link rel="stylesheet" href="/assets/voice.css"' in html
    assert '<script type="module" src="/assets/voice.js"></script>' in html


def test_page_retains_dom_controls_and_panels() -> None:
    html = _read(INDEX_PATH)

    assert 'id="connection-status"' in html
    assert "id=\"transcript-list\"" in html
    assert 'id="execution-list"' in html
    assert 'id="execution-count"' in html
    assert 'id="event-list"' in html
    assert 'id="remote-audio"' in html
    assert 'Start conversation' in html
    assert 'Stop' in html
    assert 'id="launch-mode"' in html


def test_assets_are_split_files() -> None:
    index_html = _read(INDEX_PATH)
    script_js = _read(JS_PATH)
    css = _read(CSS_PATH)

    assert "<style>" not in index_html
    assert "<script>" not in index_html
    assert "new RTCPeerConnection()" in script_js
    assert "oai-events" in script_js
    assert ".voice-card" in css


def test_js_preserves_webrtc_relay_and_function_execution_paths() -> None:
    script = _read(JS_PATH)

    assert "navigator.mediaDevices.getUserMedia" in script
    assert "new RTCPeerConnection()" in script
    assert 'pc.createDataChannel("oai-events")' in script
    assert "pc.createOffer()" in script
    assert "pc.setLocalDescription" in script
    assert "pc.setRemoteDescription" in script
    assert 'fetch("/session"' in script
    assert 'headers: { "Content-Type": "application/sdp" }' in script
    assert 'fetch("/execute"' in script
    assert "conversation.item.create" in script
    assert "function_call_output" in script


def test_js_keeps_same_origin_and_session_state_guards() -> None:
    script = _read(JS_PATH)

    assert "GATEWAY_ORIGIN" not in script
    assert "__GATEWAY_ORIGIN__" not in script
    assert "peerConnection !== sessionContext.pc" in script
    assert "dataChannel !== sessionContext.dc" in script
    assert "openAIRealtimeSessionId !== sessionContext.sessionId" in script
    assert "/session" in script
    assert "/execute" in script


def test_js_has_sanitized_timing_markers() -> None:
    script = _read(JS_PATH)

    assert "function recordTiming" in script
    assert "performance.now()" in script
    for marker in [
        "peer_connection_state",
        "data_channel_state",
        "sdp_offer_created",
        "sdp_answer_applied",
        "webrtc_transport_failure",
    ]:
        assert f'"{marker}"' in script

    assert "event.type === \"timing\"" in script
    assert "OPENAI_API_KEY" not in script
    assert "api.openai.com" not in script
    assert "Authorization" not in script
    assert "Bearer " not in script


def test_ui_contract_keeps_cleanup_guards() -> None:
    script = _read(JS_PATH)

    assert "track.stop()" in script
    assert "dataChannel.close()" in script
    assert "peerConnection.close()" in script
    assert "remoteAudio.pause()" in script
    assert "remoteAudio.srcObject = null" in script
    assert "beforeunload" in script
    assert "appendEvent({" in script


def test_activation_mode_uses_same_origin_control_websocket_and_auto_start() -> None:
    script = _read(JS_PATH)
    html = _read(INDEX_PATH)

    assert "new URLSearchParams(window.location.search)" in script
    assert 'searchParams.get("activation")' in script
    assert "history.replaceState" in script
    assert "new WebSocket" in script
    assert '"/control?activation="' in script
    assert 'type: "page_ready"' in script
    assert 'type: "page_started"' in script
    assert "startConversation()" in script
    assert 'type: "stop"' in script
    assert 'type: "teardown_complete"' in script
    assert 'event.type === "session_closed"' in script
    assert "Manual diagnostic mode" in html


def test_controller_messages_never_include_raw_sdp_or_provider_credentials() -> None:
    script = _read(JS_PATH)

    assert "controllerSocket.send" in script
    assert 'name === "webrtc_transport_failure" ? "transport_failure"' in script
    control_sender = script.split("function sendControlMessage", maxsplit=1)[1].split(
        "function recordTiming", maxsplit=1
    )[0]
    assert "sdp" not in control_sender.lower()
    assert "api.openai.com" not in script
    assert "Authorization" not in script


def test_interruption_ui_uses_pure_reducer_and_explicit_response_guards() -> None:
    script = _read(JS_PATH)
    reducer = _read(INTERRUPTION_JS_PATH)
    html = _read(INDEX_PATH)

    assert 'from "./interruption_state.mjs"' in script
    assert "function handleSpeechStarted" in script
    assert "function suppressInterruptedPlayback" in script
    assert "function restorePlaybackForResponse" in script
    assert '"input_audio_buffer.speech_started"' in script
    assert '"response.output_audio.delta"' in script
    assert '"response.output_audio.started"' in script
    assert '"playback_suppressed"' in script
    assert '"next_response_first_audio"' in script
    assert '"listening_restored"' in script
    assert "responseId" in reducer
    assert "localSessionId" in reducer
    assert "pendingRestoreResponseId" in reducer

    suppression = script.split("function suppressInterruptedPlayback", maxsplit=1)[1].split(
        "function restorePlaybackForResponse", maxsplit=1
    )[0]
    assert "remoteAudio.pause()" in suppression
    assert "remoteAudio.muted = true" in suppression
    assert "track.stop()" not in suppression

    assert 'id="interruption-diagnostics"' in html
    assert 'id="interruption-state"' in html
    assert 'id="speech-silence-ms"' in html
    assert '<details id="interruption-diagnostics"' in html
