import { applySessionAnswer } from "./connection_lifecycle.mjs";
import {
  createInterruptionState,
  reduceInterruption,
} from "./interruption_state.mjs";

"use strict";

const startButton = document.getElementById("start-button");
const stopButton = document.getElementById("stop-button");
const voiceCard = document.getElementById("voice-card");
const connectionStatus = document.getElementById("connection-status");
const statusText = document.getElementById("status-text");
const errorBanner = document.getElementById("error-banner");
const transcriptList = document.getElementById("transcript-list");
const executionList = document.getElementById("execution-list");
const executionCount = document.getElementById("execution-count");
const eventList = document.getElementById("event-list");
const remoteAudio = document.getElementById("remote-audio");
const launchMode = document.getElementById("launch-mode");
const interruptionStateText = document.getElementById("interruption-state");
const speechSilenceMs = document.getElementById("speech-silence-ms");
const responseCancelMs = document.getElementById("response-cancel-ms");
const truncationMs = document.getElementById("truncation-ms");
const listeningRestoredMs = document.getElementById("listening-restored-ms");

const initialUrl = new URL(window.location.href);
const searchParams = new URLSearchParams(window.location.search);
let activationToken = searchParams.get("activation");
const localSessionId = window.__LOCAL_SESSION_ID__ || null;
if (activationToken) {
  initialUrl.searchParams.delete("activation");
  history.replaceState(null, "", `${initialUrl.pathname}${initialUrl.search}${initialUrl.hash}`);
}

let peerConnection = null;
let dataChannel = null;
let localStream = null;
let openAIRealtimeSessionId = null;
let receivedExecutionCount = 0;
let transportFailureTimer = null;
let isStopping = false;
let controllerSocket = null;
let controllerSessionClosed = false;
let stopMessageSent = false;
let teardownCompleteSent = false;
let interruptionState = null;
const transcriptTurns = new Map();
const responseIdByItemId = new Map();

function sendControlMessage(message) {
  if (
    !controllerSocket ||
    controllerSocket.readyState !== WebSocket.OPEN ||
    !localSessionId ||
    controllerSessionClosed
  ) {
    return false;
  }
  controllerSocket.send(JSON.stringify({ ...message, session_id: localSessionId }));
  return true;
}

function controlTimingData(name, data) {
  if (name === "peer_connection_state" || name === "data_channel_state") {
    return { state: data.state };
  }
  if (name === "webrtc_transport_failure") {
    return { state: data.state };
  }
  if (name === "playback_suppressed") {
    return { suppressed: true, response_id: data.responseId };
  }
  if (name === "next_response_first_audio" || name === "listening_restored") {
    return {
      response_id: data.responseId,
      interrupted_response_id: data.interruptedResponseId,
    };
  }
  if (name === "realtime_response_done") {
    return {
      response_id: data.responseId,
      status: data.status,
      output_types: data.outputTypes,
      suppressed_response_id: data.suppressedResponseId,
      pending_restore_response_id: data.pendingRestoreResponseId,
      remote_audio_muted: data.remoteAudioMuted,
    };
  }
  if (name === "realtime_error") {
    return {
      error_type: data.errorType,
      code: data.code,
      message: data.message,
    };
  }
  return {};
}

function boundedDiagnosticText(value, maxLength = 512) {
  if (typeof value !== "string") return undefined;
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/gu, " ").trim();
  if (!normalized) return undefined;
  return normalized.slice(0, maxLength);
}

function recordRealtimeResponseDone(event) {
  const response = event.response || {};
  if (typeof response.id !== "string" || typeof response.status !== "string") return;
  recordTiming("realtime_response_done", {
    responseId: response.id,
    status: response.status,
    outputTypes: (response.output || [])
      .map((item) => boundedDiagnosticText(item?.type, 64))
      .filter(Boolean)
      .slice(0, 16),
    suppressedResponseId: interruptionState?.suppressedResponseId || undefined,
    pendingRestoreResponseId: interruptionState?.pendingRestoreResponseId || undefined,
    remoteAudioMuted: remoteAudio.muted,
  });
}

function recordRealtimeError(error) {
  recordTiming("realtime_error", {
    errorType: boundedDiagnosticText(error?.type, 128),
    code: boundedDiagnosticText(error?.code, 128),
    message: boundedDiagnosticText(error?.message) || "Unspecified Realtime error",
  });
}

