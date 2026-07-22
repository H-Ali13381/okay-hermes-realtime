const EVENT_TYPES = new Set([
  "response_created",
  "speech_started",
  "playback_suppressed",
  "response_cancelled",
  "response_truncated",
  "response_first_audio",
  "listening_restored",
]);

export function createInterruptionState(localSessionId) {
  validateIdentity(localSessionId, "localSessionId");
  return {
    localSessionId,
    activeResponseId: null,
    suppressedResponseId: null,
    pendingRestoreResponseId: null,
    restoreInFlightResponseId: null,
    interruption: null,
  };
}

export function reduceInterruption(state, event) {
  validateState(state);
  validateEvent(event);
  if (event.localSessionId !== state.localSessionId) return unchanged(state);

  switch (event.type) {
    case "response_created":
      return responseCreated(state, event);
    case "speech_started":
      return speechStarted(state, event);
    case "playback_suppressed":
      return updateInterruptedResponse(state, event, "playbackSuppressedMs");
    case "response_cancelled":
      return updateInterruptedResponse(state, event, "responseCancelledMs");
    case "response_truncated":
      return updateInterruptedResponse(state, event, "truncationObservedMs");
    case "response_first_audio":
      return responseFirstAudio(state, event);
    case "listening_restored":
      return listeningRestored(state, event);
    default:
      throw new Error(`unsupported interruption event type: ${event.type}`);
  }
}

function responseCreated(state, event) {
  if (event.responseId === state.activeResponseId) return unchanged(state);
  const pendingRestoreResponseId =
    state.suppressedResponseId && event.responseId !== state.suppressedResponseId
      ? event.responseId
      : state.pendingRestoreResponseId;
  return changed(
    {
      ...state,
      activeResponseId: event.responseId,
      pendingRestoreResponseId,
    },
    [],
  );
}

function speechStarted(state, event) {
  if (!state.activeResponseId || state.suppressedResponseId) return unchanged(state);
  const responseId = state.activeResponseId;
  return changed(
    {
      ...state,
      suppressedResponseId: responseId,
      pendingRestoreResponseId: null,
      restoreInFlightResponseId: null,
      interruption: {
        responseId,
        speechStartedMs: event.atMs,
        playbackSuppressedMs: null,
        responseCancelledMs: null,
        truncationObservedMs: null,
        nextResponseFirstAudioMs: null,
        listeningRestoredMs: null,
        speechToSilenceMs: null,
      },
    },
    [{ type: "suppress_playback", responseId }],
  );
}

function updateInterruptedResponse(state, event, fieldName) {
  const trace = state.interruption;
  if (
    !trace ||
    !event.responseId ||
    event.responseId !== trace.responseId ||
    trace[fieldName] !== null
  ) {
    return unchanged(state);
  }
  const interruption = { ...trace, [fieldName]: event.atMs };
  if (fieldName === "playbackSuppressedMs") {
    interruption.speechToSilenceMs = nonNegativeDelta(
      trace.speechStartedMs,
      event.atMs,
    );
  }
  return changed({ ...state, interruption }, []);
}

function responseFirstAudio(state, event) {
  const trace = state.interruption;
  if (
    !trace ||
    !state.suppressedResponseId ||
    !state.pendingRestoreResponseId ||
    event.responseId !== state.pendingRestoreResponseId ||
    event.responseId === state.suppressedResponseId ||
    state.restoreInFlightResponseId ||
    trace.nextResponseFirstAudioMs !== null
  ) {
    return unchanged(state);
  }
  return changed(
    {
      ...state,
      restoreInFlightResponseId: event.responseId,
      interruption: {
        ...trace,
        nextResponseFirstAudioMs: event.atMs,
      },
    },
    [
      {
        type: "restore_playback",
        interruptedResponseId: trace.responseId,
        responseId: event.responseId,
      },
    ],
  );
}

function listeningRestored(state, event) {
  const trace = state.interruption;
  if (
    !trace ||
    event.interruptedResponseId !== trace.responseId ||
    event.responseId !== state.restoreInFlightResponseId ||
    trace.listeningRestoredMs !== null
  ) {
    return unchanged(state);
  }
  return changed(
    {
      ...state,
      suppressedResponseId: null,
      pendingRestoreResponseId: null,
      restoreInFlightResponseId: null,
      interruption: {
        ...trace,
        listeningRestoredMs: event.atMs,
      },
    },
    [],
  );
}

function nonNegativeDelta(startMs, endMs) {
  if (startMs === null || endMs === null || endMs < startMs) return null;
  return Number((endMs - startMs).toFixed(3));
}

function validateState(state) {
  if (!state || typeof state !== "object") throw new TypeError("state must be an object");
  validateIdentity(state.localSessionId, "state.localSessionId");
}

function validateEvent(event) {
  if (!event || typeof event !== "object") throw new TypeError("event must be an object");
  if (!EVENT_TYPES.has(event.type)) throw new Error(`unsupported interruption event type: ${event.type}`);
  validateIdentity(event.localSessionId, "localSessionId");
  validateTimestamp(event.atMs);

  if (event.type !== "speech_started") {
    validateIdentity(event.responseId, "responseId");
  }
  if (event.type === "listening_restored") {
    validateIdentity(event.interruptedResponseId, "interruptedResponseId");
  }
}

function validateIdentity(value, name) {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 256 ||
    value.trim() !== value ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    throw new Error(`${name} must be a bounded non-empty identifier`);
  }
}

function validateTimestamp(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error("atMs must be a finite non-negative number");
  }
}

function unchanged(state) {
  return { state, effects: [] };
}

function changed(state, effects) {
  return { state, effects };
}
