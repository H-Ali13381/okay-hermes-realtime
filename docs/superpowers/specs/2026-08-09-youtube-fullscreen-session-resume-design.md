# Fullscreen YouTube Playback with Voice-Session Resume

## Status

Approved design for the follow-up to `2026-08-07-youtube-popup-playback-design.md`.
This document supersedes that design where it previously required voice-session teardown before
playback and wakeword-only behavior after the player exits. The existing resolver, strict tool
input, process ownership, replay protection, and shutdown guarantees remain in force.

## Goal

Make voice-launched YouTube playback feel continuous:

1. show the resolved video as a paused regular-size `mpv` preview;
2. after Hermes finishes its spoken confirmation, switch the player to fullscreen and autoplay;
3. when the video finishes or is closed, resume the same voice conversation and ask a contextual
   follow-up;
4. if the exact provider session expired or disconnected, reconstruct its bounded conversational
   context inside the same local Hermes session before asking the follow-up.

## User-visible behavior

1. The user asks Hermes to play a YouTube video.
2. Hermes resolves one video and opens a paused, always-on-top `640x360` `mpv` preview displaying
   the loaded video.
3. Hermes gives one brief spoken confirmation naming the selected video.
4. Only after the matching assistant output has finished playing, the voice page stops sending
   microphone audio and disables automatic turn detection.
5. The controller sends two ordered commands over the private `mpv` IPC socket:
   `fullscreen=true`, then `pause=false`.
6. The video plays fullscreen. Hermes does not listen, answer, or generate billable Responses while
   playback is active.
7. EOF, `q`, the window close button, or a player failure all converge on the owned-process watcher.
8. If the original Realtime connection is healthy, Hermes restores that exact conversation. If it
   is unavailable, Hermes establishes a replacement Realtime connection and hydrates bounded
   conversation history.
9. Hermes restores listening and asks one brief contextual follow-up, such as "What would you like
   to do next?"

## Decisions

| Concern | Decision |
|---|---|
| Initial player presentation | Paused `640x360`, always-on-top preview |
| Fullscreen timing | After the matching spoken confirmation finishes |
| Autoplay timing | Immediately after the fullscreen IPC command is accepted |
| Fullscreen verification | No compositor polling; accepted mpv IPC is sufficient |
| Voice behavior during playback | Keep local session alive; disable microphone and VAD |
| Normal continuation | Preserve exact WebRTC/Realtime session |
| Provider expiry/disconnect | Reconnect and hydrate bounded context |
| Post-video behavior | Restore listening and request one contextual follow-up |
| Additional wake activations | Existing busy outcome while local session is active |
| Billing while paused | No Responses or committed audio; connection alone is not billed |
| Shutdown | Existing teardown coordinator remains authoritative; never resume |

## Architecture

```text
media_play_youtube(source)
        |
        v
resolve video -> spawn paused 640x360 mpv -> return media_handoff=true
        |
        v
Hermes speaks selected title
        |
        v
matching output_audio_buffer.stopped
        |
        +-- disable microphone track
        +-- set turn_detection=null
        +-- clear uncommitted input audio
        +-- snapshot bounded continuation context
        +-- send media_ready(session_id, playback_id)
        |
        v
controller validates exact session + generation
        |
        +-- mpv IPC: set fullscreen=true
        +-- mpv IPC: set pause=false
        v
fullscreen playback
        |
        v
owned mpv process exits
        |
        +-- exact Realtime peer healthy -> restore it
        |
        +-- peer unavailable -> create replacement + hydrate context
        v
restore microphone + normal VAD -> request contextual follow-up
```

## Session ownership and state

The local `VoiceSessionController` remains the authoritative owner of one local wake session.
Media playback no longer enters the terminal teardown path.

The active session gains an internal media phase:

- `idle`: no prepared playback;
- `prepared`: paused preview exists and is bound to the session/playback generation;
- `playing`: the browser handoff completed and the player was committed;
- `resuming`: the player exited and the browser is restoring or reconstructing Realtime;
- `failed`: resume failed and terminal teardown must return the system to wakeword mode.

This is an internal substate. Existing external controller status and tray contracts remain stable;
the controller can continue to report the voice session as live while media is active. A new
activation remains busy until resumption or terminal teardown completes.

Media state transitions are generation-bound by both `session_id` and `playback_id`. A stale browser
message, process watcher, provider event, or teardown cannot mutate a newer generation.

## Tool-result contract

A successful `media_play_youtube` preparation no longer requests terminal voice teardown. Its result
uses an explicit media handoff marker:

```json
{
  "action": "media.youtube.play",
  "playback_id": "opaque-local-id",
  "title": "Resolved video title",
  "channel": "Resolved channel",
  "webpage_url": "https://www.youtube.com/watch?v=...",
  "status": "prepared",
  "media_handoff": true
}
```

