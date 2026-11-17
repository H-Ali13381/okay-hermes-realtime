import assert from "node:assert/strict";
import test from "node:test";

import { MediaContinuity } from "../../frontend/media-continuity.js";

function harness() {
  const realtime = [];
  const control = [];
  const track = { enabled: true };
  const continuity = new MediaContinuity({
    sendRealtime(event) {
      realtime.push(event);
    },
    sendControl(event) {
      control.push(event);
    },
    microphoneTracks() {
      return [track];
    },
    isExactPeerHealthy() {
      return true;
    },
  });
  return { continuity, realtime, control, track };
}

test("accepted handoff suspends input before media_ready and creates no response", () => {
  const { continuity, realtime, control, track } = harness();

  continuity.begin("pending-anime-01");

  assert.equal(continuity.isActive(), true);
  assert.equal(track.enabled, false);
  assert.deepEqual(realtime, [
    {
      type: "session.update",
      session: { type: "realtime", audio: { input: { turn_detection: null } } },
    },
    { type: "input_audio_buffer.clear" },
  ]);
  assert.deepEqual(control, [
    { type: "media_ready", playback_id: "pending-anime-01", history: [] },
  ]);
  assert.equal(realtime.some((event) => event.type === "response.create"), false);
});

test("matching completion restores exact peer and requests one next-episode turn", () => {
  const { continuity, realtime, control, track } = harness();
  continuity.begin("pending-anime-01");
  realtime.length = 0;
  control.length = 0;

  const handled = continuity.finish({
    type: "media_finished",
    playback_id: "pending-anime-01",
    outcome: "completed",
    next_episode_pending: true,
  });

  assert.equal(handled, true);
  assert.equal(continuity.isActive(), false);
  assert.equal(track.enabled, true);
  assert.deepEqual(realtime[0], {
    type: "input_audio_buffer.clear",
  });
  assert.equal(realtime[1].type, "session.update");
  assert.equal(realtime[2].type, "response.create");
  assert.match(realtime[2].response.instructions, /next episode/i);
  assert.deepEqual(control, [
    { type: "media_resume_complete", playback_id: "pending-anime-01", mode: "exact" },
  ]);
});

test("stale completion cannot restore or mutate a newer generation", () => {
  const { continuity, realtime, control, track } = harness();
  continuity.begin("pending-anime-02");
  realtime.length = 0;
  control.length = 0;

  assert.equal(continuity.finish({
    type: "media_finished",
    playback_id: "pending-anime-01",
    outcome: "completed",
    next_episode_pending: true,
  }), false);
  assert.equal(track.enabled, false);
  assert.deepEqual(realtime, []);
  assert.deepEqual(control, []);
});
