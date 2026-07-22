import assert from "node:assert/strict";
import test from "node:test";

import {
  createInterruptionState,
  reduceInterruption,
} from "../../src/realtime_action_spike/web/interruption_state.mjs";

const SESSION = "local-session-1234";

function reduce(state, type, fields = {}) {
  return reduceInterruption(state, {
    type,
    localSessionId: SESSION,
    ...fields,
  });
}

test("normal interruption suppresses current response and restores on next response audio", () => {
  let state = createInterruptionState(SESSION);
  ({ state } = reduce(state, "response_created", {
    responseId: "resp-1",
    atMs: 10,
  }));

  let result = reduce(state, "speech_started", { atMs: 20 });
  state = result.state;
  assert.deepEqual(result.effects, [{ type: "suppress_playback", responseId: "resp-1" }]);

  ({ state } = reduce(state, "playback_suppressed", {
    responseId: "resp-1",
    atMs: 21.5,
  }));
  ({ state } = reduce(state, "response_cancelled", {
    responseId: "resp-1",
    atMs: 24,
  }));
  ({ state } = reduce(state, "response_truncated", {
    responseId: "resp-1",
    atMs: 25,
  }));
  ({ state } = reduce(state, "response_created", {
    responseId: "resp-2",
    atMs: 30,
  }));

  result = reduce(state, "response_first_audio", {
    responseId: "resp-2",
    atMs: 31,
  });
  state = result.state;

  assert.deepEqual(result.effects, [
    {
      type: "restore_playback",
      interruptedResponseId: "resp-1",
      responseId: "resp-2",
    },
  ]);
  assert.equal(state.interruption.listeningRestoredMs, null);
  ({ state } = reduce(state, "listening_restored", {
    responseId: "resp-2",
    interruptedResponseId: "resp-1",
    atMs: 31.4,
  }));
  assert.equal(state.interruption.speechToSilenceMs, 1.5);
  assert.equal(state.interruption.responseCancelledMs, 24);
  assert.equal(state.interruption.truncationObservedMs, 25);
  assert.equal(state.interruption.nextResponseFirstAudioMs, 31);
  assert.equal(state.interruption.listeningRestoredMs, 31.4);
  assert.equal(state.suppressedResponseId, null);
});

test("suppression does not restore for residual audio from interrupted response", () => {
  let state = createInterruptionState(SESSION);
  ({ state } = reduce(state, "response_created", { responseId: "resp-1", atMs: 1 }));
  ({ state } = reduce(state, "speech_started", { atMs: 2 }));
  ({ state } = reduce(state, "playback_suppressed", {
    responseId: "resp-1",
    atMs: 3,
  }));

  const residual = reduce(state, "response_first_audio", {
    responseId: "resp-1",
    atMs: 4,
  });

  assert.deepEqual(residual.effects, []);
  assert.equal(residual.state.suppressedResponseId, "resp-1");
  assert.equal(residual.state.interruption.nextResponseFirstAudioMs, null);
});

test("new response creation alone does not restore playback", () => {
  let state = createInterruptionState(SESSION);
  ({ state } = reduce(state, "response_created", { responseId: "resp-1", atMs: 1 }));
  ({ state } = reduce(state, "speech_started", { atMs: 2 }));
  ({ state } = reduce(state, "playback_suppressed", {
    responseId: "resp-1",
    atMs: 3,
  }));

  const created = reduce(state, "response_created", {
    responseId: "resp-2",
    atMs: 4,
  });

  assert.deepEqual(created.effects, []);
  assert.equal(created.state.suppressedResponseId, "resp-1");
  assert.equal(created.state.pendingRestoreResponseId, "resp-2");
});

test("stale session events are ignored without mutation", () => {
  const state = createInterruptionState(SESSION);
  const result = reduceInterruption(state, {
    type: "response_created",
    localSessionId: "stale-session-9999",
    responseId: "resp-stale",
    atMs: 1,
  });

  assert.strictEqual(result.state, state);
  assert.deepEqual(result.effects, []);
});

test("response lifecycle events require explicit matching response id", () => {
  let state = createInterruptionState(SESSION);
  ({ state } = reduce(state, "response_created", { responseId: "resp-1", atMs: 1 }));
  ({ state } = reduce(state, "speech_started", { atMs: 2 }));

  const stale = reduce(state, "response_cancelled", {
    responseId: "resp-stale",
    atMs: 3,
  });

  assert.strictEqual(stale.state, state);
  assert.deepEqual(stale.effects, []);
  assert.throws(
    () => reduce(state, "response_truncated", { atMs: 4 }),
    /responseId/,
  );
});

test("duplicate speech-start is idempotent", () => {
  let state = createInterruptionState(SESSION);
  ({ state } = reduce(state, "response_created", { responseId: "resp-1", atMs: 1 }));
  const first = reduce(state, "speech_started", { atMs: 2 });
  const duplicate = reduce(first.state, "speech_started", { atMs: 3 });

  assert.equal(first.effects.length, 1);
  assert.deepEqual(duplicate.effects, []);
  assert.strictEqual(duplicate.state, first.state);
});

test("two interruptions replace diagnostics only after the second speech starts", () => {
  let state = createInterruptionState(SESSION);
  ({ state } = reduce(state, "response_created", { responseId: "resp-1", atMs: 1 }));
  ({ state } = reduce(state, "speech_started", { atMs: 2 }));
  ({ state } = reduce(state, "playback_suppressed", {
    responseId: "resp-1",
    atMs: 3,
  }));
  ({ state } = reduce(state, "response_created", { responseId: "resp-2", atMs: 4 }));
  ({ state } = reduce(state, "response_first_audio", {
    responseId: "resp-2",
    atMs: 5,
  }));
  ({ state } = reduce(state, "listening_restored", {
    responseId: "resp-2",
    interruptedResponseId: "resp-1",
    atMs: 5.1,
  }));

  const previous = state.interruption;
  ({ state } = reduce(state, "speech_started", { atMs: 6 }));

  assert.notStrictEqual(state.interruption, previous);
  assert.equal(state.interruption.responseId, "resp-2");
  assert.equal(state.interruption.speechStartedMs, 6);
  assert.equal(state.interruption.playbackSuppressedMs, null);
});

test("invalid event timestamps and identities fail clearly", () => {
  const state = createInterruptionState(SESSION);

  assert.throws(
    () => reduce(state, "response_created", { responseId: "", atMs: 1 }),
    /responseId/,
  );
  assert.throws(
    () => reduce(state, "response_created", { responseId: "resp-1", atMs: -1 }),
    /atMs/,
  );
});
