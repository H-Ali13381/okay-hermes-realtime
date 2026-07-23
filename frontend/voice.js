import { OpenAIRealtimeWebRTC } from "@openai/agents-realtime";

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

let realtimeTransport = null;
let localStream = null;
let receivedExecutionCount = 0;
let isStopping = false;
let controllerSocket = null;
let controllerSessionClosed = false;
let stopMessageSent = false;
let teardownCompleteSent = false;
let interruptionStartedMs = null;
let waitingForNextAudio = false;
const transcriptTurns = new Map();

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
  if (name === "peer_connection_state") {
    return { state: data.state };
  }
  if (name === "realtime_response_done") {
    return {
      response_id: data.responseId,
      status: data.status,
      output_types: data.outputTypes,
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
    outputTypes: (Array.isArray(response.output) ? response.output : [])
      .map((item) => boundedDiagnosticText(item?.type, 64))
      .filter(Boolean)
      .slice(0, 16),
    remoteAudioMuted: false,
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
  sendControlMessage({
    type: "timing",
    name,
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

function failConversation(message) {
  if (isStopping) return;
  showError(message);
  stopConversation({ preserveError: true, reason: "transport_failure" });
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

function resetSdkInterruptionDiagnostics() {
  interruptionStartedMs = null;
  waitingForNextAudio = false;
  interruptionStateText.textContent = "No interruption measured";
  speechSilenceMs.textContent = "—";
  responseCancelMs.textContent = "—";
  truncationMs.textContent = "—";
  listeningRestoredMs.textContent = "—";
}

function elapsedSinceInterruption() {
  if (interruptionStartedMs === null) return null;
  return Number((performance.now() - interruptionStartedMs).toFixed(2));
}

function formatSdkTiming(value) {
  return value === null ? "—" : `${value.toFixed(2)} ms`;
}

function observeSpeechStarted() {
  interruptionStartedMs = performance.now();
  waitingForNextAudio = true;
  interruptionStateText.textContent = "Speech detected · SDK interruption active";
  speechSilenceMs.textContent = "0.00 ms";
  responseCancelMs.textContent = "—";
  truncationMs.textContent = "—";
  listeningRestoredMs.textContent = "—";
}

function observeResponseCancellation(event) {
  if (event.response?.status !== "cancelled" || interruptionStartedMs === null) return;
  responseCancelMs.textContent = formatSdkTiming(elapsedSinceInterruption());
  interruptionStateText.textContent = "Response cancelled · waiting for next response";
}

function observeOutputBufferCleared() {
  if (interruptionStartedMs === null) return;
  truncationMs.textContent = formatSdkTiming(elapsedSinceInterruption());
  interruptionStateText.textContent = "Output buffer cleared · listening";
}

function observeResponseFirstAudio() {
  if (!waitingForNextAudio || interruptionStartedMs === null) return;
  listeningRestoredMs.textContent = formatSdkTiming(elapsedSinceInterruption());
  interruptionStateText.textContent = "Playback restored by SDK";
  waitingForNextAudio = false;
}

async function handleRealtimeEvent(event) {
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
    observeResponseFirstAudio();
  } else if (event.type === "response.output_audio_transcript.done") {
    updateTranscriptTurn("assistant", event.item_id, event.transcript);
  } else if (event.type === "input_audio_buffer.speech_started") {
    observeSpeechStarted();
    setStatus("listening", "Listening");
  } else if (event.type === "input_audio_buffer.speech_stopped") {
    ensureTranscriptTurn("user", event.item_id);
    setStatus("thinking", "Thinking");
  } else if (event.type === "response.created") {
    setStatus("responding", "Responding");
  } else if (
    event.type === "response.output_audio.delta" ||
    event.type === "response.output_audio.started"
  ) {
    observeResponseFirstAudio();
  } else if (event.type === "output_audio_buffer.cleared") {
    observeOutputBufferCleared();
  } else if (event.type === "response.done") {
    recordRealtimeResponseDone(event);
    observeResponseCancellation(event);
    setStatus("connected", "Connected — speak naturally");
  } else if (event.type === "error") {
    recordRealtimeError(event.error);
    showError(event.error?.message || "The Realtime session returned an error.");
  }
}

async function fetchClientSecret() {
  const headers = { "X-Okay-Hermes-Client": "voice-page-v1" };
  if (localSessionId) {
    headers["X-Okay-Hermes-Session-ID"] = localSessionId;
  }
  const response = await fetch("/client-secret", { method: "POST", headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Client secret request failed (${response.status})`);
  }
  if (
    typeof payload.value !== "string" ||
    !payload.value.startsWith("ek_") ||
    !payload.session ||
    typeof payload.session.model !== "string"
  ) {
    throw new Error("Gateway returned an invalid Realtime client secret");
  }
  return payload;
}

async function startConversation() {
  clearError();
  resetTranscript();
  resetSdkInterruptionDiagnostics();
  stopMessageSent = false;
  teardownCompleteSent = false;
  startButton.disabled = true;
  setStatus("connecting", "Requesting microphone");

  try {
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    setStatus("connecting", "Creating Realtime session");
    const clientSecret = await fetchClientSecret();
    const transport = new OpenAIRealtimeWebRTC({
      audioElement: remoteAudio,
      mediaStream: localStream,
    });
    realtimeTransport = transport;

    transport.on("*", (event) => {
      if (realtimeTransport === transport) {
        void handleRealtimeEvent(event);
      }
    });
    transport.on("connection_change", (state) => {
      if (realtimeTransport !== transport) return;
      appendEvent({ type: "webrtc.connection_state", state });
      recordTiming("peer_connection_state", { state });
      if (state === "connected") {
        setStatus("connected", "Connected — speak naturally");
      } else if (state === "disconnected" && !isStopping) {
        failConversation("Realtime WebRTC transport disconnected");
      }
    });
    transport.on("error", (transportError) => {
      if (realtimeTransport !== transport) return;
      const cause = transportError?.error || transportError;
      if (cause?.type === "error") return;
      const detail = {
        type: "transport_error",
        code: typeof cause?.name === "string" ? cause.name : null,
        message:
          typeof cause?.message === "string"
            ? cause.message
            : "The Realtime transport returned an error.",
      };
      appendEvent({ type: "error", error: detail });
      recordRealtimeError(detail);
      showError(detail.message);
    });

    const connectPromise = transport.connect({
      apiKey: clientSecret.value,
      model: clientSecret.session.model,
      url: localSessionId
        ? `${window.location.origin}/session?local_session_id=${encodeURIComponent(localSessionId)}`
        : undefined,
      initialSessionConfig: {
        providerData: clientSecret.session,
      },
    });

    await connectPromise;
    if (realtimeTransport !== transport) return;

    if (localSessionId) {
      sendControlMessage({ type: "page_started" });
    }
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

    if (realtimeTransport) {
      const transport = realtimeTransport;
      realtimeTransport = null;
      transport.close();
    }

    if (localStream) {
      for (const track of localStream.getTracks()) {
        track.stop();
      }
      localStream = null;
    }

    remoteAudio.srcObject = null;
    resetSdkInterruptionDiagnostics();
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
// X / close: best-effort graceful teardown. pagehide is the reliable unload
// signal (fires on bfcache and background kill where beforeunload does not);
// beforeunload is kept for browsers that still favor it. Both are idempotent
// via the isStopping / teardownCompleteSent guards in stopConversation, and the
// controller's teardown pipeline releases the profile lock regardless of whether
// these frames actually flush before the socket drops.
window.addEventListener("pagehide", () => stopConversation({ reason: "native_cancel" }));
window.addEventListener("beforeunload", () => stopConversation({ reason: "native_cancel" }));
openControllerSocket();