The old `end_session: true` field is removed from this result. Other tools that legitimately end a
session keep their existing `end_session` behavior.

## Browser-to-controller handoff

After the successful tool output, the browser requests one explicit assistant continuation for the
spoken confirmation and latches its response ID. It waits for the matching
`output_audio_buffer.stopped`, not merely `response.done`, so generated-but-unplayed speech cannot be
cut off.

The browser then performs this order:

1. set every local microphone track's `enabled` property to `false`;
2. send `session.update` with `type: "realtime"` and `turn_detection: null`;
3. send `input_audio_buffer.clear`;
4. create a bounded in-memory continuation snapshot;
5. send `media_ready` over the existing authenticated control WebSocket.

Disabling the local track is the immediate privacy and feedback boundary. The handoff does not need
to wait for a provider acknowledgement because no microphone audio is transmitted after that point.
There is no sleep-based delay.

`media_ready` contains only the exact local session ID, playback ID, and bounded continuation data.
It carries no executable player options, local paths, provider credentials, signed stream URLs, or
raw audio.

## Fullscreen autoplay

`mpv` starts with the existing trusted preview policy:

- `--pause=yes`;
- `--force-window=yes`;
- `--ontop`;
- `--geometry=640x360-32-32`;
- private IPC socket;
- canonical resolved YouTube URL;
- no shell and no user/model-provided flags.

Fullscreen is not a startup flag. Once `media_ready` is validated, the manager sends two separate,
ordered IPC commands:

```text
["set_property", "fullscreen", true]
["set_property", "pause", false]
```

The second command is sent only after mpv accepts the first. The controller does not poll the
compositor or wait for a visual fullscreen acknowledgement. If either IPC command fails, the player
is stopped and the browser receives a failure/resume event instead of leaving an unmanaged paused
window.

## Player completion and resume

The exact owned player watcher handles every process exit. It clears matching runtime state under
the media lock, then invokes the controller completion callback after releasing that lock. This
prevents callback/manager lock inversion.

The controller sends one outbound `media_finished` event containing the exact session ID, playback
ID, and a bounded outcome (`completed` or `failed`). Manual close, `q`, and normal EOF may all appear
as `completed`; the user-visible requirement is resumption rather than distinguishing those paths.
An abnormal exit may be represented as `failed` without exposing raw process diagnostics.

On `media_finished`, the browser first clears any residual provider input buffer. It then chooses one
of two paths.

### Exact continuation

If the peer connection is connected and its data channel is open:

1. restore the normal semantic-VAD configuration;
2. enable the existing microphone track;
3. create one Response with narrow instructions to acknowledge the video ending if relevant and ask
   one brief contextual follow-up.

The conversation, tools, and provider session ID remain unchanged.

### Reconstructed continuation

If the provider peer expired, failed, or closed while media was active, the browser does not tear
down the local Hermes session. It closes only the failed provider transport, establishes a new
Realtime peer through the existing loopback `/session` boundary, and hydrates bounded history before
restoring listening.

The continuation snapshot is memory-only and contains at most:

- the latest 24 completed user/assistant transcript items;
- 16,000 aggregate characters, dropping oldest items first;
- one sanitized media outcome summary containing only title, channel, and completion state.

It excludes raw audio, credentials, execution scopes, call IDs, signed URLs, arbitrary tool payloads,
and incomplete transcript deltas. Supported user and assistant text items are replayed in order with
`conversation.item.create`. The media outcome is supplied as bounded trusted continuation context.
After hydration succeeds, the browser restores normal VAD and microphone input and requests the same
contextual follow-up.

The replacement receives a new OpenAI session ID, but it remains part of the same controller-owned
local Hermes session. The gateway rotates the execution scope for the new provider peer so stale
browser/provider calls cannot execute tools.

## Cost and lifetime behavior

OpenAI documents that conversational Realtime costs accrue when a Response is created, that there is
currently no cost for network bandwidth or connections, and that empty audio does not count as input
tokens unless committed. Input transcription is billed when audio is committed.

While media plays, this design disables the microphone track, disables VAD, clears uncommitted input,
and creates no Responses. Therefore, the retained idle connection should accrue no new Realtime
usage during playback. Normal context/input and output charges resume with the post-video Response.

The provider's documented maximum Realtime session duration is 60 minutes. Muting does not stop that
lifetime. The reconstruction path handles expiry or an ordinary provider transport failure.

References:

- <https://developers.openai.com/api/docs/guides/realtime-costs>
- <https://developers.openai.com/api/docs/guides/realtime-conversations>

## Failure handling