function recordTiming(name, data = {}) {
  const atMs = Number(performance.now().toFixed(2));
  appendEvent({
    type: "timing",
    name,
    at_ms: atMs,
    detail: data,
  });
  const protocolName = name === "webrtc_transport_failure" ? "transport_failure" : name;
  sendControlMessage({
    type: "timing",
    name: protocolName,
    monotonic_ms: atMs,
    data: controlTimingData(name, data),
  });
}

function openControllerSocket() {
  if (!activationToken || !localSessionId) return;

  launchMode.textContent = "Wake activation mode · connecting controller";
  startButton.disabled = true;
  const websocketScheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const controllerUrl = `${websocketScheme}//${window.location.host}` +
    "/control?activation=" + encodeURIComponent(activationToken);
  const socket = new WebSocket(controllerUrl);
  controllerSocket = socket;

  socket.addEventListener("open", () => {
    if (controllerSocket !== socket) return;
    activationToken = null;
    launchMode.textContent = "Wake activation mode · controller connected";
    sendControlMessage({ type: "page_ready" });
    void startConversation();
  });
  socket.addEventListener("message", (message) => {
    if (controllerSocket !== socket) return;
    try {
      const event = JSON.parse(message.data);
      if (event.type === "stop") {
        stopConversation({
          reason: typeof event.reason === "string" ? event.reason : "native_cancel",
          preserveError: true,
          sendStopMessage: false,
        });
        window.setTimeout(() => window.close(), 500);
        return;
      }
      if (event.type === "session_closed") {
        controllerSessionClosed = true;
        stopConversation({ preserveError: true, reason: "native_cancel", sendStopMessage: false });
        socket.close();
        return;
      }
      if (event.type === "action_state") {
        handleActionState(event);
      }
    } catch (_error) {
      failConversation("Controller returned an invalid message");
    }
  });
  socket.addEventListener("error", () => {
    if (controllerSocket === socket && !controllerSessionClosed) {
      failConversation("Could not connect to the local voice controller");
    }
  });
  socket.addEventListener("close", () => {
    if (controllerSocket === socket) {
      controllerSocket = null;
      stopConversation({ preserveError: true, reason: "transport_failure", sendStopMessage: false });
    }
  });
}

function setStatus(state, text) {
  connectionStatus.dataset.state = state;
  statusText.textContent = text;
  voiceCard.dataset.active = String(
    ["connecting", "connected", "listening", "thinking", "responding"].includes(state)
  );
}

function showError(message) {
  errorBanner.textContent = message;
  errorBanner.dataset.visible = "true";
  setStatus("error", "Connection error");
}

function clearError() {
  errorBanner.textContent = "";
  errorBanner.dataset.visible = "false";
}

function clearTransportFailureTimer() {
  if (transportFailureTimer !== null) {
    window.clearTimeout(transportFailureTimer);
    transportFailureTimer = null;
  }
}

function failConversation(message) {
  if (isStopping) return;
  showError(message);
  stopConversation({ preserveError: true, reason: "transport_failure" });
}

function scheduleTransportFailure(pc) {
  clearTransportFailureTimer();
  transportFailureTimer = window.setTimeout(() => {
    transportFailureTimer = null;
    if (peerConnection === pc && pc.connectionState === "disconnected") {
      recordTiming("webrtc_transport_failure", {
        state: pc.connectionState,
      });
      failConversation(`WebRTC ${pc.connectionState}`);
    }
  }, 3000);
}

function clearEmptyState(container) {
  const empty = container.querySelector(".empty");
  if (empty) empty.remove();
}

function ensureTranscriptTurn(role, itemId) {
  const key = `${role}:${itemId}`;
  const existing = transcriptTurns.get(key);
  if (existing) return existing;

  clearEmptyState(transcriptList);
  const row = document.createElement("article");
  row.className = "transcript-turn";
  row.dataset.key = key;
  row.dataset.error = "false";

  const speaker = document.createElement("div");
  speaker.className = "transcript-speaker";
  speaker.textContent = role === "user" ? "You" : "Assistant";

  const text = document.createElement("p");
  text.className = "transcript-text";
  text.textContent = role === "user" ? "Transcribing…" : "Speaking…";
  row.append(speaker, text);
  transcriptList.append(row);

  const turn = { row, text, value: "" };
  transcriptTurns.set(key, turn);
  transcriptList.scrollTop = transcriptList.scrollHeight;

  while (transcriptList.children.length > 40) {
    const oldest = transcriptList.firstElementChild;
    transcriptTurns.delete(oldest.dataset.key);
    oldest.remove();
  }
  return turn;
}

