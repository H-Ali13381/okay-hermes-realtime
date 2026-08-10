# Fullscreen YouTube Playback with Voice-Session Resume Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Turn the existing paused YouTube preview into fullscreen playback only after Hermes finishes speaking, then resume the same local voice conversation through the exact Realtime peer or one bounded context reconstruction.

**Architecture:** Keep the controller-owned `mpv` process and local voice session alive across media playback. Add generation-bound media protocol messages, an internal controller media substate, an event-driven player-completion callback, and a browser continuity layer that mutes rather than tears down Realtime. Preserve the exact peer normally; rotate the execution scope and hydrate bounded transcript context only after provider failure or expiry.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, FastAPI, OpenAI Realtime WebRTC, browser ES modules, Node test runner, `mpv` JSON IPC, pytest, systemd user services.

**Design:** `docs/superpowers/specs/2026-08-09-youtube-fullscreen-session-resume-design.md`

---

## Scope guard

Allowed production changes:

- `src/realtime_action_spike/media.py`
- `src/realtime_action_spike/runtime/protocol.py`
- `src/realtime_action_spike/runtime/controller.py`
- `src/realtime_action_spike/gateway.py`
- `src/realtime_action_spike/config.py`
- `frontend/voice.js`
- `frontend/task-turn-scheduler.js`
- one focused new browser helper under `frontend/`
- generated `src/realtime_action_spike/web/voice.js`
- focused tests and media documentation

Immutable unless a failing verification proves otherwise:

- singleton-lock semantics and exit codes;
- activation-socket contract;
- wakeword capture/inference lifecycle;
- tray ON/OFF/Exit behavior;
- controller health-marker ownership;
- unrelated tool execution and Kanban behavior;
- model-facing `media_play_youtube(source)` input schema.

Do not add a shell path, PID lookup, playlist/queue behavior, voice media controls, permanent transcript storage, compositor polling, or an unbounded reconnect loop.

---

### Task 1: Make player commit fullscreen before unpause

**Objective:** Preserve the regular paused preview, then make the trusted commit path issue ordered fullscreen and unpause commands.

**Files:**
- Modify: `src/realtime_action_spike/media.py:285-588`
- Modify: `tests/test_media.py:404-736`

**Step 1: Write failing ordered-commit tests**

Update the current commit test and add a fullscreen-failure regression:

```python
@pytest.mark.asyncio
async def test_commit_enters_fullscreen_before_unpausing(tmp_path: Path) -> None:
    process = FakePlayerProcess(31002)
    manager, _resolver, _factory, ipc, _signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
    )
    prepared = await manager.prepare("voice-session-1", "video")

    await manager.commit("voice-session-1", prepared.playback_id)

    assert [command for _path, command in ipc.commands] == [
        ["set_property", "fullscreen", True],
        ["set_property", "pause", False],
    ]
    assert manager.status["status"] == "playing"
```

Extend `RecordingIpc` so it can fail on a selected command index. Add:

```python
@pytest.mark.asyncio
async def test_fullscreen_failure_never_unpauses_and_reaps_player(tmp_path: Path) -> None:
    process = FakePlayerProcess(31011)
    ipc = RecordingIpc(fail_on_command=0)
    manager, _resolver, _factory, _ipc, signaler = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected title")],
        processes=[process],
        ipc=ipc,
    )
    prepared = await manager.prepare("voice-session-1", "video")

    with pytest.raises(MediaPlaybackError, match="simulated IPC failure"):
        await manager.commit("voice-session-1", prepared.playback_id)

    assert len(ipc.commands) == 1
    assert ipc.commands[0][1] == ["set_property", "fullscreen", True]
    assert signaler.calls[0][0] == process.pid
    assert manager.status == {"status": "idle"}
```

Keep the preview assertions: `--pause=yes`, `--geometry=640x360-32-32`, and `--ontop`. Assert no startup `--fullscreen` flag.

Add a second failure regression using `StubbornGroupSignaler`: fullscreen failure must still send no
unpause and must attempt TERM then KILL, but when the process remains alive the manager must retain
the exact prepared player and non-idle status. It must never clear ownership merely to satisfy an
idle-state assertion. After the fake process is explicitly exited, `shutdown()` must cleanly release
the retained generation.

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/test_media.py -k 'fullscreen or commit_unpauses'
```

Expected: the existing implementation records only `pause=false` and does not satisfy the ordered commands.

**Step 3: Implement the ordered IPC commit**

Change `_commit_active_locked` to:

```python
async def _commit_active_locked(self, player: _OwnedPlayer) -> None:
    try:
        await self._send_ipc(
            player.ipc_path,
            ["set_property", "fullscreen", True],
            self._ipc_timeout_seconds,
        )
        await self._send_ipc(
            player.ipc_path,
            ["set_property", "pause", False],
            self._ipc_timeout_seconds,
        )
    except BaseException:
        await self._stop_active_locked()
        raise
    player.status = "playing"
