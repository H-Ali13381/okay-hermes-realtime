from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PANEL_PATH = PROJECT_ROOT / "src/realtime_action_spike/web/realtime_panel.html"
APP_PATH = PROJECT_ROOT / "src/realtime_action_spike/streamlit_app.py"


def read_panel() -> str:
    return PANEL_PATH.read_text(encoding="utf-8")


def test_panel_has_persistent_conversation_controls_and_inspectors() -> None:
    panel = read_panel()

    assert 'id="start-button"' in panel
    assert "Start conversation" in panel
    assert 'id="stop-button"' in panel
    assert 'id="connection-status"' in panel
    assert 'id="execution-list"' in panel
    assert 'id="event-list"' in panel


def test_panel_uses_browser_webrtc_and_server_sdp_relay() -> None:
    panel = read_panel()

    assert "new RTCPeerConnection()" in panel
    assert "navigator.mediaDevices.getUserMedia" in panel
    assert 'pc.createDataChannel("oai-events")' in panel
    assert "pc.addTrack" in panel
    assert "pc.createOffer()" in panel
    assert "pc.setLocalDescription" in panel
    assert "`${GATEWAY_ORIGIN}/session`" in panel
    assert '"Content-Type": "application/sdp"' in panel
    assert "pc.setRemoteDescription" in panel
    assert "remoteAudio.srcObject" in panel


def test_panel_executes_and_returns_realtime_function_calls() -> None:
    panel = read_panel()

    assert 'event.type === "response.done"' in panel
    assert 'item.type === "function_call"' in panel
    assert "handledCallIds" in panel
    assert "`${GATEWAY_ORIGIN}/execute`" in panel
    assert 'type: "conversation.item.create"' in panel
    assert 'type: "function_call_output"' in panel
    assert 'type: "response.create"' in panel
    assert "call_id" in panel
    assert "arguments" in panel


def test_panel_cleans_up_microphone_data_channel_and_peer_connection() -> None:
    panel = read_panel()

    assert "track.stop()" in panel
    assert "dataChannel.close()" in panel
    assert "peerConnection.close()" in panel
    assert "remoteAudio.srcObject = null" in panel
    assert "beforeunload" in panel


def test_panel_never_embeds_permanent_openai_credentials() -> None:
    panel = read_panel()

    assert "OPENAI_API_KEY" not in panel
    assert "api.openai.com" not in panel
    assert "Bearer " not in panel


def test_streamlit_app_embeds_the_panel_with_loopback_gateway_origin() -> None:
    app = APP_PATH.read_text(encoding="utf-8")

    assert "st.set_page_config" in app
    assert "components.html" in app
    assert "realtime_panel.html" in app
    assert "__GATEWAY_ORIGIN__" in app
    assert "settings.gateway_host" in app
    assert "settings.gateway_port" in app
