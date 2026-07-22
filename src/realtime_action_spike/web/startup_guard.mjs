export function canApplySessionAnswer(activePeer, expectedPeer) {
  return activePeer === expectedPeer && expectedPeer?.signalingState !== "closed";
}
