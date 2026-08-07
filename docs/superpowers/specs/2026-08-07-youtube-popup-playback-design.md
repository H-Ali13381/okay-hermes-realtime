# Voice-Launched YouTube Popup Playback

## Goal

Let a user say a natural request such as “play the latest Technology Connections video” and have Okay Hermes Realtime open the selected YouTube video in a temporary `mpv` popup.

The initial version prioritizes a small lifecycle over voice media control. It closes the Realtime/WebRTC microphone before playback begins but leaves the native `okay-hermes` detector running normally. Playback is controlled with `mpv` keyboard controls; voice pause, stop, and volume are deferred until speaker-output cancellation is designed and verified.

## Decision at a glance

| Concern | Decision |
|---|---|
| Normal voice input | Natural-language YouTube search query |
| Exact URL | Supported when already available, but not expected from speech |
| Search selection | Resolve the first YouTube video with `yt-dlp` |
| Player | `mpv`, opened as a small always-on-top temporary window |
| Start timing | Prepare the player paused; begin audio only after the Realtime microphone and browser session close |
| Wake-word behavior | Leave the native detector active; do not add a media-specific pause or inhibition state |
| Playback controls | `mpv` keyboard controls only (`space`, seek keys, `q`) |
| Concurrent playback | One owned player; a new play request replaces the previous one |
| Deferred work | Voice pause/stop/volume and PipeWire/acoustic echo cancellation |

## User-visible behavior

1. The user activates Hermes and says, for example, “Play the newest Linus Tech Tips video on YouTube.”
2. The Realtime model calls `media_play_youtube` with the spoken search text.
3. The controller resolves one result and prepares an `mpv` popup in a paused state.
4. Hermes says one short confirmation containing the resolved title, such as “Opening ‘The Framework Desktop’.”
5. The existing farewell/close path drains that spoken response and tears down the Realtime microphone and browser session.
6. Only after teardown completes does the controller unpause `mpv`.
7. The native `okay-hermes` detector continues running normally while `mpv` plays.
8. The user controls playback with the popup. Pressing `q`, closing the window, or reaching end-of-file exits `mpv` without requiring any listener re-arm transition.

Speaker audio may false-trigger the wake detector. This is an accepted model-quality failure rather than a reason to add a second listener lifecycle. A trigger follows the normal activation path even while `mpv` is playing. False activations should remain observable as potential hard-negative training examples through the existing wakeword data/evaluation workflow; this feature does not silently retain new audio or create a separate recording pipeline.

## Why `mpv` plus `yt-dlp`

`mpv` is a small command-driven player that accepts URLs, supports an always-on-top window, exposes an IPC socket, and exits naturally when playback ends. Its official manual documents URL playback, `--ontop`, geometry control, and the youtube-dl hook. Arch packages `mpv` and lists `yt-dlp` as the optional dependency for video-sharing-site playback.

`yt-dlp` supports YouTube search through the `ytsearch:` prefix. Resolving the result before playback lets the tool return the selected title and channel, reject no-result cases, and avoid claiming that an unknown video started.

References:

- <https://mpv.io/manual/stable>
- <https://archlinux.org/packages/extra/x86_64/mpv/>
- <https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md>

## Scope

### In scope

- One model-facing `media_play_youtube` capability.
- Natural-language search and exact YouTube watch URLs.
- First-video resolution with bounded `yt-dlp` execution.
- One controller-owned `mpv` subprocess and IPC socket.
- Fixed popup geometry and fixed safe player arguments.
- Paused preparation followed by playback only after voice teardown.
- An unchanged, continuously active native wake-word detector during playback.
- Deterministic replacement, shutdown, and stale-state cleanup.
- Installer/preflight, documentation, unit/contract tests, and a live voice smoke test.

### Out of scope

- Voice pause, resume, stop, seek, volume, or playlist controls.
- Keeping a Realtime microphone session open while video audio plays.
- A media-specific listener pause/inhibition marker, tray state, or re-arm path.
- PipeWire routing, echo-reference capture, WebRTC acoustic echo cancellation, or source separation.
- Playlists, autoplay, queues, recommendations, or background audio mode.
- Arbitrary websites, local files, model-selected executables, shell fragments, or model-selected `mpv`/`yt-dlp` flags.
- Downloading videos to permanent storage.
- Persisting watch history beyond behavior already inherent to the selected tools.