```

Do not add fullscreen to `_build_mpv_argv`. On either IPC failure, use the existing bounded
stop/reap path and re-raise. A successfully reaped player returns to idle; a process still alive
after forced-termination timeout remains exactly owned and non-idle so it cannot become an orphan.
Do not poll `get_property` or the compositor.

**Step 4: Run focused verification**

```bash
uv run ruff check src/realtime_action_spike/media.py tests/test_media.py
uv run pytest -q tests/test_media.py
```

Expected: all media tests pass.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/media.py tests/test_media.py
git commit -m "feat: fullscreen media before playback"
```

---

### Task 2: Add generation-bound player completion delivery

**Objective:** Notify the controller exactly once when the exact owned player exits, without invoking callbacks while the media lock is held.

**Files:**
- Modify: `src/realtime_action_spike/media.py`
- Modify: `tests/test_media.py`

**Step 1: Write failing completion tests**

Add tests for normal exit, abnormal exit, intentional shutdown, and stale watchers:

```python
@pytest.mark.asyncio
async def test_player_exit_notifies_once_after_state_is_cleared(tmp_path: Path) -> None:
    process = FakePlayerProcess(31008)
    completions: list[PlaybackCompletion] = []
    manager, *_ = _manager(
        tmp_path,
        videos=[_video("dQw4w9WgXcQ", "Selected")],
        processes=[process],
    )
    async def record_completion(completion: PlaybackCompletion) -> None:
        completions.append(completion)

    manager.bind_completion_handler(record_completion)
    prepared = await manager.prepare("voice-session-1", "video")
    await manager.commit("voice-session-1", prepared.playback_id)

    process.exit(0)
    await _wait_until(lambda: len(completions) == 1)

    assert manager.status == {"status": "idle"}
    assert completions == [
        PlaybackCompletion(
            session_id="voice-session-1",
            playback_id=prepared.playback_id,
            title="Selected",
            channel="Channel",
            outcome="completed",
        )
    ]
```

Also prove:

- nonzero return code produces `outcome="failed"`;
- `shutdown()` and `stop_for_session()` cancel the watcher and produce no completion callback;
- an older watcher cannot notify after replacement;
- handler exceptions are logged and do not resurrect media state;
- a second `bind_completion_handler` call is rejected.

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/test_media.py -k 'notifies or completion or stop_for_session'
```

Expected: `PlaybackCompletion`, binding, and exact-session stop do not exist.

**Step 3: Implement completion and intentional stop**

Add:

```python
PlaybackOutcome = Literal["completed", "failed"]


@dataclass(frozen=True, slots=True)
class PlaybackCompletion:
    session_id: str
    playback_id: str
    title: str
    channel: str
    outcome: PlaybackOutcome
```

Store one optional completion handler. In `_watch_player`, await the process, clear only the exact player and runtime path under `_lock`, then call the captured handler after leaving `_lock`. Use return code `0` as completed and any other code as failed.

Add:

```python
async def stop_for_session(self, session_id: str, playback_id: str) -> None:
    async with self._lock:
        player = self._active
        if (
            player is not None
            and player.session_id == session_id
            and player.playback_id == playback_id
        ):
            await self._stop_active_locked()
```

Intentional stop cancels the watcher before terminating the process, so it cannot emit `media_finished`.

**Step 4: Run focused verification**

```bash
uv run ruff check src/realtime_action_spike/media.py tests/test_media.py
uv run pytest -q tests/test_media.py
```

**Step 5: Commit**

```bash
git add src/realtime_action_spike/media.py tests/test_media.py
git commit -m "feat: report owned media completion"
```

---

### Task 3: Define the media continuity protocol

**Objective:** Add strict, bounded browser/controller messages for media readiness, cancellation, completion, and resume acknowledgement.

**Files:**
- Modify: `src/realtime_action_spike/runtime/protocol.py:53-263`
- Modify: `tests/runtime/test_protocol.py`

**Step 1: Write failing protocol tests**

Add coverage for these exact models:

```python
class ContinuationItem(_StrictBaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=16_000)


class MediaReadyMessage(_StrictBaseModel):
    type: Literal["media_ready"] = "media_ready"
    session_id: str
    playback_id: str
    history: list[ContinuationItem] = Field(max_length=24)


class MediaCancelMessage(_StrictBaseModel):
    type: Literal["media_cancel"] = "media_cancel"
    session_id: str
    playback_id: str
    reason: Literal["confirmation_failed", "confirmation_timed_out", "browser_failure"]


