export async function applySessionAnswer({ activePeer, expectedPeer, response }) {
  const answerSdp = await response.text();
  if (activePeer() !== expectedPeer || expectedPeer.signalingState === "closed") {
    return false;
  }

  await expectedPeer.setRemoteDescription({ type: "answer", sdp: answerSdp });
  return true;
}