function updateTranscriptTurn(role, itemId, value, options = {}) {
  if (!itemId) return;
  const turn = ensureTranscriptTurn(role, itemId);
  turn.value = options.append ? `${turn.value}${value || ""}` : value || "";
  turn.text.textContent =
    turn.value || (role === "user" ? "No speech detected." : "No transcript received.");
  turn.row.dataset.error = String(Boolean(options.error));
  transcriptList.scrollTop = transcriptList.scrollHeight;
}

function resetTranscript() {
  transcriptTurns.clear();
  const empty = document.createElement("div");
  empty.className = "empty";
  empty.textContent = "Your words and the assistant’s spoken replies will appear here.";
  transcriptList.replaceChildren(empty);
}

function formatJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}

function appendEvent(event) {
  const noisy = event.type === "timing"
    ? false
    : event.type.endsWith(".delta") || event.type === "rate_limits.updated";
  if (noisy) {
    if (event.type === "timing") {
      const markerType = String(event.name);
      if (!markerType) return;
    } else {
      return;
    }
  }

  clearEmptyState(eventList);
  const row = document.createElement("article");
  row.className = "event-row";

  const time = document.createElement("div");
  time.className = "event-time";
  time.textContent = new Date().toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  const body = document.createElement("div");
  const type = document.createElement("div");
  type.className = "event-type";
  type.textContent = event.type;
  const detail = document.createElement("pre");
  detail.textContent = formatJson(summarizeEvent(event));
  body.append(type, detail);
  row.append(time, body);
  eventList.prepend(row);

  while (eventList.children.length > 80) {
    eventList.lastElementChild.remove();
  }
}

function summarizeEvent(event) {
  if (event.type === "response.done") {
    return {
      response_id: event.response?.id,
      status: event.response?.status,
      output_types: (event.response?.output || []).map((item) => item.type),
    };
  }
  if (event.type === "session.created" || event.type === "session.updated") {
    return {
      session_id: event.session?.id,
      model: event.session?.model,
      voice: event.session?.audio?.output?.voice,
    };
  }
  if (event.type === "error") return event.error || event;
  if (event.type === "timing") {
    return {
      at_ms: event.at_ms,
      marker: event.name,
      detail: event.detail,
    };
  }
  return Object.fromEntries(
    Object.entries(event).filter(([key]) => !["type", "event_id"].includes(key)).slice(0, 6)
  );
}

function createActionStateCard(state) {
  clearEmptyState(executionList);
  receivedExecutionCount += 1;
  executionCount.textContent = `${receivedExecutionCount} received`;

  const card = document.createElement("article");
  card.className = "execution";

  const head = document.createElement("div");
  head.className = "execution-head";
  const name = document.createElement("div");
  name.className = "execution-name";
  name.textContent = "Action state";
  const label = document.createElement("div");
  label.className = "execution-state";
  label.textContent = "received";
  head.append(name, label);

  const payload = document.createElement("pre");
  payload.className = "execution-result";
  payload.textContent = formatJson(state);
  card.append(head, payload);
  executionList.prepend(card);
}

function sanitizeActionState(rawState) {
  if (!rawState || typeof rawState !== "object") {
    return rawState;
  }

  const keys = ["type", "call_id", "capability", "execution", "ok", "error", "result"];
  const sanitized = {};
  for (const key of keys) {
    if (key in rawState) {
      sanitized[key] = rawState[key];
    }
  }

  return sanitized;
}

function handleActionState(eventData) {
  const actionState = eventData?.action_state;
  const payload = sanitizeActionState(actionState ?? eventData);
  createActionStateCard(payload);
}

function interruptionContextIsCurrent(sessionContext) {
  return Boolean(
    interruptionState &&
    interruptionState.localSessionId === sessionContext.sessionId &&
    openAIRealtimeSessionId === sessionContext.sessionId &&
    peerConnection === sessionContext.pc &&
    dataChannel === sessionContext.dc
  );
}

