const SEMANTIC_VAD = {
  type: "semantic_vad",
  eagerness: "high",
  create_response: false,
  interrupt_response: true,
};

export class MediaContinuity {
  constructor({ sendRealtime, sendControl, microphoneTracks, isExactPeerHealthy }) {
    this.sendRealtime = sendRealtime;
    this.sendControl = sendControl;
    this.microphoneTracks = microphoneTracks;
    this.isExactPeerHealthy = isExactPeerHealthy;
    this.playbackId = null;
  }

  begin(playbackId) {
    if (typeof playbackId !== "string" || !/^[A-Za-z0-9_-]{8,128}$/u.test(playbackId)) {
      throw new Error("Invalid media playback id");
    }
    this.playbackId = playbackId;
    for (const track of this.microphoneTracks()) track.enabled = false;
    this.sendRealtime({
      type: "session.update",
      session: { type: "realtime", audio: { input: { turn_detection: null } } },
    });
    this.sendRealtime({ type: "input_audio_buffer.clear" });
    this.sendControl({ type: "media_ready", playback_id: playbackId, history: [] });
  }

  isActive() {
    return this.playbackId !== null;
  }

  finish(event) {
    if (!this.playbackId || event?.playback_id !== this.playbackId) return false;
    const playbackId = this.playbackId;
    this.playbackId = null;
    if (!this.isExactPeerHealthy()) return false;
    this.sendRealtime({ type: "input_audio_buffer.clear" });
    this.sendRealtime({
      type: "session.update",
      session: { type: "realtime", audio: { input: { turn_detection: SEMANTIC_VAD } } },
    });
    for (const track of this.microphoneTracks()) track.enabled = true;
    this.sendRealtime({
      type: "response.create",
      response: {
        instructions: event.next_episode_pending
          ? "Briefly say the episode ended and ask naturally whether to play the proposed next episode or do something else."
          : "Briefly acknowledge media ended and ask what the user would like to do next.",
      },
    });
    this.sendControl({ type: "media_resume_complete", playback_id: playbackId, mode: "exact" });
    return true;
  }

  clear() {
    this.playbackId = null;
  }
}
