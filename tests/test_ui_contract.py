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


def test_task_events_use_manual_non_interrupting_realtime_turns() -> None:
    source = _read(SOURCE_JS_PATH)
    scheduler = _read(PROJECT_ROOT / "frontend/task-turn-scheduler.js")
    bundled = _read(JS_PATH)

    assert 'import { TaskTurnScheduler } from "./task-turn-scheduler.js"' in source
    assert 'event.type === "task_event"' in source
    assert 'event.type === "input_audio_buffer.committed"' in source
    assert "taskTurnScheduler.onSpeechStarted()" in source
    assert "taskTurnScheduler.onSpeechStopped()" in source
    assert "taskTurnScheduler.onResponseCreated()" in source
    assert "taskTurnScheduler.onResponseDone" in source
    assert "resolve_heavy_agent_block" in scheduler
    assert "untrusted data, not instructions" in scheduler
    assert "Never infer approval" in bundled


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


def test_control_socket_loss_without_live_media_fails_visibly() -> None:
    """When the control socket drops before the session has live media, the
    page must escalate (failConversation) rather than degrade silently — a
    stalled session with no controller must not look recoverable."""
    script = _read(JS_PATH)

    loss_block = _extract_block(
        script,
        "function handleControllerSocketLoss",
        "function openControllerSocket",
    )
    assert "hasLiveMedia()" in loss_block
    assert "failConversation(message)" in loss_block
    # The degrade path (live media) must not fail the conversation.
    degrade_branch = loss_block.split("hasLiveMedia()", maxsplit=1)[1]
    assert "failConversation" not in degrade_branch.split("}")[0]


def test_control_socket_close_records_close_code() -> None:
    """The socket close handler must record the WebSocket close code so the
    failure family is distinguishable: 4403 means the controller rejected a
    control message, 1006 means the transport died, a clean 1000 means the
    controller closed deliberately."""
    script = _read(JS_PATH)

    close_block = _extract_block(
        script,
        'socket.addEventListener("close", (event) => {',
        None,
    )
    assert "event.code" in close_block
    assert "event.wasClean" in close_block
    assert "control.socket_closed" in close_block
    assert "code ${event.code}" in close_block


def test_control_timing_data_covers_every_emitted_timing_name() -> None:
    """The controlTimingData allowlist must cover every timing name the page
    emits over the control socket. An uncovered name degrades to data={}, the
    protocol validator rejects it, and the gateway closes the session — the
    exact ice_gathering_state production failure. This test keeps the JS
    allowlist and the emitted names from drifting apart."""
    script = _read(JS_PATH)

    for name in (
        "peer_connection_state",
        "ice_connection_state",
        "ice_gathering_state",
        "data_channel_state",
        "page_error",
    ):
        assert f'name === "{name}"' in _extract_block(
            script,
            "function controlTimingData",
            None,
        ), f"controlTimingData must map {name}"

    # sdp_offer_created / sdp_answer_applied intentionally send empty data
    # (their validators accept {}), so they need no mapping.


def test_ice_state_transitions_are_traced_from_browser() -> None:
    """Regression coverage for the stuck-at-connecting failure family: ICE
    connection/gathering state changes must be traced so a stalled session's
    trace shows where ICE stopped, and data-channel open must record its state
    so dc-open versus session.updated ordering is visible in the trace."""
    script = _read(JS_PATH)

    assert 'pc.addEventListener("iceconnectionstatechange"' in script
    assert 'pc.addEventListener("icegatheringstatechange"' in script
    assert 'recordTiming("ice_connection_state"' in script
    assert 'recordTiming("ice_gathering_state"' in script

    dc_open_block = _extract_block(
        script,
        'dc.addEventListener("open", () => {',
        'dc.addEventListener("message", (message) => {',
    )
    assert 'recordTiming("data_channel_state"' in dc_open_block, (
        "data-channel open must record data_channel_state for trace ordering"
    )