function reduceBrowserInterruption(type, fields, sessionContext) {
  if (!interruptionContextIsCurrent(sessionContext)) return [];
  const result = reduceInterruption(interruptionState, {
    type,
    localSessionId: sessionContext.sessionId,
    atMs: Number(performance.now().toFixed(3)),
    ...fields,
  });
  interruptionState = result.state;
  renderInterruptionDiagnostics();
  return result.effects;
}

function renderInterruptionDiagnostics() {
  const trace = interruptionState?.interruption || null;
  if (!trace) {
    interruptionStateText.textContent = "No interruption measured";
    speechSilenceMs.textContent = "—";
    responseCancelMs.textContent = "—";
    truncationMs.textContent = "—";
    listeningRestoredMs.textContent = "—";
    return;
  }

  if (trace.listeningRestoredMs !== null) {
    interruptionStateText.textContent = "Playback restored";
  } else if (trace.playbackSuppressedMs !== null) {
    interruptionStateText.textContent = "Playback suppressed · waiting for next response";
  } else {
    interruptionStateText.textContent = "Speech detected · suppressing playback";
  }
  speechSilenceMs.textContent = formatTimingDelta(trace.speechToSilenceMs);
  responseCancelMs.textContent = formatTimingDelta(
    timingDelta(trace.speechStartedMs, trace.responseCancelledMs),
  );
  truncationMs.textContent = formatTimingDelta(
    timingDelta(trace.speechStartedMs, trace.truncationObservedMs),
  );
  listeningRestoredMs.textContent = formatTimingDelta(
    timingDelta(trace.speechStartedMs, trace.listeningRestoredMs),
  );
}

function timingDelta(startMs, endMs) {
  if (startMs === null || endMs === null || endMs < startMs) return null;
  return Number((endMs - startMs).toFixed(2));
}

function formatTimingDelta(value) {
  return value === null ? "—" : `${value.toFixed(2)} ms`;
}

function handleSpeechStarted(sessionContext) {
  const effects = reduceBrowserInterruption("speech_started", {}, sessionContext);
  for (const effect of effects) {
    if (effect.type === "suppress_playback") {
      suppressInterruptedPlayback(effect, sessionContext);
    }
  }
}

function suppressInterruptedPlayback(effect, sessionContext) {
  if (
    !interruptionContextIsCurrent(sessionContext) ||
    interruptionState.suppressedResponseId !== effect.responseId
  ) {
    return;
  }
  remoteAudio.muted = true;
  remoteAudio.pause();
  reduceBrowserInterruption(
    "playback_suppressed",
    { responseId: effect.responseId },
    sessionContext,
  );
  recordTiming("playback_suppressed", {
    suppressed: true,
    responseId: effect.responseId,
  });
}

async function restorePlaybackForResponse(effect, sessionContext) {
  if (
    !interruptionContextIsCurrent(sessionContext) ||
    interruptionState.restoreInFlightResponseId !== effect.responseId
  ) {
    return;
  }
  recordTiming("next_response_first_audio", {
    responseId: effect.responseId,
    interruptedResponseId: effect.interruptedResponseId,
  });
  remoteAudio.muted = false;
  try {
    await remoteAudio.play();
  } catch (_error) {
    remoteAudio.muted = true;
    showError("Playback could not resume after interruption");
    return;
  }
  if (!interruptionContextIsCurrent(sessionContext)) return;
  reduceBrowserInterruption(
    "listening_restored",
    {
      responseId: effect.responseId,
      interruptedResponseId: effect.interruptedResponseId,
    },
    sessionContext,
  );
  recordTiming("listening_restored", {
    responseId: effect.responseId,
    interruptedResponseId: effect.interruptedResponseId,
  });
}

function handleResponseCreated(event, sessionContext) {
  const responseId = event.response?.id;
  if (!responseId) return;
  reduceBrowserInterruption(
    "response_created",
    { responseId },
    sessionContext,
  );
}

function handleResponseFirstAudio(event, sessionContext) {
  const responseId = event.response_id;
  if (!responseId) return;
  const effects = reduceBrowserInterruption(
    "response_first_audio",
    { responseId },
    sessionContext,
  );
  for (const effect of effects) {
    if (effect.type === "restore_playback") {
      void restorePlaybackForResponse(effect, sessionContext);
    }
  }
}

