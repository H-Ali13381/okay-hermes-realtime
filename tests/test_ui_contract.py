from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from realtime_action_spike.config import Settings
from realtime_action_spike.gateway import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/index.html"
CSS_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/voice.css"
JS_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/voice.js"
SOURCE_JS_PATH = PROJECT_ROOT / "frontend/voice.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_block(source: str, start: str, end: str | None = None) -> str:
    start_index = source.index(start)
    if end is None:
        return source[start_index:]
    end_index = source.index(end, start_index + len(start))
    return source[start_index:end_index]


def _assert_order(body: str, snippets: list[str]) -> None:
    positions = [body.index(snippet) for snippet in snippets]
    assert positions == sorted(positions)


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

    assert css.status_code == 200
    assert css.text
    assert css.headers["content-type"].startswith("text/css")

    assert js.status_code == 200
    assert js.text
    assert js.headers["content-type"].startswith(("text/javascript", "application/javascript"))


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


def test_package_metadata_has_no_agents_sdk_dependencies() -> None:
    package = json.loads((PROJECT_ROOT / "package.json").read_text(encoding="utf-8"))
    dependencies = package.get("dependencies", {})

    assert "@openai/agents-realtime" not in dependencies
    assert "zod" not in dependencies


def test_assets_are_split_files() -> None:
    index_html = _read(INDEX_PATH)
    script_js = _read(JS_PATH)
    css = _read(CSS_PATH)

    assert "<style>" not in index_html
    assert "<script>" not in index_html
    assert "new RTCPeerConnection" in script_js
    assert "oai-events" in script_js
    assert ".voice-card" in css


def test_js_uses_direct_realtime_webrtc_transport_and_scoped_tools() -> None:
    script = _read(JS_PATH)
    start_block = _extract_block(
        script,
        "async function startConversation() {",
        "function stopConversation(options = {}) {",
    )

    assert "navigator.mediaDevices.getUserMedia" in script
    assert "new RTCPeerConnection" in start_block
    assert 'createDataChannel("oai-events")' in start_block
    assert "fetch(sessionUrl" in script
    assert "`/session?local_session_id=${encodeURIComponent(localSessionId)}`" in script
    assert '"Content-Type": "application/sdp"' in script
    assert 'X-Okay-Hermes-Execution-Scope' in script
    assert 'fetch("/execute"' in script
    assert '"X-Okay-Hermes-Client": "voice-page-v1"' in script
    assert 'type: "response.create"' in script
    assert "conversation.item.create" in script
    assert "function_call_output" in script
    assert "executeFunctionCall" in script
    assert "OpenAIRealtimeWebRTC" not in script
    assert 'fetch("/client-secret"' not in script


def test_js_gives_transient_webrtc_disconnect_three_second_grace() -> None:
    script = _read(JS_PATH)
    source = _read(PROJECT_ROOT / "frontend/voice.js")

    assert "function scheduleTransportFailure" in script
    assert "window.setTimeout" in script
    assert "}, 3000);" in source
    assert "function clearTransportFailureTimer" in script
    assert 'pc.connectionState === "disconnected"' in script
    assert "scheduleTransportFailure(pc)" in script
    assert "clearTransportFailureTimer()" in script


def test_control_socket_loss_degrades_after_media_is_live() -> None:
    script = _read(JS_PATH)

    assert "function hasLiveMedia" in script
    assert "function handleControllerSocketLoss" in script
    assert 'dataChannel?.readyState === "open"' in script
    assert "controller unavailable" in script
    assert "stopConversation" not in _extract_block(
        script,
        "function handleControllerSocketLoss",
        "function openControllerSocket",
    )


def test_js_keeps_same_origin_and_session_state_guards() -> None:
    script = _read(JS_PATH)

    assert "GATEWAY_ORIGIN" not in script
    assert "__GATEWAY_ORIGIN__" not in script
    assert "peerConnection" in script
    assert "dataChannel" in script
    assert 'headers["X-Okay-Hermes-Session-ID"] = localSessionId' in script


def test_js_has_sanitized_timing_markers() -> None:
    script = _read(JS_PATH)
    app_timing = _extract_block(
        script,
        "function controlTimingData(name, data) {",
        "function boundedDiagnosticText",
    )

    assert "function recordTiming" in script
    assert "performance.now()" in script
    for marker in [
        "peer_connection_state",
        "realtime_response_done",
        "realtime_error",
    ]:
        assert f'"{marker}"' in script

    assert "event.type === \"timing\"" in script
    assert "OPENAI_API_KEY" not in app_timing
    assert "Bearer " not in app_timing


def test_ui_contract_keeps_cleanup_guards() -> None:
    script = _read(JS_PATH)

    assert "track.stop()" in script
    assert "dataChannel.close()" in script
    assert "peerConnection.close()" in script
    assert "remoteAudio.srcObject = null" in script
    assert "remoteAudio.pause()" not in script
    assert "remoteAudio.muted = true" not in script
    assert "beforeunload" in script
    # X / window-close must fire a best-effort teardown even when beforeunload is
    # unreliable (bfcache, background kill), so pagehide is registered too.
    assert "pagehide" in script
    assert "appendEvent({" in script


def test_event_diagnostics_redact_embedded_audio_before_rendering() -> None:
    script = _read(JS_PATH)

    assert "function sanitizeDiagnosticValue" in script
    assert 'key === "audio"' in script
    assert "sanitizeDiagnosticValue(event.item)" in script