| Failure | Behavior |
|---|---|
| Preview preparation fails | Controlled tool failure; voice session remains live |
| Confirmation response fails/cancels | Abort prepared player; keep or restore voice session |
| Browser sends stale `media_ready` | Reject and abort only the stale prepared generation |
| Fullscreen IPC fails | Stop/reap player; restore voice and report concise failure |
| Unpause IPC fails | Stop/reap player; restore voice and report concise failure |
| Player exits before handoff | Clear exact generation; browser resumes without starting media |
| Player exits normally/manually | Send one `media_finished`; resume and ask follow-up |
| Player crashes | Send bounded failed outcome; resume and briefly acknowledge failure |
| Provider fails during playback | Preserve local session and reconstruct after player exit |
| Reconstruction fails | One bounded failure; terminal teardown; return to wakeword mode |
| Browser/control channel is lost | Abort owned media and use authoritative terminal teardown |
| Controller/service/tray shutdown | Stop/reap media, close voice resources, never request follow-up |
| Stale watcher/event | Ignore; cannot mutate current generation |

No failure path uses an unbounded retry loop. Provider reconstruction gets one attempt under the
existing startup timeout. All process and IPC cleanup remains bounded.

## Native listener and tray behavior

The native wake detector remains independent and active, as in the original media design. Because
the local voice session remains active during playback, additional wake activations receive the
existing busy result and cannot open a competing session.

No media-specific listener marker, PipeWire routing, voice media controls, or tray menu is added.
Tray/service Exit remains authoritative and suppresses media resumption.

## Observability

Session traces add bounded, secret-free events for:

- `media_preview_prepared`;
- `media_handoff_ready`;
- `media_playback_started`;
- `media_playback_finished`;
- `media_resume_exact`;
- `media_resume_reconstructed`;
- `media_resume_failed`.

Ordering evidence records IDs and bounded outcomes only. It does not record local paths, provider
credentials, execution scopes, signed URLs, or raw transcripts. Existing controller and player
health endpoints remain bounded.

## Testing

### Media manager

- Preview argv remains paused, regular-size, always-on-top, and private.
- Commit sends `fullscreen=true` before `pause=false`.
- Unpause is not attempted when fullscreen IPC fails.
- Each IPC failure stops and reaps the exact player.
- EOF, `q`/window close simulation, and abnormal exit notify completion once.
- Completion callbacks run outside the manager lock.
- Stale watchers cannot clear or notify for a newer generation.

### Protocol and controller

- `media_ready` validates exact session and playback IDs.
- Media playback does not invoke terminal teardown.
- The internal media phase follows prepared -> playing -> resuming -> idle.
- `media_finished` is delivered once for the matching generation.
- New activation remains busy during playback/resumption.
- Provider failure during playback does not close the local session.
- Browser/control loss, service shutdown, and tray Exit stop media and never resume.
- Stale messages cannot resume or terminate newer media.

### Browser and gateway

- Successful media output uses `media_handoff`, not `end_session`.
- Handoff waits for matching `output_audio_buffer.stopped`.
- Microphone disable, VAD disable, and input clear precede `media_ready`.
- No Response is created while media is playing.
- Healthy-peer completion restores exact session and asks one follow-up.
- Failed-peer completion rotates execution scope, reconnects, hydrates bounded history, restores
  listening, and asks one follow-up.
- Hydration excludes incomplete items and sensitive fields and enforces item/character limits.
- Reconstruction failure tears down once without retrying.

### Repository and live verification

Run the project's normal Python, web, native, packaging, systemd, and lint gates. Then exercise the
actual Realtime text-input path and real player:

1. request a known YouTube video;
2. verify a paused `640x360` preview appears;
3. verify Hermes finishes its confirmation before fullscreen/unpause;
4. close the player manually and verify exact-session contextual follow-up;
5. repeat through natural EOF;
6. force provider disconnection while media plays and verify reconstructed continuation;
7. verify no Realtime Response or committed input occurs during playback;
8. trigger tray/service shutdown during playback and verify all resources stop with no follow-up;
9. inspect session traces and journals for ordering, bounded diagnostics, and cleanup.

## Acceptance criteria

- Video appears first as a paused regular-size preview.
- It becomes fullscreen and starts only after Hermes's confirmation audio stops.
- The microphone is not transmitted and no Responses are created during playback.
- Closing or finishing the video resumes the same local conversation automatically.
- A healthy provider peer continues exactly; an unavailable peer is reconstructed once from bounded
  context.
- Hermes restores listening and asks one contextual follow-up.
- Shutdown never triggers resumption.
- Existing singleton lock, activation socket, health marker, listener, tray Exit, and bounded process
  ownership regressions remain green.

## Non-goals

- Voice pause, seek, volume, or stop commands during playback.
- Listening to the user while video audio plays.
- Provider-session persistence beyond the provider's lifetime.
- Bit-for-bit reconstruction of undocumented provider-internal state.
- Persisting conversation history to disk or across controller restarts.
- Playlists, queues, recommendations, or background audio.
- Compositor-specific fullscreen detection or animation control.