class MediaFinishedMessage(_StrictBaseModel):
    type: Literal["media_finished"] = "media_finished"
    session_id: str
    playback_id: str
    outcome: Literal["completed", "failed"]


class MediaResumeCompleteMessage(_StrictBaseModel):
    type: Literal["media_resume_complete"] = "media_resume_complete"
    session_id: str
    playback_id: str
    mode: Literal["exact", "reconstructed"]
```

Tests must reject:

- provider-looking or malformed session IDs;
- malformed playback IDs;
- unknown fields;
- more than 24 history items;
- more than 16,000 aggregate history characters;
- blank/control-character history text;
- secret-shaped keys and arbitrary nested tool data;
- stale expected session IDs during `parse_loopback_message`.

Test encode/parse round trips for every message and include them in `LoopbackMessage`.

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/runtime/test_protocol.py -k media
```

Expected: imports/collection fail because the media protocol types do not exist.

**Step 3: Implement strict validators**

Use one `_PLAYBACK_ID_RE` allowing 8–128 URL-safe characters. Normalize transcript whitespace without rewriting legitimate punctuation. Add a model-level validator on `MediaReadyMessage`:

```python
@model_validator(mode="after")
def _validate_history_budget(self) -> MediaReadyMessage:
    if sum(len(item.text) for item in self.history) > 16_000:
        raise ValueError("continuation history exceeds 16000 characters")
    return self
```

Do not put titles, URLs, paths, call IDs, execution scopes, or raw tool payloads in these messages.

**Step 4: Run focused verification**

```bash
uv run ruff check src/realtime_action_spike/runtime/protocol.py tests/runtime/test_protocol.py
uv run pytest -q tests/runtime/test_protocol.py
```

**Step 5: Commit**

```bash
git add src/realtime_action_spike/runtime/protocol.py tests/runtime/test_protocol.py
git commit -m "feat: add media continuity protocol"
```

---

### Task 4: Move controller media playback out of terminal teardown

**Objective:** Make `media_ready` commit the exact prepared preview while keeping the local voice session live, then relay exact player completion to the browser.

**Files:**
- Modify: `src/realtime_action_spike/media.py:285-302`
- Modify: `src/realtime_action_spike/runtime/controller.py:60-828`
- Modify: `src/realtime_action_spike/gateway.py:112-203` only if the generic relay regression exposes a defect
- Modify: `tests/test_media.py`
- Modify: `tests/runtime/test_controller.py:90-348`
- Modify: `tests/integration/test_session_failures.py`
- Modify: `tests/test_gateway.py`

**Step 1: Replace the teardown-based media test with RED continuity tests**

Replace `test_youtube_playback_prepares_and_commits_after_model_teardown` with tests proving:

```python
@pytest.mark.asyncio
async def test_media_ready_commits_without_tearing_down_local_session() -> None:
    controller, media, session_id = await live_media_controller()
    result = await controller.prepare_youtube_playback(
        session_id=session_id,
        source="latest useful video",
    )

    assert result["media_handoff"] is True
    assert "end_session" not in result

    ready = MediaReadyMessage(
        session_id=session_id,
        playback_id=result["playback_id"],
        history=[ContinuationItem(role="user", text="Play the latest useful video")],
    )
    await controller.process_control_message(session_id, encode_loopback_message(ready))

    assert controller.active_session_id == session_id
    assert controller.status == "live"
    assert media.commit_calls == [
        {"session_id": session_id, "playback_id": result["playback_id"]}
    ]
    assert media.phase == "playing"
    assert launcher.handles[0].closed_calls == 0
```

Add tests that:

- mismatched playback ID is rejected without committing;
- duplicate `media_ready` is idempotent or rejected without a second commit;
- media completion queues one matching `MediaFinishedMessage` and moves to `resuming`;
- completion for an old generation is ignored;
- `media_resume_complete` clears the exact media generation and records exact/reconstructed mode;
- new activation returns busy through prepared/playing/resuming;
- `media_cancel` stops only the exact paused generation and leaves voice live;
- button/service/tray teardown stops prepared or playing media and never queues resume;
- player exit racing terminal teardown cannot leave or deliver a resumable `media_finished`;
- terminal teardown removes an already queued `MediaFinishedMessage` before the next outbound read;
- a full outbound queue fails closed through one terminal teardown instead of silently dropping the
  critical `MediaFinishedMessage` or leaving the session stuck in `resuming`;
- commit failure queues a bounded failed completion and leaves the voice session resumable;
- no controller lock is held while awaiting media IPC.

Add a media result contract test in `tests/test_media.py` proving
`PreparedPlayback.tool_result()` contains `media_handoff: true` and contains no `end_session` field.

