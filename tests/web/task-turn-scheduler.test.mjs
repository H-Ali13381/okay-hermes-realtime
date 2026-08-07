import assert from "node:assert/strict";
import test from "node:test";

import { TaskTurnScheduler } from "../../frontend/task-turn-scheduler.js";

function taskEvent(overrides = {}) {
  return {
    type: "task_event",
    session_id: "local-session-1234",
    event_id: 17,
    task_id: "t_voice01",
    kind: "blocked",
    title: "Voice handoff: update config",
    detail: "May I overwrite config.toml?",
    requires_user_input: true,
    block_kind: "needs_input",
    ...overrides,
  };
}

function harness(initiallyReady = true) {
  const sent = [];
  let ready = initiallyReady;
  const scheduler = new TaskTurnScheduler({
    sendEvent(event) {
      sent.push(event);
    },
    isReady() {
      return ready;
    },
  });
  return {
    scheduler,
    sent,
    setReady(value) {
      ready = value;
    },
  };
}

function responseInstructions(sent) {
  assert.equal(sent.length, 1);
  assert.equal(sent[0].type, "response.create");
  return sent[0].response.instructions;
}

test("an idle task event creates one response with untrusted task data", () => {
  const { scheduler, sent } = harness();

  scheduler.onTaskEvent(taskEvent());

  const instructions = responseInstructions(sent);
  assert.match(instructions, /untrusted data/i);
  assert.match(instructions, /resolve_heavy_agent_block/);
  assert.match(instructions, /"task_id":"t_voice01"/);
  assert.match(instructions, /"event_id":17/);
});

test("a task event never creates a response while the user is speaking", () => {
  const { scheduler, sent } = harness();

  scheduler.onSpeechStarted();
  scheduler.onTaskEvent(taskEvent());
  assert.deepEqual(sent, []);

  scheduler.onSpeechStopped();
  assert.deepEqual(sent, []);

  scheduler.onAudioCommitted();
  assert.equal(sent.length, 1);
});

test("a task event waits for an active assistant response to finish", () => {
  const { scheduler, sent } = harness();

  scheduler.requestResponse();
  scheduler.onResponseCreated();
  sent.length = 0;
  scheduler.onTaskEvent(taskEvent({ event_id: 18 }));
  assert.deepEqual(sent, []);

  scheduler.onResponseDone({ status: "completed" });
  assert.equal(sent.length, 1);
  assert.match(sent[0].response.instructions, /"event_id":18/);
});

test("a cancelled announcement is requeued until the next committed user turn", () => {
  const { scheduler, sent } = harness();

  scheduler.onTaskEvent(taskEvent({ event_id: 19 }));
  scheduler.onResponseCreated();
  sent.length = 0;

  scheduler.onSpeechStarted();
  scheduler.onResponseDone({ status: "cancelled" });
  assert.deepEqual(sent, []);

  scheduler.onSpeechStopped();
  scheduler.onAudioCommitted();
  assert.equal(sent.length, 1);
  assert.match(sent[0].response.instructions, /"event_id":19/);
});

test("manual VAD mode creates a response only after audio is committed", () => {
  const { scheduler, sent } = harness();

  scheduler.onSpeechStarted();
  scheduler.onSpeechStopped();
  assert.deepEqual(sent, []);

  scheduler.onAudioCommitted();
  assert.deepEqual(sent, [{ type: "response.create" }]);
});

test("manual VAD mode also tolerates commit arriving before speech_stopped", () => {
  const { scheduler, sent } = harness();

  scheduler.onSpeechStarted();
  scheduler.onAudioCommitted();
  assert.deepEqual(sent, []);

  scheduler.onSpeechStopped();
  assert.deepEqual(sent, [{ type: "response.create" }]);
});

test("function-call continuations keep task events for the tool-result response", () => {
  const { scheduler, sent } = harness();

  scheduler.requestResponse();
  scheduler.onResponseCreated();
  sent.length = 0;
  scheduler.onTaskEvent(taskEvent({ event_id: 20 }));

  scheduler.onResponseDone({ status: "completed", deferContinuation: true });
  assert.deepEqual(sent, []);

  scheduler.requestResponse();
  assert.equal(sent.length, 1);
  assert.match(sent[0].response.instructions, /"event_id":20/);
});

test("a failed announcement waits for a later user turn instead of retry-looping", () => {
  const { scheduler, sent } = harness();

  scheduler.onTaskEvent(taskEvent({ event_id: 22 }));
  scheduler.onResponseCreated();
  sent.length = 0;
  scheduler.onResponseDone({ status: "failed" });
  assert.deepEqual(sent, []);

  scheduler.onSpeechStarted();
  scheduler.onSpeechStopped();
  scheduler.onAudioCommitted();
  assert.equal(sent.length, 1);
  assert.match(sent[0].response.instructions, /"event_id":22/);
});

test("events received before the data channel is ready stay queued", () => {
  const { scheduler, sent, setReady } = harness(false);

  scheduler.onTaskEvent(taskEvent({ event_id: 21 }));
  assert.deepEqual(sent, []);

  setReady(true);
  scheduler.onChannelReady();
  assert.equal(sent.length, 1);
});