function handleResponseCancellation(event, sessionContext) {
  const responseId = event.response?.id;
  if (!responseId || event.response?.status !== "cancelled") return;
  reduceBrowserInterruption(
    "response_cancelled",
    { responseId },
    sessionContext,
  );
}

function handleResponseTruncation(event, sessionContext) {
  const responseId = event.response_id || responseIdByItemId.get(event.item_id);
  if (!responseId) return;
  reduceBrowserInterruption(
    "response_truncated",
    { responseId },
    sessionContext,
  );
}


async function handleRealtimeEvent(event, sessionContext) {
  appendEvent(event);

  if (event.type === "conversation.item.input_audio_transcription.delta") {
    updateTranscriptTurn("user", event.item_id, event.delta, { append: true });
  } else if (event.type === "conversation.item.input_audio_transcription.completed") {
    updateTranscriptTurn("user", event.item_id, event.transcript);
  } else if (event.type === "conversation.item.input_audio_transcription.failed") {
    updateTranscriptTurn(
      "user",
      event.item_id,
      `Transcription failed: ${event.error?.message || "unknown error"}`,
      { error: true }
    );
  } else if (event.type === "response.output_audio_transcript.delta") {
    updateTranscriptTurn("assistant", event.item_id, event.delta, { append: true });
    handleResponseFirstAudio(event, sessionContext);
  } else if (event.type === "response.output_audio_transcript.done") {
    updateTranscriptTurn("assistant", event.item_id, event.transcript);
  } else if (event.type === "response.output_item.added") {
    if (event.response_id && event.item?.id) {
      responseIdByItemId.set(event.item.id, event.response_id);
    }
  } else if (event.type === "input_audio_buffer.speech_started") {
    handleSpeechStarted(sessionContext);
    setStatus("listening", "Listening");
  } else if (event.type === "input_audio_buffer.speech_stopped") {
    ensureTranscriptTurn("user", event.item_id);
    setStatus("thinking", "Thinking");
  } else if (event.type === "response.created") {
    handleResponseCreated(event, sessionContext);
    setStatus("responding", "Responding");
  } else if (
    event.type === "response.output_audio.delta" ||
    event.type === "response.output_audio.started"
  ) {
    handleResponseFirstAudio(event, sessionContext);
  } else if (event.type === "conversation.item.truncated") {
    handleResponseTruncation(event, sessionContext);
  } else if (event.type === "response.done") {
    recordRealtimeResponseDone(event);
    handleResponseCancellation(event, sessionContext);
    setStatus("connected", "Connected — speak naturally");
  } else if (event.type === "action_state") {
    handleActionState(event);
  } else if (event.type === "error") {
    recordRealtimeError(event.error);
    showError(event.error?.message || "The Realtime session returned an error.");
  }
}