## Model-facing capability contract

The capability name is `media_play_youtube`. It accepts one bounded string:

```json
{
  "source": "latest Technology Connections video"
}
```

`source` is 1–300 characters. The handler classifies it deterministically:

- A valid `https` YouTube watch, Shorts, or `youtu.be` URL is treated as an exact URL.
- A string without a URL shape is treated as a search query.
- A string that looks like a URL but is not an allowlisted YouTube host is rejected rather than searched.

The tool description tells the model to pass the user’s title/channel/topic words as a search query. It must not ask a voice user to dictate a URL.

A successful prepared result contains bounded metadata:

```json
{
  "action": "media.youtube.play",
  "playback_id": "opaque-local-id",
  "title": "Resolved video title",
  "channel": "Resolved channel",
  "webpage_url": "https://www.youtube.com/watch?v=...",
  "status": "prepared",
  "end_session": true
}
```

`end_session: true` reuses the existing trusted close behavior: lock automatic interruption, create one brief continuation response, wait for matching output playback completion, then run normal browser/controller teardown. The model does not claim playback is audible yet; it says “Opening…” or equivalent.

## Architecture

```text
spoken request
      |
      v
OpenAI Realtime tool call: media_play_youtube(source)
      |
      v
scoped loopback POST /execute
      |
      +-- call-id replay/fingerprint checks
      +-- strict source validation
      v
MediaPlaybackManager.prepare(session_id, source)
      |
      +-- resolve first result with yt-dlp
      +-- validate canonical YouTube URL + bounded metadata
      +-- spawn controller-owned mpv paused
      +-- establish private IPC socket / confirm process alive
      v
function_call_output(status=prepared, end_session=true)
      |
      v
brief spoken confirmation -> existing drained voice teardown
      |
      v
MediaPlaybackManager.commit(playback_id)
      |
      +-- unpause mpv through private IPC
      +-- monitor exact PID/process handle
      v
mpv exit -> remove IPC + reap exact owned process
```

## Components

### 1. `YouTubePlayArguments`

A strict Pydantic arguments model with extra fields forbidden and one `source` field. Validation normalizes surrounding whitespace, rejects control characters, rejects overlong input, and distinguishes an allowlisted URL from ordinary search text.

### 2. `YouTubeResolver`

A narrow asynchronous wrapper around `yt-dlp`.

Responsibilities:

- Resolve at most one video.
- Use `asyncio.create_subprocess_exec` or an equivalent argv-only API; never invoke a shell.
- Apply a short configurable timeout and terminate/reap the resolver on timeout or cancellation.
- Return only bounded title, channel, canonical webpage URL, and video identifier.
- Validate the returned URL and identifier independently of `yt-dlp` output.
- Treat no result, malformed output, unsupported URL, authentication challenge, and timeout as explicit controlled failures.

The resolver may use browser cookies only through an explicit local configuration option. It does not read a personal browser profile by default and never logs cookies or signed media URLs.

### 3. `MediaPlaybackManager`

A controller-owned asynchronous service with one active playback slot.

Responsibilities:

- Verify that `mpv` and `yt-dlp` are available before advertising successful preparation.
- Replace an existing owned player through bounded terminate-then-kill teardown.
- Create an opaque playback ID and private runtime directory under `$XDG_RUNTIME_DIR/okay-hermes-realtime/media/` with mode `0700`.
- Spawn `mpv` with a fixed argv, no shell, a private IPC socket, and paused playback.
- Confirm the exact process remains alive and that its IPC endpoint becomes ready before returning `prepared`.
- Bind the prepared playback to the exact active voice session.
- Unpause only when that session finishes through the expected completed/model-request teardown path.
- Abort a prepared player if the owning voice session fails, is cancelled, becomes stale, or the service shuts down before commit.
- Monitor and reap the process, remove only its own runtime files, and expose bounded status for `/health` and diagnostics.

The model cannot supply player flags, geometry, paths, environment variables, IPC commands, or process identifiers.

### 4. Native listener remains independent

