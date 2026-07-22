import assert from "node:assert/strict";
import test from "node:test";

import { canApplySessionAnswer } from "../../src/realtime_action_spike/web/startup_guard.mjs";

test("accepts an SDP answer only for the current open peer", () => {
  const peer = { signalingState: "have-local-offer" };

  assert.equal(canApplySessionAnswer(peer, peer), true);
  assert.equal(canApplySessionAnswer(peer, { signalingState: "have-local-offer" }), false);
  assert.equal(canApplySessionAnswer(peer, { signalingState: "closed" }), false);
});

test("rejects a late SDP answer after teardown clears the active peer", () => {
  const cancelledPeer = { signalingState: "closed" };

  assert.equal(canApplySessionAnswer(null, cancelledPeer), false);
});
