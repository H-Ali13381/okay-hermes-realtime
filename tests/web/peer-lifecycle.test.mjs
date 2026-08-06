import assert from "node:assert/strict";
import test from "node:test";

import { canApplyRemoteAnswer } from "../../frontend/peer-lifecycle.js";

function peer(signalingState = "have-local-offer") {
  return { signalingState };
}

test("accepts an answer only for the current open peer", () => {
  const expectedPeer = peer();

  assert.equal(canApplyRemoteAnswer(expectedPeer, expectedPeer), true);
});

test("rejects an answer for a replaced peer", () => {
  assert.equal(canApplyRemoteAnswer(peer(), peer()), false);
});

test("rejects an answer for a closed peer", () => {
  const expectedPeer = peer("closed");

  assert.equal(canApplyRemoteAnswer(expectedPeer, expectedPeer), false);
});

test("rejects an answer after the active peer is cleared", () => {
  assert.equal(canApplyRemoteAnswer(null, peer()), false);
});
