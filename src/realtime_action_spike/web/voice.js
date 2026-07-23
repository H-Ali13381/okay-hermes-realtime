// frontend/voice.js
var startButton = document.getElementById("start-button");
var stopButton = document.getElementById("stop-button");
var voiceCard = document.getElementById("voice-card");
var connectionStatus = document.getElementById("connection-status");
var statusText = document.getElementById("status-text");
var errorBanner = document.getElementById("error-banner");
var transcriptList = document.getElementById("transcript-list");
var executionList = document.getElementById("execution-list");
var executionCount = document.getElementById("execution-count");
var eventList = document.getElementById("event-list");
var remoteAudio = document.getElementById("remote-audio");
var launchMode = document.getElementById("launch-mode");
var interruptionStateText = document.getElementById("interruption-state");
var speechSilenceMs = document.getElementById("speech-silence-ms");
var responseCancelMs = document.getElementById("response-cancel-ms");
var truncationMs = document.getElementById("truncation-ms");
var listeningRestoredMs = document.getElementById("listening-restored-ms");
var initialUrl = new URL(window.location.href);
var searchParams = new URLSearchParams(window.location.search);
var activationToken = searchParams.get("activation");
var localSessionId = window.__LOCAL_SESSION_ID__ || null;
if (activationToken) {
  initialUrl.searchParams.delete("activation");
  history.replaceState(null, "", `${initialUrl.pathname}${initialUrl.search}${initialUrl.hash}`);
}
var peerConnection = null;
var dataChannel = null;
var localStream = null;
var executionScope = null;
var disconnectAfterResponse = false;
var transportFailureTimer = null;
var handledCallIds = /* @__PURE__ */ new Set();
var receivedExecutionCount = 0;
var isStopping = false;
var controllerSocket = null;
var controllerSessionClosed = false;
var stopMessageSent = false;
var teardownCompleteSent = false;
var interruptionStartedMs = null;
var waitingForNextAudio = false;
var transcriptTurns = /* @__PURE__ */ new Map();
function sendControlMessage(message) {
  if (!controllerSocket || controllerSocket.readyState !== WebSocket.OPEN || !localSessionId || controllerSessionClosed) {
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
      remote_audio_muted: data.remoteAudioMuted
    };
  }
  if (name === "realtime_error") {
    return {
      error_type: data.errorType,
      code: data.code,
      message: data.message
    };
  }
  return {};
}
function boundedDiagnosticText(value, maxLength = 512) {
  if (typeof value !== "string") return void 0;
  const normalized = value.replace(/[\u0000-\u001f\u007f]+/gu, " ").trim();
  if (!normalized) return void 0;
  return normalized.slice(0, maxLength);
}
function recordRealtimeResponseDone(event) {
  const response = event.response || {};
  if (typeof response.id !== "string" || typeof response.status !== "string") return;
  recordTiming("realtime_response_done", {
    responseId: response.id,
    status: response.status,
    outputTypes: (Array.isArray(response.output) ? response.output : []).map((item) => boundedDiagnosticText(item?.type, 64)).filter(Boolean).slice(0, 16),
    remoteAudioMuted: false
  });
}
function recordRealtimeError(error) {
  recordTiming("realtime_error", {
    errorType: boundedDiagnosticText(error?.type, 128),
    code: boundedDiagnosticText(error?.code, 128),
    message: boundedDiagnosticText(error?.message) || "Unspecified Realtime error"
  });
}
function recordTiming(name, data = {}) {
  const atMs = Number(performance.now().toFixed(2));
  appendEvent({
    type: "timing",
    name,
    at_ms: atMs,
    detail: data
  });
  sendControlMessage({
    type: "timing",
    name,
    monotonic_ms: atMs,
    data: controlTimingData(name, data)
  });
}
function hasLiveMedia() {
  return peerConnection?.connectionState === "connected" && dataChannel?.readyState === "open";
}
function handleControllerSocketLoss(message) {
  if (hasLiveMedia()) {
    launchMode.textContent = "Wake activation mode \xB7 controller unavailable";
    setStatus("connected", "Connected \u2014 controller unavailable");
    appendEvent({ type: "control.degraded", message });
    return;
  }
  failConversation(message);
}
function openControllerSocket() {
  if (!activationToken || !localSessionId) return;
  launchMode.textContent = "Wake activation mode \xB7 connecting controller";
  startButton.disabled = true;
  const websocketScheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const controllerUrl = `${websocketScheme}//${window.location.host}/control?activation=` + encodeURIComponent(activationToken);
  const socket = new WebSocket(controllerUrl);
  controllerSocket = socket;
  socket.addEventListener("open", () => {
    if (controllerSocket !== socket) return;
    activationToken = null;
    launchMode.textContent = "Wake activation mode \xB7 controller connected";
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
          sendStopMessage: false
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
      handleControllerSocketLoss("Could not connect to the local voice controller");
    }
  });
  socket.addEventListener("close", () => {
    if (controllerSocket === socket) {
      controllerSocket = null;
      if (!controllerSessionClosed) {
        handleControllerSocketLoss("Local voice controller disconnected");
      }
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
function scheduleTransportFailure(pc) {
  clearTransportFailureTimer();
  transportFailureTimer = window.setTimeout(() => {
    transportFailureTimer = null;
    if (peerConnection === pc && pc.connectionState === "disconnected") {
      failConversation(`WebRTC ${pc.connectionState}`);
    }
  }, 3e3);
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
  text.textContent = role === "user" ? "Transcribing\u2026" : "Speaking\u2026";
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
  turn.text.textContent = turn.value || (role === "user" ? "No speech detected." : "No transcript received.");
  turn.row.dataset.error = String(Boolean(options.error));
  transcriptList.scrollTop = transcriptList.scrollHeight;
}
function resetTranscript() {
  transcriptTurns.clear();
  const empty = document.createElement("div");
  empty.className = "empty";
  empty.textContent = "Your words and the assistant\u2019s spoken replies will appear here.";
  transcriptList.replaceChildren(empty);
}
function formatJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (_error) {
    return String(value);
  }
}
function sanitizeDiagnosticValue(value) {
  if (Array.isArray(value)) {
    return value.map(sanitizeDiagnosticValue);
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, nestedValue]) => [
      key,
      key === "audio" && typeof nestedValue === "string" ? `[embedded audio omitted: ${nestedValue.length} characters]` : sanitizeDiagnosticValue(nestedValue)
    ])
  );
}
function appendEvent(event) {
  const noisy = event.type === "timing" ? false : event.type.endsWith(".delta") || event.type === "rate_limits.updated";
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
  time.textContent = (/* @__PURE__ */ new Date()).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
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
      output_types: (event.response?.output || []).map((item) => item.type)
    };
  }
  if (event.type === "session.created" || event.type === "session.updated") {
    return {
      session_id: event.session?.id,
      model: event.session?.model,
      voice: event.session?.audio?.output?.voice
    };
  }
  if (event.type === "conversation.item.retrieved") {
    return {
      item: sanitizeDiagnosticValue(event.item)
    };
  }
  if (event.type === "error") return event.error || event;
  if (event.type === "timing") {
    return {
      at_ms: event.at_ms,
      marker: event.name,
      detail: event.detail
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
  speechSilenceMs.textContent = "\u2014";
  responseCancelMs.textContent = "\u2014";
  truncationMs.textContent = "\u2014";
  listeningRestoredMs.textContent = "\u2014";
}
function elapsedSinceInterruption() {
  if (interruptionStartedMs === null) return null;
  return Number((performance.now() - interruptionStartedMs).toFixed(2));
}
function formatSdkTiming(value) {
  return value === null ? "\u2014" : `${value.toFixed(2)} ms`;
}
function observeSpeechStarted() {
  interruptionStartedMs = performance.now();
  waitingForNextAudio = true;
  interruptionStateText.textContent = "Speech detected \xB7 SDK interruption active";
  speechSilenceMs.textContent = "0.00 ms";
  responseCancelMs.textContent = "\u2014";
  truncationMs.textContent = "\u2014";
  listeningRestoredMs.textContent = "\u2014";
}
function observeResponseCancellation(event) {
  if (event.response?.status !== "cancelled" || interruptionStartedMs === null) return;
  responseCancelMs.textContent = formatSdkTiming(elapsedSinceInterruption());
  interruptionStateText.textContent = "Response cancelled \xB7 waiting for next response";
}
function observeOutputBufferCleared() {
  if (interruptionStartedMs === null) return;
  truncationMs.textContent = formatSdkTiming(elapsedSinceInterruption());
  interruptionStateText.textContent = "Output buffer cleared \xB7 listening";
}
function observeResponseFirstAudio() {
  if (!waitingForNextAudio || interruptionStartedMs === null) return;
  listeningRestoredMs.textContent = formatSdkTiming(elapsedSinceInterruption());
  interruptionStateText.textContent = "Playback restored by SDK";
  waitingForNextAudio = false;
}
function sendRealtimeEvent(event, channel = dataChannel) {
  if (!channel || channel.readyState !== "open") {
    throw new Error("Realtime data channel is not open");
  }
  channel.send(JSON.stringify(event));
}
async function executeFunctionCall(item, sessionContext) {
  const callId = item.call_id;
  if (!callId || handledCallIds.has(callId)) return;
  handledCallIds.add(callId);
  let output;
  try {
    const response = await fetch("/execute", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Okay-Hermes-Client": "voice-page-v1"
      },
      body: JSON.stringify({
        scope: sessionContext.scope,
        call_id: callId,
        name: item.name,
        arguments: item.arguments || "{}"
      })
    });
    output = await response.json().catch(() => ({}));
    if (!response.ok && !output.error) {
      output = {
        ok: false,
        call_id: callId,
        error: { type: "gateway_error", message: `Gateway returned ${response.status}` }
      };
    }
  } catch (error) {
    output = {
      ok: false,
      call_id: callId,
      error: { type: "gateway_unreachable", message: error.message || String(error) }
    };
  }
  if (peerConnection !== sessionContext.pc || dataChannel !== sessionContext.dc || executionScope !== sessionContext.scope) {
    return;
  }
  createActionStateCard(output);
  if (!output.ok) {
    setStatus("connected", "Connected \u2014 tools unavailable");
  }
  sendRealtimeEvent(
    {
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: callId,
        output: JSON.stringify(output)
      }
    },
    sessionContext.dc
  );
  sendRealtimeEvent({ type: "response.create" }, sessionContext.dc);
  if (output.ok && output.result?.end_session) {
    disconnectAfterResponse = true;
  }
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
  } else if (event.type === "response.function_call_arguments.done") {
    await executeFunctionCall(
      {
        type: "function_call",
        call_id: event.call_id,
        name: event.name,
        arguments: event.arguments,
        item_id: event.item_id
      },
      sessionContext
    );
  } else if (event.type === "response.output_audio.delta" || event.type === "response.output_audio.started") {
    observeResponseFirstAudio();
  } else if (event.type === "output_audio_buffer.cleared") {
    observeOutputBufferCleared();
  } else if (event.type === "response.done") {
    recordRealtimeResponseDone(event);
    observeResponseCancellation(event);
    const functionCalls = (event.response?.output || []).filter(
      (item) => item.type === "function_call"
    );
    if (functionCalls.length > 0) {
      for (const item of functionCalls) {
        await executeFunctionCall(item, sessionContext);
      }
    } else if (disconnectAfterResponse) {
      disconnectAfterResponse = false;
      window.setTimeout(() => stopConversation({ reason: "model_request" }), 500);
    } else {
      setStatus("connected", "Connected \u2014 speak naturally");
    }
  } else if (event.type === "error") {
    recordRealtimeError(event.error);
    showError(event.error?.message || "The Realtime session returned an error.");
  }
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
        autoGainControl: true
      }
    });
    const pc = new RTCPeerConnection();
    peerConnection = pc;
    pc.ontrack = (event) => {
      if (peerConnection !== pc) return;
      remoteAudio.srcObject = event.streams[0];
      remoteAudio.play().catch(() => {
      });
    };
    for (const track of localStream.getTracks()) {
      pc.addTrack(track, localStream);
    }
    const dc = pc.createDataChannel("oai-events");
    dataChannel = dc;
    dc.addEventListener("open", () => {
      if (peerConnection !== pc || dataChannel !== dc) return;
      setStatus("connected", "Connected \u2014 speak naturally");
      stopButton.disabled = false;
      if (localSessionId) {
        sendControlMessage({ type: "page_started" });
      }
    });
    dc.addEventListener("message", (message) => {
      if (peerConnection !== pc || dataChannel !== dc) return;
      try {
        const event = JSON.parse(message.data);
        void handleRealtimeEvent(event, { pc, dc, scope: executionScope });
      } catch (error) {
        showError(`Could not parse a Realtime event: ${error.message || String(error)}`);
      }
    });
    dc.addEventListener("error", () => {
      if (peerConnection === pc && dataChannel === dc && !isStopping) {
        failConversation("Realtime data channel failed");
      }
    });
    dc.addEventListener("close", () => {
      if (peerConnection === pc && dataChannel === dc && !isStopping) {
        failConversation("Realtime data channel closed unexpectedly");
      }
    });
    pc.addEventListener("connectionstatechange", () => {
      if (peerConnection !== pc) return;
      appendEvent({ type: "webrtc.connection_state", state: pc.connectionState });
      recordTiming("peer_connection_state", { state: pc.connectionState });
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
    recordTiming("sdp_offer_created");
    const headers = {
      "Content-Type": "application/sdp",
      "X-Okay-Hermes-Client": "voice-page-v1"
    };
    if (localSessionId) {
      headers["X-Okay-Hermes-Session-ID"] = localSessionId;
    }
    const sessionUrl = localSessionId ? `/session?local_session_id=${encodeURIComponent(localSessionId)}` : "/session";
    const response = await fetch(sessionUrl, {
      method: "POST",
      body: offer.sdp,
      headers
    });
    if (!response.ok) {
      let detail = await response.text();
      try {
        detail = JSON.parse(detail).detail || detail;
      } catch (_error) {
      }
      throw new Error(detail || `Session gateway returned ${response.status}`);
    }
    executionScope = response.headers.get("X-Okay-Hermes-Execution-Scope");
    if (localSessionId && !executionScope) {
      throw new Error("Session gateway did not return a local execution scope");
    }
    const answer = { type: "answer", sdp: await response.text() };
    await pc.setRemoteDescription(answer);
    recordTiming("sdp_answer_applied");
  } catch (error) {
    showError(error.message || String(error));
    stopConversation({ preserveError: true, reason: "transport_failure" });
  }
}
function stopConversation(options = {}) {
  if (isStopping || teardownCompleteSent) return;
  isStopping = true;
  clearTransportFailureTimer();
  const shouldSendStopMessage = options.sendStopMessage !== false;
  try {
    if (!stopMessageSent) {
      if (shouldSendStopMessage) {
        sendControlMessage({ type: "stop", reason: options.reason || "button" });
      }
      stopMessageSent = true;
    }
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
    executionScope = null;
    handledCallIds.clear();
    disconnectAfterResponse = false;
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
window.addEventListener("pagehide", () => stopConversation({ reason: "native_cancel" }));
window.addEventListener("beforeunload", () => stopConversation({ reason: "native_cancel" }));
openControllerSocket();