async function startConversation() {
  clearError();
  resetTranscript();
  stopMessageSent = false;
  teardownCompleteSent = false;
  remoteAudio.muted = false;
  remoteAudio.pause();
  remoteAudio.srcObject = null;
  openAIRealtimeSessionId = null;
  interruptionState = null;
  renderInterruptionDiagnostics();
  startButton.disabled = true;
  setStatus("connecting", "Requesting microphone");

  let sessionId = null;

  try {
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const pc = new RTCPeerConnection();
    peerConnection = pc;
    pc.ontrack = (event) => {
      if (peerConnection !== pc) return;
      remoteAudio.srcObject = event.streams[0];
      remoteAudio.play().catch(() => {});
    };

    for (const track of localStream.getTracks()) {
      pc.addTrack(track, localStream);
    }

    const dc = pc.createDataChannel("oai-events");
    dataChannel = dc;
    dc.addEventListener("statechange", () => {
      if (peerConnection !== pc || dataChannel !== dc) return;
      appendEvent({ type: "webrtc.channel_state", state: dc.readyState });
      recordTiming("data_channel_state", {
        state: dc.readyState,
      });
      if (dc.readyState === "closed" && !isStopping) {
        failConversation("Realtime data channel closed unexpectedly");
      }
    });
    dc.addEventListener("open", () => {
      if (dataChannel !== dc || peerConnection !== pc) return;
      setStatus("connected", "Connected — speak naturally");
    });
    dc.addEventListener("message", (message) => {
      if (dataChannel !== dc || peerConnection !== pc) return;
      try {
        const event = JSON.parse(message.data);
        void handleRealtimeEvent(event, { pc, dc, sessionId });
      } catch (error) {
        showError(`Could not parse a Realtime event: ${error.message}`);
      }
    });
    dc.addEventListener("error", () => {
      if (dataChannel === dc && peerConnection === pc) {
        failConversation("Realtime data channel failed");
      }
    });
    dc.addEventListener("close", () => {
      if (!isStopping && dataChannel === dc && peerConnection === pc) {
        failConversation("Realtime data channel closed unexpectedly");
      }
    });

    pc.addEventListener("connectionstatechange", () => {
      if (peerConnection !== pc) return;
      recordTiming("peer_connection_state", {
        state: pc.connectionState,
      });
      appendEvent({ type: "webrtc.connection_state", state: pc.connectionState });
      if (pc.connectionState === "failed") {
        failConversation(`WebRTC ${pc.connectionState}`);
      } else if (pc.connectionState === "disconnected") {
        scheduleTransportFailure(pc);
      } else {
        clearTransportFailureTimer();
      }
    });

    setStatus("connecting", "Creating Realtime session");
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    recordTiming("sdp_offer_created", {
      offer_type: offer.type,
    });

    const sdpResponse = await fetch("/session", {
      method: "POST",
      body: offer.sdp,
      headers: { "Content-Type": "application/sdp" },
    });
    if (!sdpResponse.ok) {
      let detail = await sdpResponse.text();
      try {
        detail = JSON.parse(detail).detail || detail;
      } catch (_error) {
        // Keep plain-text provider or gateway errors readable.
      }
      throw new Error(detail || `Session gateway returned ${sdpResponse.status}`);
    }

    sessionId = sdpResponse.headers.get("X-OpenAI-Realtime-Session-ID");
    if (!sessionId) {
      throw new Error("Session gateway did not return an OpenAI Realtime session ID");
    }
    openAIRealtimeSessionId = sessionId;
    interruptionState = createInterruptionState(sessionId);
    renderInterruptionDiagnostics();
    const answerApplied = await applySessionAnswer({
      activePeer: () => peerConnection,
      expectedPeer: pc,
      response: sdpResponse,
    });
    if (!answerApplied) return;
    recordTiming("sdp_answer_applied", {
      type: "answer",
    });
    sendControlMessage({ type: "page_started" });
    stopButton.disabled = false;
  } catch (error) {
    showError(error.message || String(error));
    stopConversation({ preserveError: true, reason: "transport_failure" });
  }
}

function stopConversation(options = {}) {
  if (isStopping || teardownCompleteSent) return;
  isStopping = true;
  const shouldSendStopMessage = options.sendStopMessage !== false;

  try {
    if (!stopMessageSent) {
      if (shouldSendStopMessage) {
        sendControlMessage({ type: "stop", reason: options.reason || "button" });
      }
      stopMessageSent = true;
    }

    clearTransportFailureTimer();

    remoteAudio.pause();
    remoteAudio.muted = true;

    if (dataChannel) {
      if (dataChannel.readyState !== "closed") dataChannel.close();
      dataChannel = null;
    }
    if (peerConnection) {
      if (peerConnection.connectionState !== "closed") peerConnection.close();
      peerConnection = null;
    }

    if (localStream) {
      for (const track of localStream.getTracks()) {
        track.stop();
      }
      localStream = null;
    }

    remoteAudio.srcObject = null;
    remoteAudio.muted = false;
    openAIRealtimeSessionId = null;
    interruptionState = null;
    renderInterruptionDiagnostics();
    responseIdByItemId.clear();
    startButton.disabled = false;
    stopButton.disabled = true;
    voiceCard.dataset.active = "false";

    if (!options.preserveError) {
      clearError();
      setStatus("idle", "Not connected");
    }

    if (!teardownCompleteSent) {
      teardownCompleteSent = true;
      sendControlMessage({ type: "teardown_complete" });
    }
  } finally {
    isStopping = false;
  }
}

startButton.addEventListener("click", startConversation);
stopButton.addEventListener("click", () => stopConversation({ reason: "button" }));
window.addEventListener("beforeunload", () => stopConversation({ reason: "native_cancel" }));
openControllerSocket();