def test_activation_mode_uses_same_origin_control_websocket_and_auto_start() -> None:
    script = _read(JS_PATH)
    html = _read(INDEX_PATH)
    start_block = _extract_block(
        script,
        "async function startConversation() {",
        "function stopConversation(options = {}) {",
    )

    assert "new URLSearchParams(window.location.search)" in script
    assert 'searchParams.get("activation")' in script
    assert "history.replaceState" in script
    assert "new WebSocket" in script
    assert "/control?activation=" in script
    assert 'type: "page_ready"' in script
    assert 'type: "realtime_connected"' not in script
    assert "provider_call_id: providerCallId" not in script
    assert 'type: "page_started"' in script
    assert start_block.index('createDataChannel("oai-events")') < start_block.index(
        'sendControlMessage({ type: "page_started" })'
    )
    assert "startConversation()" in script
    assert 'type: "stop"' in script
    assert 'type: "teardown_complete"' in script
    assert 'event.type === "session_closed"' in script
    assert "Manual diagnostic mode" in html


def test_controller_messages_never_include_raw_sdp_or_provider_credentials() -> None:
    script = _read(JS_PATH)

    assert "controllerSocket.send" in script
    control_sender = script.split("function sendControlMessage", maxsplit=1)[1].split(
        "function controlTimingData", maxsplit=1
    )[0]
    assert "sdp" not in control_sender.lower()
    assert "clientSecret" not in control_sender
    assert "activationToken" not in control_sender


def test_late_sdp_answer_is_ignored_after_peer_teardown() -> None:
    script = _read(JS_PATH)
    source_script = _read(SOURCE_JS_PATH)

    assert 'from "./peer-lifecycle.js"' in source_script
    answer_block = script.split(
        'const answer = { type: "answer", sdp: await response.text() };',
        maxsplit=1,
    )[1].split('recordTiming("sdp_answer_applied")', maxsplit=1)[0]
    assert "canApplyRemoteAnswer(peerConnection, pc)" in answer_block
    assert "webrtc.late_sdp_answer_ignored" in answer_block
    assert answer_block.index("canApplyRemoteAnswer") < answer_block.index(
        "pc.setRemoteDescription(answer)"
    )
    assert answer_block.index("canApplyRemoteAnswer") < answer_block.index(
        "executionScope = response.headers.get"
    )


def test_interruption_ui_observes_sdk_events_without_owning_playback() -> None:
    script = _read(JS_PATH)
    html = _read(INDEX_PATH)

    assert "function observeSpeechStarted" in script
    assert "function observeResponseCancellation" in script
    assert "function observeOutputBufferCleared" in script
    assert "function observeResponseFirstAudio" in script
    assert '"input_audio_buffer.speech_started"' in script
    assert '"response.output_audio.delta"' in script
    assert '"output_audio_buffer.cleared"' in script
    assert "remoteAudio.pause()" not in script
    assert "remoteAudio.muted = true" not in script

    assert 'id="interruption-diagnostics"' in html
    assert 'id="interruption-state"' in html
    assert 'id="speech-silence-ms"' in html
    assert '<details id="interruption-diagnostics"' in html


def test_audio_transcript_delta_only_observes_sdk_playback_restoration() -> None:
    script = _read(JS_PATH)
    transcript_delta_branch = _extract_block(
        script,
        'if (event.type === "response.output_audio_transcript.delta") {',
        '} else if (event.type === "response.output_audio_transcript.done") {',
    )

    assert "updateTranscriptTurn" in transcript_delta_branch
    assert "observeResponseFirstAudio()" in transcript_delta_branch
    assert "remoteAudio" not in transcript_delta_branch


def test_js_stop_message_from_controller_invokes_local_teardown_once() -> None:
    script = _read(JS_PATH)

    assert "if (event.type === \"stop\")" in script
    assert "sendStopMessage: false" in script
    assert "stopConversation({" in script
    assert "sendControlMessage({ type: \"stop\"" in script
    assert "isStopping || teardownCompleteSent" in script

    stop_event_block = _extract_block(
        script,
        "if (event.type === \"stop\") {",
        "if (event.type === \"session_closed\") {",
    )
    assert "sendStopMessage: false" in stop_event_block
    assert "stopConversation({" in stop_event_block
    assert 'window.setTimeout(() => window.close(), 500);' in stop_event_block
    _assert_order(
        stop_event_block,
        ["stopConversation({", "window.setTimeout(() => window.close(), 500);"],
    )


def test_js_teardown_order_is_transport_then_tracks_then_ack() -> None:
    script = _read(JS_PATH)

    stop_block = _extract_block(
        script,
        "function stopConversation(options = {}) {",
        "window.addEventListener(\"beforeunload\"",
    )

    assert "dataChannel.close();" in stop_block
    assert "peerConnection.close();" in stop_block
    assert "for (const track of localStream.getTracks())" in stop_block
    assert "track.stop();" in stop_block
    assert 'sendControlMessage({ type: "teardown_complete"' in stop_block

    _assert_order(
        stop_block,
        [
            "dataChannel.close();",
            "peerConnection.close();",
            "for (const track of localStream.getTracks())",
            'sendControlMessage({ type: "teardown_complete"',
        ],
    )