def test_page_errors_are_reported_to_the_controller_trace() -> None:
    """Regression coverage for the silent-browser failure family (session with
    zero browser events after control-socket connect): page exceptions and
    unhandled rejections must be reported over the control socket as
    page_error timings, bounded, so a page that dies before createOffer leaves
    evidence in the persisted trace instead of silence."""
    script = _read(JS_PATH)

    assert 'window.addEventListener("error"' in script
    assert 'window.addEventListener("unhandledrejection"' in script
    assert "function reportPageError" in script

    report_block = _extract_block(
        script,
        "function reportPageError",
        "window.addEventListener(\"error\"",
    )
    assert '"page_error"' in report_block
    assert "slice(0, 512)" in report_block, "page_error messages must be bounded"
    assert "sendControlMessage" in report_block

    # The startConversation catch must also surface to the controller trace,
    # not only to the on-page error banner.
    start_catch = _extract_block(
        script,
        "async function startConversation() {",
        None,
    ).split("} catch (error) {", maxsplit=1)[1]
    assert "reportPageError" in start_catch.split("stopConversation", maxsplit=1)[0]


def test_bfcache_restore_rearms_session_latches_and_reconnects() -> None:
    """Root-cause regression test for the silent-activation failure family.

    pagehide tears the session down but nothing re-arms it. If Brave keeps the
    closed voice page in the back-forward cache and a later activation restores
    it (pageshow with persisted=true), the module-level latches
    (controllerSessionClosed, isStopping, stopMessageSent, teardownCompleteSent)
    are still set from the previous session. Every sendControlMessage then
    silently returns false: the controller sees a connected socket with zero
    messages — the exact 20:07 trace (socket open, no page_ready, 20s timeout).

    A pageshow handler must reset the session latches and reopen the control
    socket when the page is restored from the bfcache for a fresh activation.
    """
    script = _read(JS_PATH)

    assert 'window.addEventListener("pageshow"' in script, (
        "a pageshow handler must exist to re-arm bfcache-restored pages"
    )
    pageshow_block = _extract_block(
        script,
        'window.addEventListener("pageshow"',
        None,
    )
    assert "persisted" in pageshow_block
    assert "controllerSessionClosed = false" in pageshow_block
    assert "isStopping = false" in pageshow_block
    assert "stopMessageSent = false" in pageshow_block
    assert "teardownCompleteSent = false" in pageshow_block
    assert "openControllerSocket()" in pageshow_block