Media playback does not add a control channel, marker, pause state, status value, or re-arm transition to the native wake listener. PipeWire capture, health reporting, model-window handling, and activation behavior continue exactly as they do when no player exists.

If playback audio produces a false wake, the ordinary activation path runs. The event is useful evidence for wakeword evaluation and potential hard-negative training, but collection and labeling remain responsibilities of the existing wakeword workflow rather than this media feature. The media implementation must not silently begin retaining microphone audio.

### 5. Session-bound commit

Prepared playback is not audible until the voice session is gone. The controller’s existing exact-session lifecycle owns the commit decision:

- `prepared` result requests the existing drained close path.
- On successful matching session finalization, commit sends an unpause command over the private `mpv` IPC socket.
- On any mismatched, stale, failed, or cancelled session finalization, abort terminates the paused player.
- A stale teardown from an older session can never commit or terminate a newer playback generation.

This avoids timer-based audio races. There is no fixed “sleep two seconds and hope the microphone closed” delay.

## Fixed player policy

The final argv is constructed exclusively by trusted code. Its policy includes:

- paused startup;
- always-on-top popup;
- fixed bounded geometry near a screen corner;
- no terminal attachment;
- one canonical YouTube URL;
- one private IPC socket;
- no playlist expansion;
- no model-provided options;
- deterministic process-group ownership and teardown.

The exact geometry may be configurable by a trusted environment setting later, but it is not part of the model tool schema.

The first version should use `mpv`’s normal keyboard controls. `q` exits; closing the window has the same lifecycle result.

## Concurrency and replay

- The existing `/execute` execution scope, call limit, and `call_id` fingerprint cache remain authoritative.
- Replaying an identical tool call returns the cached preparation result and does not launch another player.
- Conflicting reuse of a `call_id` remains rejected.
- A new non-replayed play request replaces the previous owned player only after the new request has passed validation and resolution.
- Playback IDs prevent late watcher callbacks from clearing or terminating a newer player’s state.
- Only one playback manager instance belongs to the controller process.

## Error handling

| Failure | Behavior |
|---|---|
| `mpv` missing | Controlled tool failure naming the missing local prerequisite; no close request |
| `yt-dlp` missing | Controlled tool failure; no close request |
| Search timeout or no result | Controlled tool failure; keep voice session open |
| Non-YouTube URL | Reject; do not reinterpret as a search query |
| Invalid/malformed resolver output | Controlled failure with bounded diagnostics |
| Existing player will not terminate | Fail closed; do not launch a second unmanaged player |
| Player exits before preparation completes | Controlled tool failure; reap the owned process |
| IPC readiness timeout | Terminate/reap player, remove its runtime files, return failure |
| Voice teardown fails/cancels | Abort and reap the paused player |
| Player exits during playback | Reap it and remove matching runtime files; listener state is unchanged |
| Controller shutdown/restart | Kill/reap the owned player and recover stale player runtime state cleanly |
| Stale player watcher or stale session teardown | Ignore; it cannot mutate the current playback generation |

Errors returned to the model are concise and do not expose cookies, signed stream URLs, arbitrary stderr, runtime tokens, IPC paths, or local process details.

## Installation and configuration

`mpv` and `yt-dlp` become explicit runtime prerequisites. The installer/preflight checks both commands and reports distribution-neutral package names; it must not silently invoke a privileged package manager.

For the current Arch machine:

```bash
sudo pacman -S mpv yt-dlp
```

`yt-dlp` is already installed locally; `mpv` is not.

Potential trusted settings:

| Setting | Default | Purpose |
|---|---|---|
| `YOUTUBE_RESOLVE_TIMEOUT_SECONDS` | bounded short timeout | Search/metadata resolution budget |
| `MPV_BIN` | `mpv` resolved through PATH/known install paths | Player executable |
| `YT_DLP_BIN` | `yt-dlp` resolved through PATH/known install paths | Resolver executable |
| `YOUTUBE_BROWSER_COOKIES` | unset | Explicit opt-in browser cookie source for bot/age checks |

Popup geometry and player flags remain code-owned in the first version.

## Prompt behavior

The Realtime instructions and tool description should enforce:

- Use `media_play_youtube` for an explicit request to play a YouTube video.
- Put the user’s spoken title, channel, recency words, and topic into `source` without inventing a URL.
- Do not ask the user to dictate a URL.
- After a successful `prepared` result, acknowledge the returned title in one short sentence.
- Do not claim that playback is audible before session teardown commits it.
- On failure, explain the actionable reason briefly and keep the conversation open.
- Do not route this lightweight action through Hermes/Kanban.

## Testing

### Capability and resolver tests

- Tool catalog includes `media_play_youtube` with a strict schema.
- Natural queries and allowlisted exact YouTube URLs validate.
- Empty, overlong, control-character, non-YouTube URL, and extra-field inputs fail.
- Resolver uses argv execution without a shell and requests one result.
- Valid metadata is bounded and canonicalized.
- Timeout, cancellation, no-result, malformed JSON, challenge/authentication, and unsupported-result cases are controlled and reaped.

### Playback manager tests

- Fixed `mpv` argv contains paused startup, popup policy, canonical URL, and private IPC path.
- Model input never reaches argv as flags or paths.
- Preparation returns only after process/IPC readiness.
- Commit unpauses only the exact prepared generation.
- Failed/cancelled/stale session finalization aborts instead of committing.
- A new request replaces and reaps the old player.
- Normal exit, window close, EOF, crash, timeout, and shutdown all release only the matching generation.
- Identical replay never launches twice; call-ID conflict remains rejected.

### Native listener regression tests

- Media preparation, commit, player exit, and controller restart do not create listener markers or mutate listener state.
- Capture health and the normal inference loop remain active while playback exists.
- The tray continues using its existing controller/capture states; no media-specific listener state is added.

### Gateway and browser contract tests

- `/execute` remains loopback-, scope-, and session-bound.
- A successful media result carries `status=prepared` and `end_session=true`.
- The browser locks VAD and drains the one short confirmation before teardown, using the existing exact response-ID close behavior.
- No `mpv` unpause occurs before matching controller finalization.
- Stale teardown cannot commit newer playback.
- `/health` reports bounded media state without local paths or tokens.

### End-to-end smoke test

1. Confirm no owned player or stale media runtime state exists.
2. Activate by native wake word without headphones.
3. Say a distinctive search request.
4. Verify the expected resolved title is spoken.
5. Verify the Realtime microphone/browser close before `mpv` audio starts.
6. Verify one small always-on-top `mpv` window appears and plays the selected video.
7. While it plays, verify native listener capture and inference remain active and no media-specific listener state appears.
8. Press `q`.
9. Verify the exact player is reaped and its runtime IPC files are removed; the listener requires no re-arm transition.
10. Repeat with a second request, no-result query, missing-player simulation, and controller restart.

Final repository gates remain the project’s normal Python, web, native, packaging, systemd, and live-runtime checks.

## Deferred follow-up: voice media controls with speaker cancellation

Voice pause, stop, resume, seek, and volume are deliberately excluded from this version. They require Hermes to listen while media is audible, which is unsafe without removing speaker playback from the microphone path.

A future design must evaluate and verify a real audio-reference path, such as PipeWire/WebRTC acoustic echo cancellation using the player sink monitor as the far-end reference. It must measure residual playback leakage, speech recognition accuracy, latency, and behavior across speakers, headphones, Bluetooth, device changes, and high volume. Wake-word false activations during ordinary playback remain useful independent wake-model evidence. Simple noise suppression, prompt instructions, or subtracting arbitrary PCM without clock/drift handling are not acceptable substitutes.

Only after that audio path passes should `media_control` expose IPC-backed pause/stop/volume operations while a Realtime conversational microphone remains open.

## Approved decisions

- Natural spoken search is the primary interface; exact YouTube URLs are secondary.
- Resolve one result before playback and confirm the selected title.
- Use `mpv`, not VLC, Celluloid, or a browser popup.
- Prepare playback paused and start it only after the Realtime microphone closes.
- Leave the native `okay-hermes` detector active during playback; add no media-specific inhibition or re-arm lifecycle.
- Accept and observe speaker-induced false wakes as potential hard-negative training evidence rather than suppressing them in the media feature.
- Keep pause, stop, volume, and acoustic echo cancellation as explicit follow-up work.