Add a gateway relay regression proving a queued `MediaFinishedMessage` is sent over the
existing control WebSocket without closing it, then the socket continues accepting a matching
`media_resume_complete`. The generic relay must not special-case this as terminal output.

Update `RecordingMediaManager` with `bind_completion_handler`, `commit`, and `stop_for_session` recording.

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/runtime/test_controller.py tests/integration/test_session_failures.py \
  tests/test_gateway.py -k media
```

Expected: media still commits from teardown and new protocol messages are not handled.

**Step 3: Implement the internal media substate**

Add to `_WakeSession`:

```python
media_phase: Literal["idle", "prepared", "playing", "resuming", "failed"] = "idle"
prepared_playback_id: str | None = None
continuation_history: tuple[ContinuationItem, ...] = ()
```

Bind the manager completion handler in controller construction. Update preparation to set `prepared` and return `media_handoff`, not `end_session`.

Make the producer change in `PreparedPlayback.tool_result()` itself; the controller must not rewrite
the media manager's result after the fact.

Handle `MediaReadyMessage` in two phases:

1. under the controller lock, validate exact session/generation and capture the commit arguments;
2. release the controller lock and await `MediaPlaybackManager.commit`;
3. reacquire the lock, verify the same active session is still non-terminal, then mark `playing` and trace `media_playback_started`.

If teardown wins the race, stop the exact media generation instead of reviving it.

The completion handler must acquire only the controller lock, verify exact session/playback/phase, set `resuming`, and enqueue one `MediaFinishedMessage`. It must never call media-manager methods from inside the callback.

`MediaFinishedMessage` is critical control traffic. If the bounded outbound queue is full, mark the
media phase failed and start one terminal teardown after releasing the controller lock; never leave
the session in `resuming` and never silently drop the event. As soon as terminal teardown begins,
invalidate the active media generation and remove queued `MediaFinishedMessage` values for that
generation before publishing terminal `StopMessage`. `MediaResumeCompleteMessage` is inbound and
must be rejected once terminal teardown has started.

On terminal teardown, call `stop_for_session` for both prepared and playing media. Remove the old “commit from `finalize_media` when reason is MODEL_REQUEST” behavior.

**Step 4: Run focused verification**

```bash
uv run ruff check src/realtime_action_spike/runtime/controller.py tests/runtime/test_controller.py \
  src/realtime_action_spike/media.py tests/test_media.py \
  tests/integration/test_session_failures.py tests/test_gateway.py
uv run pytest -q tests/runtime/test_controller.py tests/runtime/test_teardown.py \
  tests/test_media.py tests/integration/test_session_failures.py tests/test_gateway.py
```

**Step 5: Commit**

```bash
git add src/realtime_action_spike/media.py src/realtime_action_spike/runtime/controller.py \
  tests/test_media.py tests/runtime/test_controller.py \
  tests/integration/test_session_failures.py tests/test_gateway.py
git commit -m "feat: keep voice session alive during media"
```

---

### Task 5: Suspend task announcements while media owns the output

**Objective:** Guarantee that background task events cannot create Realtime Responses during fullscreen playback.

**Files:**
- Modify: `frontend/task-turn-scheduler.js`
- Modify: `tests/web/task-turn-scheduler.test.mjs`

**Step 1: Write failing scheduler tests**

```javascript
test("suspension queues task events without creating a response", () => {
  const { scheduler, sent } = harness();
  scheduler.setSuspended(true);

  scheduler.onTaskEvent(taskEvent({ event_id: 31 }));
  scheduler.requestResponse();

  assert.deepEqual(sent, []);
});

test("resuming releases one queued response", () => {
  const { scheduler, sent } = harness();
  scheduler.setSuspended(true);
  scheduler.onTaskEvent(taskEvent({ event_id: 32 }));

  scheduler.setSuspended(false);

  assert.equal(sent.length, 1);
  assert.match(sent[0].response.instructions, /"event_id":32/);
});
```

Also prove repeated `setSuspended(false)` does not duplicate output and `reset()` clears suspension.

**Step 2: Run tests to verify RED**

```bash
node --test --test-name-pattern='suspension|resuming releases' \
  tests/web/task-turn-scheduler.test.mjs
