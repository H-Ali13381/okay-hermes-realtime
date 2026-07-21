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

let peerConnection = null;
let dataChannel = null;
let localStream = null;
let openAIRealtimeSessionId = null;
let receivedExecutionCount = 0;
let disconnectAfterResponse = false;
let transportFailureTimer = null;
let isStopping = false;
const handledCallIds = new Set();
const transcriptTurns = new Map();

function recordTiming(name, data = {}) {
  appendEvent({
    type: "timing",
    name,
    at_ms: Number(performance.now().toFixed(2)),
    detail: data,
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
  stopConversation({ preserveError: true });
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

function createExecutionCard(item, parsedArguments) {
  clearEmptyState(executionList);
  receivedExecutionCount += 1;
  executionCount.textContent = `${receivedExecutionCount} received`;

  const card = document.createElement("article");
  card.className = "execution";
  const head = document.createElement("div");
  head.className = "execution-head";
  const name = document.createElement("div");
  name.className = "execution-name";
  name.textContent = item.name;
  const state = document.createElement("div");
  state.className = "execution-state";
  state.textContent = "requested";
  head.append(name, state);

  const argumentsBlock = document.createElement("pre");
  argumentsBlock.textContent = formatJson({
    call_id: item.call_id,
    arguments: parsedArguments,
  });

  const result = document.createElement("pre");
  result.className = "execution-result";
  result.textContent = "Waiting for local broker…";
  card.append(head, argumentsBlock, result);
  executionList.prepend(card);
  return { state, result };
}

function updateExecutionCard(card, output) {
  card.state.textContent = output.ok ? "completed" : "rejected";
  card.state.classList.add(output.ok ? "ok" : "error");
  card.result.textContent = formatJson(output);
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

  let parsedArguments = item.arguments;
  try {
    parsedArguments = JSON.parse(item.arguments || "{}");
  } catch (_error) {
    parsedArguments = item.arguments;
  }
  const card = createExecutionCard(item, parsedArguments);

  let output;
  try {
    const response = await fetch("/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionContext.sessionId,
        call_id: callId,
        name: item.name,
        arguments: item.arguments || "{}",
      }),
    });
    output = await response.json();
    if (!response.ok && !output.error) {
      output = {
        ok: false,
        call_id: callId,
        error: { type: "gateway_error", message: `Gateway returned ${response.status}` },
      };
    }
  } catch (error) {
    output = {
      ok: false,
      call_id: callId,
      error: { type: "gateway_unreachable", message: error.message },
    };
  }

  if (
    peerConnection !== sessionContext.pc ||
    dataChannel !== sessionContext.dc ||
    openAIRealtimeSessionId !== sessionContext.sessionId
  ) {
    card.state.textContent = "discarded";
    card.state.classList.add("error");
    card.result.textContent = "Originating Realtime session ended before result delivery.";
    return;
  }

  updateExecutionCard(card, output);
  sendRealtimeEvent(
    {
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: callId,
        output: JSON.stringify(output),
      },
    },
    sessionContext.dc,
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
  } else if (event.type === "response.output_audio_transcript.done") {
    updateTranscriptTurn("assistant", event.item_id, event.transcript);
  } else if (event.type === "input_audio_buffer.speech_started") {
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
        item_id: event.item_id,
      },
      sessionContext,
    );
  } else if (event.type === "response.done") {
    const functionCalls = (event.response?.output || []).filter(
      (item) => item.type === "function_call"
    );
    if (functionCalls.length > 0) {
      for (const item of functionCalls) {
        await executeFunctionCall(item, sessionContext);
      }
    } else if (disconnectAfterResponse) {
      disconnectAfterResponse = false;
      window.setTimeout(stopConversation, 500);
    } else {
      setStatus("connected", "Connected — speak naturally");
    }
  } else if (event.type === "error") {
    showError(event.error?.message || "The Realtime session returned an error.");
  }
}

async function startConversation() {
  clearError();
  resetTranscript();
  openAIRealtimeSessionId = null;
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
    const answerSdp = await sdpResponse.text();
    const answer = { type: "answer", sdp: answerSdp };
    await pc.setRemoteDescription(answer);
    recordTiming("sdp_answer_applied", {
      type: "answer",
    });
    stopButton.disabled = false;
  } catch (error) {
    showError(error.message || String(error));
    stopConversation({ preserveError: true });
  }
}

function stopConversation(options = {}) {
  if (isStopping) return;
  isStopping = true;
  clearTransportFailureTimer();
  if (localStream) {
    for (const track of localStream.getTracks()) track.stop();
    localStream = null;
  }
  if (dataChannel) {
    if (dataChannel.readyState !== "closed") dataChannel.close();
    dataChannel = null;
  }
  if (peerConnection) {
    if (peerConnection.connectionState !== "closed") peerConnection.close();
    peerConnection = null;
  }
  remoteAudio.pause();
  remoteAudio.srcObject = null;
  openAIRealtimeSessionId = null;
  handledCallIds.clear();
  disconnectAfterResponse = false;
  startButton.disabled = false;
  stopButton.disabled = true;
  voiceCard.dataset.active = "false";
  if (!options.preserveError) {
    clearError();
    setStatus("idle", "Not connected");
  }
  isStopping = false;
}

startButton.addEventListener("click", startConversation);
stopButton.addEventListener("click", () => stopConversation());
window.addEventListener("beforeunload", () => stopConversation());