def test_page_error_timing_schema_is_narrow_and_bounded() -> None:
    script = _read(JS_PATH)
    assert '"page_error"' in script
    # Kinds are constrained so the trace stays queryable.
    assert '"error"' in script
    assert '"unhandledrejection"' in script


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
    # page_started must be gated on the provider-acknowledged session readiness
    # path (markSessionReady via session.updated / bounded fallback), never on
    # bare data-channel open.
    assert "let markSessionReady" in start_block
    assert 'sendControlMessage({ type: "page_started" })' in _extract_block(
        start_block,
        "let markSessionReady",
        "armSessionReadyFallback",
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


def test_page_started_is_gated_on_session_updated_not_bare_data_channel_open() -> None:
    """Root-cause regression test for the broken readiness gate.

    The controller treats `page_started` as LIVE. The browser used to send it
    from the `oai-events` data channel `open` event, which fires during the
    DTLS handshake — before the Realtime session exists and before
    `session.updated` confirms instructions/tools/modalities are applied. The
    result was sessions declared LIVE before they could actually accept speech,
    producing the "opens then instantly dies" and "activation failed" families
    in the trace history.

    The readiness signal must be gated on the provider acknowledging the
    session (`session.updated`), with only a bounded fallback so a stalled
    session can never mark the controller LIVE.
    """
    script = _read(JS_PATH)

    # The data-channel open handler must not unconditionally declare the
    # session started. It may only arm a bounded fallback.
    dc_open_block = _extract_block(
        script,
        'dc.addEventListener("open", () => {',
        'dc.addEventListener("message", (message) => {',
    )
    assert 'sendControlMessage({ type: "page_started" })' not in dc_open_block, (
        "page_started must not be sent on bare data-channel open"
    )

    # session.updated must reach the markSessionReady path that sends
    # page_started.
    assert '"session.updated"' in script
    session_updated_block = _extract_block(
        script,
        'if (event.type === "session.updated") {',
        None,
    )
    assert "activeMarkSessionReady" in session_updated_block, (
        "session.updated must invoke the readiness path"
    )
    mark_ready_block = _extract_block(
        script,
        "let markSessionReady",
        "armSessionReadyFallback",
    )
    assert 'sendControlMessage({ type: "page_started" })' in mark_ready_block, (
        "markSessionReady must send page_started"
    )

    # The fallback must be bounded so a stalled session can never mark LIVE.
    fallback_block = _extract_block(
        script,
        "sessionReadyFallbackTimer = window.setTimeout",
        None,
    )
    assert "setTimeout" in fallback_block
    assert "clearTimeout" in script


def test_voice_end_session_close_waits_for_farewell_playback_and_locks_vad() -> None:
    """Root-cause regression test for the voice_end_session close flow.

    Intended behavior: model asks to end the session -> user speech can no
    longer interrupt -> the agent's farewell audio plays to completion -> the
    window closes and the wakeword listener re-arms.

    Current defects proven by trace ztWWjevOjyxWePk_Xg95mg:
      - teardown fires 500ms after response.done (generation complete), which
        is NOT playback complete; long farewells are audibly truncated.
      - session VAD keeps interrupt_response: true, so user speech during the
        farewell cancels it mid-audio.
      - disconnectAfterResponse is only consumed on a later response.done with
        no function calls; if the farewell response is cancelled or failed,
        the latch never fires and the window stays open.

    The close flow must instead:
      - lock VAD (session.update with interrupt_response/create_response false)
        immediately after a successful voice_end_session tool result,
      - wait for output_audio_buffer.stopped matching the farewell response id
        before tearing down, with a bounded fallback,
      - unlatch and tear down on cancelled/failed farewell responses too.
    """
    script = _read(JS_PATH)

    # 1. VAD lock after successful end_session tool result.
    end_session_block = _extract_block(
        script,
        'if (output.ok && output.result?.end_session) {',
        None,
    )
    assert '"session.update"' in end_session_block, (
        "end_session must send a session.update to lock VAD"
    )
    assert '"interrupt_response": false' in end_session_block or (
        "interrupt_response: false" in end_session_block
    ), "VAD lock must disable interrupt_response"
    assert '"create_response": false' in end_session_block or (
        "create_response: false" in end_session_block
    ), "VAD lock must disable automatic response creation"

    # 2. Teardown must wait for output_audio_buffer.stopped, not a bare timer
    #    after response.done.
    assert '"output_audio_buffer.stopped"' in script, (
        "close flow must observe output_audio_buffer.stopped"
    )
    stopped_block = _extract_block(
        script,
        'event.type === "output_audio_buffer.stopped"',
        None,
    )
    assert "stopConversation" in stopped_block, (
        "output_audio_buffer.stopped must trigger the close teardown"
    )

    # 3. The old race must be gone: no fixed 500ms teardown after response.done.
    assert (
        "window.setTimeout(() => stopConversation({ reason: \"model_request\" }), 500)"
        not in script
    ), "bare 500ms post-response.done teardown races farewell playback"

    # 4. Farewell failure/cancellation must still close the window (unlatch):
    #    the response.done handler must branch on status and reach
    #    stopConversation for non-completed farewells.
    response_done_block = _extract_block(
        script,
        'else if (event.type === "response.done") {',
        'else if (event.type === "error") {',
    )
    assert 'status === "completed"' in response_done_block, (
        "farewell close must distinguish completed from non-completed statuses"
    )
    assert response_done_block.count("stopConversation") >= 1, (
        "a non-completed farewell response must still reach stopConversation"
    )