```

Expected: `setSuspended` is missing.

**Step 3: Implement one explicit scheduler gate**

Add `this.suspended = false` in `reset`, a setter that calls `maybeCreateResponse()` only on true→false, and `this.suspended` to the early-return guard in `maybeCreateResponse`.

Do not discard pending task events during suspension.

**Step 4: Run focused verification**

```bash
npm run test:web
```

**Step 5: Commit**

```bash
git add frontend/task-turn-scheduler.js tests/web/task-turn-scheduler.test.mjs
git commit -m "feat: suspend task turns during media"
```

---

### Task 6: Build bounded browser continuation helpers

**Objective:** Isolate and unit-test transcript bounding, hydration event generation, and provider-health decisions outside the large voice controller script.

**Files:**
- Create: `frontend/media-continuity.js`
- Create: `tests/web/media-continuity.test.mjs`
- Modify: `package.json` only if the current test glob does not discover the new test (it should already discover `tests/web/*.test.mjs`).

**Step 1: Write failing helper tests**

Specify this API:

```javascript
import {
  ContinuationHistory,
  MediaContinuityState,
  buildHydrationEvents,
  isProviderPeerHealthy,
} from "../../frontend/media-continuity.js";
```

Tests must prove:

- `complete(role, itemId, text)` stores only completed nonblank user/assistant text;
- updating the same item ID replaces rather than duplicates it;
- snapshots retain at most 24 items and 16,000 characters, dropping oldest first;
- control characters and unsupported roles are rejected;
- returned snapshots are copies;
- media state accepts only `idle -> prepared -> playing -> resuming -> idle` transitions;
- stale confirmation, playback, and completion IDs cannot advance the state;
- provider failure during `playing` marks the peer unavailable without making media terminal;
- exact resume is selected for a healthy peer and reconstruction for an unavailable peer;
- reconstruction can be claimed once only;
- hydration uses ordered `conversation.item.create` events with `input_text` for user and `output_text` for assistant;
- hydration events contain no call IDs, tool payloads, local paths, or URLs added by the helper;
- health requires `pc.connectionState === "connected"` and `dc.readyState === "open"`.

Example:

```javascript
test("history drops oldest items to satisfy both bounds", () => {
  const history = new ContinuationHistory({ maxItems: 2, maxCharacters: 12 });
  history.complete("user", "u1", "first");
  history.complete("assistant", "a1", "second");
  history.complete("user", "u2", "third");

  assert.deepEqual(history.snapshot(), [
    { role: "assistant", text: "second" },
    { role: "user", text: "third" },
  ]);
});
```

**Step 2: Run tests to verify RED**

```bash
node --test tests/web/media-continuity.test.mjs
```

Expected: module not found.

**Step 3: Implement the minimal pure module**

No DOM, WebRTC, fetch, timers, or global state belongs in this module. Keep constants exported for the 24-item/16,000-character contract. Normalize whitespace and reject control characters.

`MediaContinuityState` owns only deterministic IDs, phases, provider availability, and the
single-reconstruction claim. It returns decisions to `voice.js`; it never sends events or mutates
tracks itself. This keeps real browser effects explicit while making stale-generation and retry
behavior executable under Node tests.

`buildHydrationEvents` must return plain JSON-ready objects only; it must not send them.

**Step 4: Run focused verification**

```bash
node --test tests/web/media-continuity.test.mjs
npm run test:web
node --check frontend/media-continuity.js
```

**Step 5: Commit**

```bash
git add frontend/media-continuity.js tests/web/media-continuity.test.mjs
git commit -m "feat: add bounded media continuation history"
```

---

### Task 7: Implement exact-peer media handoff and resumption in the voice page

**Objective:** Mute rather than tear down the healthy Realtime session, start media after matching spoken-audio completion, and restore the exact peer with one contextual follow-up.

**Files:**
- Modify: `frontend/voice.js:24-1026`
- Modify: `tests/test_ui_contract.py:106-676`
- Modify: `src/realtime_action_spike/web/voice.js` through `npm run build:web`; never hand-edit the bundle.

**Step 1: Write failing browser contract tests**

Add focused source-contract tests proving:

- successful media output branches on `output.result?.media_handoff`, not `end_session`;
- the existing `end_session` farewell/terminal path remains intact for `voice_end_session`;
- media handoff waits for the matching `output_audio_buffer.stopped` response ID;
- handoff order is microphone disable → VAD null → input buffer clear → `media_ready`;
- `taskTurnScheduler.setSuspended(true)` precedes `media_ready`;
- `media_finished` exact-peer handling restores VAD/microphone and creates one contextual follow-up;
- `media_resume_complete` is sent with `mode: "exact"`;
- `media_finished` is ignored once terminal stopping has begun, even if the control relay already
  pulled it from the controller queue;
- no `stopConversation({ reason: "model_request" })` exists in the media handoff block;
- a bounded confirmation timeout sends `media_cancel` and restores voice rather than tearing down;
- media state is generation-bound by the stored playback ID;
- `hasLiveMedia()` includes prepared/playing/resuming media so control-socket failure remains visible and deterministic.

Where source assertions would be brittle, move the state decision into `media-continuity.js` and test behavior with Node instead.

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/test_ui_contract.py -k 'media or end_session'
npm run test:web
```

Expected: the current browser still treats media as `end_session` and closes after the confirmation.

**Step 3: Implement browser media state**

Import `ContinuationHistory`, `MediaContinuityState`, `buildHydrationEvents`, and
`isProviderPeerHealthy`. Create one `MediaContinuityState` instance. Keep DOM/WebRTC side effects in
`voice.js`, but route every phase/ID/reconnect decision through the tested state object rather than
duplicating mutable booleans in the page.

Feed completed user and assistant transcript events into `ContinuationHistory`. On successful media output, store the playback ID and arm a media confirmation latch. Keep the existing `end_session` latch separate.

When the matching output buffer stops:

```javascript
for (const track of localStream?.getAudioTracks() || []) {
  track.enabled = false;
}
taskTurnScheduler.setSuspended(true);
sendRealtimeEvent({
  type: "session.update",
  session: { type: "realtime", audio: { input: { turn_detection: null } } },
});
sendRealtimeEvent({ type: "input_audio_buffer.clear" });
sendControlMessage({
  type: "media_ready",
  playback_id: mediaSession.playbackId,
  history: continuationHistory.snapshot(),
});
```

On matching `media_finished` with a healthy peer:

1. clear the provider input buffer;
2. restore the normal semantic-VAD session configuration;
3. enable audio tracks;
4. create one Response with narrow follow-up instructions and the bounded completion outcome;
5. send `media_resume_complete` with `mode="exact"`;
6. keep task turns suspended until that follow-up response finishes, then release queued task events.

Use one bounded timer for confirmation playback failure. It aborts only the prepared generation through `media_cancel`.

**Step 4: Build and run focused verification**

```bash
npm run check:web
npm run test:web
uv run pytest -q tests/test_ui_contract.py tests/test_gateway.py -k 'media or end_session or voice_route'
```

**Step 5: Commit**

```bash
git add frontend/voice.js src/realtime_action_spike/web/voice.js tests/test_ui_contract.py
git commit -m "feat: resume exact voice peer after media"
```

---

### Task 8: Reconstruct one failed or expired provider peer

**Objective:** Reopen Realtime within the same local controller session, rotate the execution scope, hydrate bounded context, and resume exactly once.

**Files:**
- Modify: `frontend/voice.js`
- Modify: `frontend/media-continuity.js` only if a pure helper is needed
- Modify: `src/realtime_action_spike/gateway.py:244-536`
- Modify: `tests/test_gateway.py`
- Modify: `tests/test_ui_contract.py`
- Modify: `tests/web/media-continuity.test.mjs`
- Rebuild: `src/realtime_action_spike/web/voice.js`

**Step 1: Write failing gateway rotation tests**

Use two successful `/session?local_session_id=...` calls for the same still-active local session. Assert:

- the second call receives a different execution scope;
- the old scope immediately receives `409 stale_session` from `/execute`;
- the new scope executes normally;
- the call-ID replay cache is cleared on scope rotation;
- a superseded concurrent SDP attempt cannot become active;
- a no-longer-active local session remains rejected.

The current gateway already contains most rotation behavior; these tests lock it as a reconstruction contract before browser work depends on it.

**Step 2: Write failing browser reconstruction tests**

Add contract/helper tests proving:

- data-channel/peer failure during `playing` marks `providerUnavailable` and does not call terminal `failConversation`;
- local controller/control-socket failure still uses terminal teardown;
- `media_finished` with an unhealthy peer makes one replacement attempt only;
- replacement uses the same `local_session_id` and receives a fresh execution scope;
- hydration events are sent after replacement `session.updated` and before microphone enable;
- hydration is followed by normal VAD, microphone enable, one contextual follow-up, and `media_resume_complete(mode="reconstructed")`;
- a failed replacement calls terminal teardown once and does not retry.

**Step 3: Run tests to verify RED**

```bash
uv run pytest -q tests/test_gateway.py tests/test_ui_contract.py -k 'replacement or rotation or reconstruct or media'
node --test tests/web/media-continuity.test.mjs
```

Expected: gateway tests may partly pass, but browser reconstruction behavior is absent.

**Step 4: Refactor provider transport creation without resetting the local session**

Extract the provider-only portion of `startConversation` into one function conceptually shaped as:

```javascript
async function openRealtimePeer({ replacement = false } = {}) {
  // Reuse the existing disabled localStream for replacement.
  // Create a fresh RTCPeerConnection and data channel.
  // POST SDP against the same local_session_id.
  // Replace executionScope from the response header.
  // Resolve only after session.updated for this exact peer.
}
```

Initial activation may reset transcript/UI state; replacement must not. A replacement must not resend `page_started` or create a new controller activation.

During media playback, provider transport close/failure marks the peer unavailable and closes only provider resources. It leaves the local control socket, browser process, continuation history, and player untouched.

After replacement readiness:

1. send bounded hydration events in order;
2. clear input audio;
3. restore normal semantic VAD;
4. enable the microphone;
5. request one contextual follow-up;
6. acknowledge reconstructed resume to the controller.

One boolean `reconnectAttempted` enforces the single attempt.

**Step 5: Verify the provider hydration schema live before relying on it**

Run a bounded standalone Realtime WebSocket probe using the configured model. Create a new session, send one user `input_text` item and one assistant `output_text` item with `conversation.item.create`, then request a text response asking for the prior topic. Do not print credentials or raw SDP.

Expected: both items are accepted and the response reflects the hydrated topic. If the provider rejects assistant `output_text`, adjust only `buildHydrationEvents` to the documented accepted item shape and lock that shape in tests.

**Step 6: Run focused verification**

```bash
npm run check:web
npm run test:web
uv run ruff check src/realtime_action_spike/gateway.py tests/test_gateway.py tests/test_ui_contract.py
uv run pytest -q tests/test_gateway.py tests/test_ui_contract.py tests/runtime/test_controller.py
```

**Step 7: Commit**

```bash
git add frontend/voice.js frontend/media-continuity.js \
  src/realtime_action_spike/web/voice.js src/realtime_action_spike/gateway.py \
  tests/test_gateway.py tests/test_ui_contract.py tests/web/media-continuity.test.mjs
git commit -m "feat: reconstruct voice context after media"
```

---

### Task 9: Update prompts, diagnostics, and user documentation

**Objective:** Align the model-facing handoff language and bounded traces with resumable playback without changing unrelated tools.

**Files:**
- Modify: `src/realtime_action_spike/config.py:15-59`
- Modify: `src/realtime_action_spike/runtime/protocol.py` only for new bounded timing names
- Modify: `src/realtime_action_spike/runtime/controller.py` only for trace emission
- Modify: `README.md`
- Modify: `docs/runbooks/stage-1-install-and-smoke.md`
- Modify: `tests/test_config.py`
- Modify: `tests/runtime/test_protocol.py`
- Modify: `tests/runtime/test_controller.py`

**Step 1: Write failing contract tests**

Assert the instructions say:

- after prepared media, give one short title confirmation;
- do not claim playback started before local handoff;
- the local runtime pauses/resumes the voice session automatically;
- do not ask the user to dictate a URL.

Add bounded timing names:

```text
media_preview_prepared
media_handoff_ready
media_playback_started
media_playback_finished
media_resume_exact
media_resume_reconstructed
media_resume_failed
```

Timing payloads may contain only exact local playback ID and bounded outcome/mode fields. They must reject transcripts, URLs, paths, tokens, and execution scopes.

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/test_config.py tests/runtime/test_protocol.py tests/runtime/test_controller.py -k media
```

**Step 3: Implement minimal prompt/docs/trace changes**

Do not alter the tool input schema or add model-controlled player arguments. Update the README and smoke runbook to describe preview → spoken confirmation → fullscreen playback → contextual resume, plus the exact/reconstructed distinction and 60-minute provider lifetime.

**Step 4: Run focused verification**

```bash
uv run ruff check src/realtime_action_spike/config.py src/realtime_action_spike/runtime tests/test_config.py
uv run pytest -q tests/test_config.py tests/runtime/test_protocol.py tests/runtime/test_controller.py
```

**Step 5: Commit**

```bash
git add src/realtime_action_spike/config.py src/realtime_action_spike/runtime/protocol.py \
  src/realtime_action_spike/runtime/controller.py README.md \
  docs/runbooks/stage-1-install-and-smoke.md tests/test_config.py \
  tests/runtime/test_protocol.py tests/runtime/test_controller.py
git commit -m "docs: describe resumable fullscreen playback"
```

---

### Task 10: Run full gates, deploy, and exercise real playback

**Objective:** Prove repository integrity and the real user flow through installed controller, Realtime text input, actual `mpv`, service shutdown, and logs.

**Files:**
- Modify only if verification exposes a defect.

**Step 1: Run repository gates from a clean environment**

The installed service config exports `MPV_BIN` and `YT_DLP_BIN`; unset them before installer isolation tests so host paths cannot leak into fixtures.

```bash
unset MPV_BIN YT_DLP_BIN YOUTUBE_BROWSER_COOKIES
uv run ruff check .
uv run python -m compileall -q src scripts
uv run pytest -q
npm run test:web
npm run check:web
bash -n scripts/install_user_services.sh
bash scripts/build-package.sh
systemd-analyze --user verify \
  systemd/okay-hermes-realtime-controller.service \
  systemd/okay-hermes-realtime-wakeword.service
git diff --check
```

Expected: every command exits `0`; no Python or Node test failures.

**Step 2: Build and install the verified package**

```bash
uv pip install \
  --python ~/.local/share/okay-hermes-realtime/venv/bin/python \
  --reinstall \
  dist/openai_realtime_action_spike-0.1.0-py3-none-any.whl
systemctl --user restart okay-hermes-realtime-controller.service
systemctl --user restart okay-hermes-realtime-wakeword.service
```

Verify controller, wakeword, and tray status plus `/health`. Confirm the controller environment resolves absolute `MPV_BIN` and `YT_DLP_BIN` paths.

**Step 3: Exercise the actual Realtime text-input contract**

Use the configured Realtime model and the same `conversation.item.create(input_text)` path used in the prior playback verification. Submit:

```text
Play Me at the zoo on YouTube.
```

Verify the model calls exactly one `media_play_youtube` with bounded source text and that the installed controller prepares one player generation. Do not bypass the production resolver or manager.

**Step 4: Verify preview and handoff ordering on the real player**

Before confirmation playback finishes, query the private mpv IPC endpoint and verify:

```text
pause=true
fullscreen=false
```

After the matching confirmation audio stops, verify:

```text
fullscreen=true
pause=false
```

Confirm one fullscreen mpv process and no second player. Inspect the session trace for:

```text
media_preview_prepared
media_handoff_ready
media_playback_started
```

in that order.

**Step 5: Verify manual close and exact resume**

Close the player through normal mpv IPC `quit` (equivalent to `q`/window close lifecycle). Verify:

- player and IPC socket are reaped;
- controller remains on the same local session ID;
- browser restores microphone/VAD;
- one contextual follow-up is produced;
- trace contains `media_playback_finished` then `media_resume_exact`;
- no terminal teardown occurred during handoff.

**Step 6: Verify natural EOF**

Run a bounded short video to natural completion and verify the same resume behavior. If the selected public video duration is unsuitable, use a known short allowlisted YouTube URL; do not weaken production URL validation.

**Step 7: Verify reconstructed continuation deterministically**

Use the browser lifecycle test harness to close only the provider peer while media is playing, leaving the local controller socket and page alive. Verify one replacement `/session` request, execution-scope rotation, bounded hydration, one contextual follow-up, and `media_resume_reconstructed`.

Do not add a production debug endpoint or global test hook solely for this test. If direct live peer termination cannot be isolated from the browser without such a hook, rely on the deterministic browser/gateway integration test plus the live hydration-schema probe, and state that limitation honestly.

**Step 8: Verify tray/service shutdown suppression**

Start another playback, then invoke the real tray Exit D-Bus menu action or stop the controller service. Verify:

- `mpv`, controller, wakeword, and tray ownership converge as designed;
- no `media_finished` follow-up is produced after shutdown starts;
- process counts reach zero for owned player/browser/controller/listener paths;
- restarting the tray/controller/wakeword returns health to ready/healthy/ok.

**Step 9: Inspect logs and traces**

```bash
journalctl --user \
  -u okay-hermes-realtime-controller.service \
  -u okay-hermes-realtime-wakeword.service \
  -u okay-hermes-realtime-tray.service \
  --since '-20 minutes' --no-pager
```

Check for provider errors, stale-session violations, retry loops, orphan player processes, raw transcript leakage, and teardown ordering failures. Confirm `/health` contains only bounded media status.

**Step 10: Final diff and review gate**

```bash
git status --short --branch
git diff --check
git log --oneline --decorate -15
```

Request a read-only final review covering spec compliance, controller/media lock ordering, stale generations, browser exact/reconstructed continuity, task-event suppression, teardown authority, and live evidence. Fix Critical/Important findings with RED/GREEN regression tests before completion.

Commit only verification-driven corrections, one coherent commit per defect.

---

## Final acceptance checklist

- [ ] Regular paused preview appears before fullscreen.
- [ ] Fullscreen is accepted before unpause.
- [ ] Matching spoken audio completion gates handoff.
- [ ] Microphone, VAD, and buffered input are disabled/cleared before playback.
- [ ] No Realtime Responses occur while media is playing.
- [ ] Exact provider peer resumes normally.
- [ ] One bounded reconstructed peer resumes after provider loss.
- [ ] Hermes asks one contextual post-video follow-up.
- [ ] EOF, manual close, abnormal exit, and stale-generation paths are covered.
- [ ] Browser/control loss and service/tray shutdown remain terminal and suppress resume.
- [ ] No permanent conversation history or sensitive trace payload is added.
- [ ] Full Python, web, packaging, systemd, and live-runtime gates pass.
- [ ] Installed services are healthy and the worktree is clean.
