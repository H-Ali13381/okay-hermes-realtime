import assert from "node:assert/strict";
import test from "node:test";

import { applySessionAnswer } from "../../src/realtime_action_spike/web/connection_lifecycle.mjs";

test("applies an SDP answer to the current open peer", async () => {
  const descriptions = [];
  const peer = {
    signalingState: "have-local-offer",
    async setRemoteDescription(description) {
      descriptions.push(description);
    },
  };

  const applied = await applySessionAnswer({
    activePeer: () => peer,
    expectedPeer: peer,
    response: { async text() { return "answer-sdp"; } },
  });

  assert.equal(applied, true);
  assert.deepEqual(descriptions, [{ type: "answer", sdp: "answer-sdp" }]);
});

test("discards an SDP answer when teardown revokes the peer lease", async () => {
  let currentPeer;
  let appliedDescription = false;
  const peer = {
    signalingState: "closed",
    async setRemoteDescription() {
      appliedDescription = true;
    },
  };
  currentPeer = peer;

  const applied = await applySessionAnswer({
    activePeer: () => currentPeer,
    expectedPeer: peer,
    response: {
      async text() {
        currentPeer = null;
        return "late-answer-sdp";
      },
    },
  });

  assert.equal(applied, false);
  assert.equal(appliedDescription, false);
});
