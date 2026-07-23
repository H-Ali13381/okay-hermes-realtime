export function canApplyRemoteAnswer(activePeer, expectedPeer) {
  return activePeer === expectedPeer && expectedPeer?.signalingState !== "closed";
}