# WebRTC Wakeword Replacement — Stage 1 Implementation Plan

> **For Hermes:** Use `subagent-driven-development` to implement this plan task-by-task. Every implementation task follows test-first red → green → refactor, then receives spec-compliance and code-quality review before its commit.

**Goal:** Turn the committed OpenAI Realtime spike into a separately installed Linux replacement candidate whose native tray and local “Okay Hermes” listener open a visible Brave WebRTC voice page, measure interruption timing, close cleanly, and rearm wake detection.

**Architecture:** Keep browser WebRTC responsible for microphone capture, browser acoustic processing, and model-audio playback. Move authoritative OpenAI events, tool calls, session policy, launch/close ownership, and timing persistence into the local FastAPI controller through OpenAI’s `call_id` sideband WebSocket. Adapt—not import—the proven OHV native PipeWire/ONNX listener and Qt tray under collision-safe names and paths.

**Tech stack:** Python 3.12, FastAPI/Uvicorn, HTTPX, `websockets`, Pydantic, plain HTML/CSS/JavaScript, browser WebRTC, Brave Origin Nightly app mode, C11 PipeWire + ONNX Runtime C API, C++17 Qt6 Widgets/DBus/Network + KF6PulseAudioQt, user systemd.

**Accepted design:** `docs/design/2026-07-21-webrtc-wakeword-replacement.md`

**Starting checkpoint:** `d9269bb` on preserved branch `feature/gpt-realtime-prototype`; implementation branch `feature/webrtc-wakeword-replacement`.

---

## Scope and execution gates

Stage 1 proves the wake/session/interruption lifecycle. It includes only the real capabilities needed to exercise that lifecycle:

- `assistant_get_current_time`;
- `voice_end_session`.

Timers, media control, and Hermes foreground/background task execution remain in the typed catalog only in diagnostic mode or are removed from the wake-session catalog until a separate Stage 2 plan replaces their simulations. Stage 1 must not imply that simulated side effects are production-ready.

Do not modify or install over `/home/user/Documents/SideProjects/Public/okay-hermes-voice`, `hermes-wakeword.service`, `okay-hermes-wakeword-tray`, or `~/.hermes/wakeword/config.yaml`.

Do not run the old and replacement wake listeners simultaneously during live tests. Stopping the old unit for a manual smoke is a reversible test action; never disable, overwrite, or uninstall it automatically.

### Baseline verification

Run before implementation and after every task touching shared runtime code:

```bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
```

Current expected baseline:

```text
44 passed, 1 known StarletteDeprecationWarning
All checks passed!
```

### Official contracts to re-check before OpenAI-facing edits

- WebRTC: https://developers.openai.com/api/docs/guides/realtime-webrtc
- Server-side controls: https://developers.openai.com/api/docs/guides/realtime-server-controls
- Realtime conversations/function calling: https://developers.openai.com/api/docs/guides/realtime-conversations
- Interruption/truncation: https://developers.openai.com/api/docs/guides/realtime-conversations#interruption-and-truncation

The implementation must stop if the live documentation differs from assumptions in this plan.

---

## Phase 0 — Freeze the accepted boundary and add observability first

### Task 1: Commit the accepted architecture and plan checkpoint

**Objective:** Preserve the approved design and execution plan on the replacement branch before runtime changes.

**Files:**

- Add: `docs/design/2026-07-21-webrtc-wakeword-replacement.md`
- Add: `docs/plans/2026-07-21-webrtc-wakeword-replacement-stage-1.md`

**Step 1: Verify the branch and source checkpoint**

Run:

```bash
git branch --show-current
git merge-base --is-ancestor d9269bb HEAD
git diff --check
```

Expected:

```text
feature/webrtc-wakeword-replacement
```

Both remaining commands exit 0.

**Step 2: Verify the baseline**

Run the three baseline commands above. Expected: 44 tests pass; Ruff and compileall pass.

**Step 3: Commit only the two documents**

```bash
git add docs/design/2026-07-21-webrtc-wakeword-replacement.md \
        docs/plans/2026-07-21-webrtc-wakeword-replacement-stage-1.md
git commit -m "docs: plan WebRTC wakeword replacement"
```

**Step 4: Prepare isolated execution after plan approval**

Use `using-git-worktrees` before Task 2. If the branch remains checked out in this source directory, switch the source checkout back to `feature/gpt-realtime-prototype`, then create a worktree for the existing replacement branch in the repository’s approved worktree location. Do not create a third full checkout.

---

### Task 2: Add explicit session state and timing records

**Objective:** Establish a provider-aware, testable event timeline before changing transport or UI behavior.

**Files:**

- Create: `src/realtime_action_spike/runtime/__init__.py`
- Create: `src/realtime_action_spike/runtime/session_state.py`
- Create: `src/realtime_action_spike/runtime/timing.py`
- Create: `tests/runtime/test_session_state.py`
- Create: `tests/runtime/test_timing.py`

**Required model:**

```python
class SessionPhase(StrEnum):
    IDLE = "idle"
    LAUNCHING = "launching"
    CONNECTING = "connecting"
    LIVE = "live"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class TimingEvent:
    name: str
    monotonic_ns: int
    wall_time: str
    source: Literal["wake", "controller", "browser", "openai"]
    data: dict[str, JsonValue]

@dataclass(slots=True)
class SessionTrace:
    session_id: str
    events: list[TimingEvent]
```

`SessionTrace.record()` must accept an injected monotonic/wall clock for deterministic tests, reject timestamps that move backwards, and serialize one JSON object per line without secret material.

**Step 1: Write failing tests**

Cover:

- legal state order: idle → launching → connecting → live → stopping → closed;
- illegal resurrection from closed/failed;
- repeated stop is idempotent;
- timing events preserve order and source;
- backward monotonic timestamps fail loudly;
- JSONL output excludes API keys and SDP.

**Step 2: Verify red**

```bash
uv run pytest tests/runtime/test_session_state.py tests/runtime/test_timing.py -q
```

Expected: import failures because the modules do not exist.

**Step 3: Implement the minimum state reducer and trace writer**

Keep transitions explicit in one mapping. Do not introduce a generic workflow framework.

**Step 4: Verify green**

Run the focused tests, then the full baseline suite.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/runtime tests/runtime
git commit -m "feat: add voice session state and timing trace"
```

---

## Phase 1 — Replace Streamlit with a controller-owned visible page

### Task 3: Serve the voice page directly from FastAPI

**Objective:** Remove Streamlit from the runtime path while preserving the proven WebRTC panel behavior.

**Files:**

- Create: `src/realtime_action_spike/web/index.html`
- Create: `src/realtime_action_spike/web/voice.css`
- Create: `src/realtime_action_spike/web/voice.js`
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_ui_contract.py`
- Modify: `tests/test_launcher.py`
- Delete after green tests: `src/realtime_action_spike/streamlit_app.py`
- Delete after green tests: `src/realtime_action_spike/web/realtime_panel.html`

**Required HTTP surface:**

```text
GET /voice             -> index.html
GET /assets/voice.css  -> text/css
GET /assets/voice.js   -> JavaScript
GET /health            -> existing JSON health contract
```

The page must be same-origin with the gateway. Remove Streamlit-origin CORS; do not replace it with `*`.

**Step 1: Rewrite failing contract tests**

Assert:

- `/voice` returns the visible page;
- assets are loopback-served with correct content types;
- the page contains status, transcript, actions, Hermes-task placeholder, Stop, and collapsed diagnostics;
- no Streamlit iframe or Streamlit import remains;
- permanent OpenAI credentials do not appear in page assets;
- existing `getUserMedia`, `RTCPeerConnection`, cleanup, stale-session, and transcript contracts remain represented in `voice.js`.

**Step 2: Verify red**

```bash
uv run pytest tests/test_ui_contract.py tests/test_launcher.py -q
```

**Step 3: Split the current panel without redesigning it**

Move markup, styles, and script into the three files. Preserve current behavior and IDs first. Let visual polish wait until lifecycle tests pass.

**Step 4: Remove Streamlit dependency and launcher process**

`pyproject.toml` must no longer require `streamlit`. `scripts/run.py` must start only the controller process and print the `/voice` diagnostic URL.

**Step 5: Verify green**

```bash
uv sync --dev
uv run pytest tests/test_ui_contract.py tests/test_launcher.py -q
uv run pytest -q
uv run ruff check .
```

**Step 6: Commit**

```bash
git add pyproject.toml uv.lock scripts/run.py src/realtime_action_spike tests
git commit -m "feat: serve visible WebRTC page without Streamlit"
```

---

### Task 4: Define the loopback controller protocol

**Objective:** Give the native activation handler, browser page, and controller a narrow typed protocol.

**Files:**

- Create: `src/realtime_action_spike/runtime/protocol.py`
- Create: `tests/runtime/test_protocol.py`

**Protocol messages:**

```json
{"type":"activation","probability":0.91,"detected_at":1784670000.1,"native_listener":true}
{"type":"page_ready","session_id":"local-opaque-id"}
{"type":"page_started","session_id":"local-opaque-id"}
{"type":"timing","session_id":"local-opaque-id","name":"playback_suppressed","monotonic_ms":123.4}
{"type":"stop","session_id":"local-opaque-id","reason":"button"}
{"type":"teardown_complete","session_id":"local-opaque-id"}
{"type":"session_closed","session_id":"local-opaque-id","outcome":"completed"}
```

Use strict Pydantic models with `extra="forbid"`. `session_id` is an opaque local scope, not the OpenAI `call_id` and not the browser launch token.

**Step 1: Write failing model-validation tests**

Cover missing fields, extra fields, malformed probabilities/timestamps, invalid stop reasons, and cross-session IDs.

**Step 2: Verify red, implement, verify green**

```bash
uv run pytest tests/runtime/test_protocol.py -q
```

**Step 3: Commit**

```bash
git add src/realtime_action_spike/runtime/protocol.py tests/runtime/test_protocol.py
git commit -m "feat: define local voice controller protocol"
```

---

### Task 5: Add a single-session controller and browser launch tokens

**Objective:** Own one active wake-triggered session and reject stale or replayed page connections.

**Files:**

- Create: `src/realtime_action_spike/runtime/controller.py`
- Create: `src/realtime_action_spike/runtime/tokens.py`
- Create: `tests/runtime/test_controller.py`
- Create: `tests/runtime/test_tokens.py`
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `tests/test_gateway.py`

**Required behavior:**

- One active local session at a time.
- Activation while launching/live returns `busy` without opening another page.
- Generate separate random values for local session ID and one-time browser launch token.
- Consume the launch token exactly once and bind it to the active local session.
- Reject expired, replayed, or superseded tokens.
- Never expose OpenAI credentials or the future sideband URL.

**HTTP/WebSocket surface:**

```text
GET /voice?activation=<one-time-token>
WS  /control?activation=<one-time-token>
POST /internal/open     diagnostic/tray launch request, loopback only
```

A normal browser navigation does not start a wake session unless it has a valid activation token. Diagnostic mode may show a Start button but must be visually labeled manual.

**Step 1: Write failing tests**

Test concurrent activation race, one-time token replay, wrong token, stale session stop, repeated teardown, and sanitized health output.

**Step 2: Implement with an injected launcher/clock**

The controller must not call `subprocess` directly; accept a `BrowserLauncher` protocol so state tests remain deterministic.

**Step 3: Verify**

```bash
uv run pytest tests/runtime/test_controller.py tests/runtime/test_tokens.py tests/test_gateway.py -q
uv run pytest -q
```

**Step 4: Commit**

```bash
git add src/realtime_action_spike/runtime src/realtime_action_spike/gateway.py tests
git commit -m "feat: add scoped wake session controller"
```

---

### Task 6: Launch and close only the dedicated Brave app process

**Objective:** Open a visible app-mode page on wake and close it without touching ordinary Brave windows.

**Files:**

- Create: `src/realtime_action_spike/runtime/browser.py`
- Create: `tests/runtime/test_browser.py`
- Modify: `src/realtime_action_spike/config.py`
- Modify: `.env.example`

**Configuration:**

```text
BRAVE_BIN=/usr/bin/brave-origin-nightly
VOICE_BROWSER_PROFILE=~/.local/share/okay-hermes-realtime/brave-profile
VOICE_PAGE_URL=http://127.0.0.1:8765/voice
VOICE_BROWSER_START_TIMEOUT_SECONDS=10
```

**Launch contract:**

```python
[
    brave_bin,
    f"--user-data-dir={profile_dir}",
    "--app=<loopback URL with one-time token>",
    "--no-first-run",
    "--disable-default-apps",
]
```

Start a new process session, retain the process group ID, and terminate only that group after the page confirms WebRTC teardown. Escalate TERM → timed wait → KILL. A process that already exited is a successful idempotent close.

**Step 1: Write failing tests**

Cover exact argument ownership, configured binary resolution, path expansion, no shell invocation, no normal Brave profile, timeout, early exit, idempotent close, and process-group-only kill.

**Step 2: Implement and verify**

```bash
uv run pytest tests/runtime/test_browser.py -q
```

**Step 3: Add a microphone-permission bootstrap note**

The first visible launch may prompt once for microphone permission. Do not use `--use-fake-ui-for-media-stream`. The dedicated profile must retain the user’s explicit choice.

**Step 4: Commit**

```bash
git add .env.example src/realtime_action_spike/config.py \
        src/realtime_action_spike/runtime/browser.py tests/runtime/test_browser.py
git commit -m "feat: manage dedicated Brave voice window"
```

---

### Task 7: Add the blocking native activation bridge

**Objective:** Let the C wake listener pause inference until the controller reports that the browser session ended.

**Files:**

- Create: `src/realtime_action_spike/runtime/activation_socket.py`
- Create: `src/realtime_action_spike/activation_handler.py`
- Create: `src/realtime_action_spike/service.py`
- Create: `tests/runtime/test_activation_socket.py`
- Create: `tests/test_activation_handler.py`
- Create: `tests/test_service.py`
- Modify: `src/realtime_action_spike/config.py`

**Runtime path:**

```text
$XDG_RUNTIME_DIR/okay-hermes-realtime/activation.sock
```

The short-lived handler reads exactly one JSON activation object from stdin, validates it, sends it over the Unix socket, then blocks for a terminal result:

```json
{"outcome":"completed","session_id":"local-opaque-id"}
{"outcome":"busy"}
{"outcome":"failed","error":"sanitized message"}
```

The socket server must remove stale socket files only after verifying they are sockets and no healthy controller is listening. It must use owner-only permissions.

`service.py` is the long-lived process entrypoint: create the controller, activation socket, and FastAPI app under one asyncio lifecycle; publish readiness only after both HTTP and the Unix socket are available; close both on SIGTERM. It must expose `main()` without doing work at import time.

**Step 1: Write failing tests**

Cover valid handoff, malformed stdin, missing socket, busy response, controller disconnect, signal cancellation, owner-only mode, stale socket recovery, handler waiting until session closure, service readiness ordering, and bounded service shutdown.

**Step 2: Implement and verify**

```bash
uv run pytest tests/runtime/test_activation_socket.py tests/test_activation_handler.py \
                  tests/test_service.py -q
```

**Step 3: Commit**

```bash
git add src/realtime_action_spike/activation_handler.py src/realtime_action_spike/service.py \
        src/realtime_action_spike/runtime/activation_socket.py \
        src/realtime_action_spike/config.py tests
git commit -m "feat: bridge native wake activations to controller"
```

---

## Phase 2 — Move authoritative OpenAI control to sideband

### Task 8: Preserve the upstream OpenAI call handle

**Objective:** Parse the OpenAI `Location` header from `/v1/realtime/calls` and bind the provider call to the active local session.

**Files:**

- Create: `src/realtime_action_spike/openai/__init__.py`
- Create: `src/realtime_action_spike/openai/calls.py`
- Create: `tests/openai/test_calls.py`
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `tests/test_gateway.py`

**Required type:**

```python
@dataclass(frozen=True, slots=True)
class RealtimeCallHandle:
    call_id: str
    request_id: str | None
    sdp_answer: str
```

Parse only an official `Location` value with the expected Realtime call path. Reject a successful upstream response with a missing or malformed call ID instead of silently returning SDP without sideband authority.

Do not expose `call_id` to page JavaScript. Continue returning only the opaque local session ID.

**Step 1: Add failing contract tests**

Cover valid relative/absolute Location forms documented by OpenAI, missing Location, malformed path, request ID retention, and superseded session races.

**Step 2: Implement, verify focused and full tests**

```bash
uv run pytest tests/openai/test_calls.py tests/test_gateway.py -q
```

**Step 3: Commit**

```bash
git add src/realtime_action_spike/openai src/realtime_action_spike/gateway.py tests
git commit -m "feat: retain OpenAI realtime call handles"
```

---

### Task 9: Join the OpenAI sideband WebSocket

**Objective:** Let the controller receive authoritative events and send session/tool events without routing business logic through browser JavaScript.

**Files:**

- Create: `src/realtime_action_spike/openai/sideband.py`
- Create: `tests/openai/test_sideband.py`
- Modify: `pyproject.toml`
- Modify: `src/realtime_action_spike/runtime/controller.py`

**Connection contract:**

```text
wss://api.openai.com/v1/realtime?call_id=<URL-encoded call_id>
Authorization: Bearer <server-side API key>
```

Use the current official header and URL contract at implementation time. Inject the WebSocket connector in tests. The sideband client owns a reader task, serialized sends, explicit close, and terminal failure notification to the session controller.

**Step 1: Write failing tests**

Cover URL encoding, secret-only server ownership, event JSON decoding, malformed event handling, one writer lock, clean close, remote close, cancellation, and stale sideband events after local-session replacement.

**Step 2: Add `websockets` and implement**

Do not add reconnect loops inside a live call. A lost authoritative sideband is a session failure and triggers bounded teardown.

**Step 3: Verify**

```bash
uv sync --dev
uv run pytest tests/openai/test_sideband.py tests/runtime/test_controller.py -q
uv run pytest -q
```

**Step 4: Commit**

```bash
git add pyproject.toml uv.lock src/realtime_action_spike/openai/sideband.py \
        src/realtime_action_spike/runtime/controller.py tests
git commit -m "feat: control realtime calls through sideband"
```

---

### Task 10: Move function calls from browser execution to sideband execution

**Objective:** Keep the browser as media/UI while the controller validates and continues OpenAI function calls.

**Files:**

- Create: `src/realtime_action_spike/openai/events.py`
- Create: `src/realtime_action_spike/openai/tool_loop.py`
- Create: `tests/openai/test_events.py`
- Create: `tests/openai/test_tool_loop.py`
- Modify: `src/realtime_action_spike/capabilities.py`
- Modify: `src/realtime_action_spike/web/voice.js`
- Modify: `tests/test_ui_contract.py`
- Modify: `tests/test_capabilities.py`

**Authoritative loop:**

1. Receive completed `function_call` item or `response.function_call_arguments.done` on sideband.
2. Capture `call_id`, `name`, and final JSON arguments.
3. Deduplicate by provider `call_id` plus canonical request fingerprint.
4. Execute the local broker once.
5. Send `conversation.item.create` with `item.type="function_call_output"`, same `call_id`, and JSON-string `item.output`.
6. Send `response.create` unless `voice_end_session` is entering close-after-farewell behavior.
7. Publish a sanitized action-state event to the page.

The page must no longer call `/execute`. Remove that endpoint after the sideband tests and gateway tests are green. Retain the proven idempotency logic in a controller-owned execution registry rather than deleting it.

**Stage 1 tool catalog:**

- `assistant_get_current_time`: real local result.
- `voice_end_session`: real lifecycle transition.

Do not expose simulated timer/media/Hermes tools in normal wake sessions.

**Step 1: Write failing parser and tool-loop tests**

Cover delta completion, response-done fallback, duplicate same request, duplicate conflicting request, malformed arguments, unknown capability, stale local session, exact output string shape, continuation ordering, and sanitized UI projection.

**Step 2: Implement, then remove browser executor**

Browser JavaScript may display action events but must contain no local execution fetch and no function-call continuation logic.

**Step 3: Verify**

```bash
uv run pytest tests/openai/test_events.py tests/openai/test_tool_loop.py \
                  tests/test_gateway.py tests/test_ui_contract.py \
                  tests/test_capabilities.py -q
uv run pytest -q
```

**Step 4: Commit**

```bash
git add src/realtime_action_spike/openai src/realtime_action_spike/capabilities.py \
        src/realtime_action_spike/gateway.py src/realtime_action_spike/web/voice.js tests
git commit -m "feat: execute realtime tools on trusted sideband"
```

---

## Phase 3 — Make interruption and teardown measurable

### Task 11: Add an interruption timeline reducer

**Objective:** Measure the timing defect observed in the prior demo without pretending transcript events equal audible playback.

**Files:**

- Create: `src/realtime_action_spike/openai/interruption.py`
- Create: `tests/openai/test_interruption.py`
- Modify: `src/realtime_action_spike/runtime/timing.py`
- Modify: `src/realtime_action_spike/runtime/controller.py`

**Per-interruption fields:**

```python
@dataclass(slots=True)
class InterruptionTrace:
    response_id: str | None
    user_speech_onset_ms: float | None
    speech_started_received_ns: int | None
    provider_audio_start_ms: int | None
    playback_suppressed_ns: int | None
    response_cancelled_ns: int | None
    truncation_observed_ns: int | None
    listening_restored_ns: int | None
    next_response_first_audio_ns: int | None
```

The reducer consumes both OpenAI sideband events and browser timing markers. Under WebRTC VAD, record OpenAI’s automatic cancellation/truncation events; do not send manual `conversation.item.truncate` unless current official docs require it for this exact WebRTC flow.

**Step 1: Write failing reducer tests**

Cover:

- normal interruption order;
- browser suppression before sideband cancellation;
- duplicated events;
- missing optional provider truncation event;
- late event from stale response/session;
- two separate interruptions;
- computed speech-start → audible-silence duration;
- incomplete traces preserved as incomplete, not fabricated.

**Step 2: Implement and verify**

```bash
uv run pytest tests/openai/test_interruption.py tests/runtime/test_timing.py -q
```

**Step 3: Commit**

```bash
git add src/realtime_action_spike/openai/interruption.py \
        src/realtime_action_spike/runtime tests/openai/test_interruption.py tests/runtime/test_timing.py
git commit -m "feat: measure realtime interruption timing"
```

---

### Task 12: Suppress residual playback and show the timing trail

**Objective:** Make interruption response immediate in the page and visible in diagnostics.

**Files:**

- Create: `src/realtime_action_spike/web/interruption_state.mjs`
- Create: `tests/web/interruption_state.test.mjs`
- Modify: `src/realtime_action_spike/web/voice.js`
- Modify: `src/realtime_action_spike/web/index.html`
- Modify: `src/realtime_action_spike/web/voice.css`
- Modify: `tests/test_ui_contract.py`

**Browser behavior:**

- Continue receiving OpenAI lifecycle events on the WebRTC data channel for the lowest-latency local response.
- On `input_audio_buffer.speech_started`, record a browser monotonic timestamp and immediately suppress the current remote audio element/track.
- Send `playback_suppressed` to the local control WebSocket.
- Keep suppression scoped to the interrupted response; restore audio for the next response only after the corresponding lifecycle transition.
- Never tear down the microphone for an ordinary interruption.
- Display listening/restored state and the most recent timing deltas in the collapsed diagnostic drawer.

Do not infer response IDs from DOM order. Track explicit provider/local session identity.

**Step 1: Add failing static contracts**

Assert named handlers and state guards for speech started, playback suppression, response cancellation, next-response restoration, timing publication, and stale-session rejection.

**Step 2: Add a deterministic JavaScript reducer test**

Extract the pure browser interruption reducer into `src/realtime_action_spike/web/interruption_state.mjs`, load `voice.js` as a module, and test it with Node’s built-in test runner. Node is a Stage 1 development prerequisite; fail clearly if it is missing rather than silently reducing this to source-string checks.

**Step 3: Implement and verify**

```bash
uv run pytest tests/test_ui_contract.py -q
node --test tests/web/interruption_state.test.mjs
uv run pytest -q
```

**Step 4: Commit**

```bash
git add src/realtime_action_spike/web tests/test_ui_contract.py tests/web
git commit -m "feat: suppress and expose interrupted playback"
```

---

### Task 13: Make every session end path converge on one teardown

**Objective:** Guarantee Stop, close phrase, browser failure, timeout, and tray Turn Off release media and rearm wake detection exactly once.

**Files:**

- Create: `src/realtime_action_spike/runtime/teardown.py`
- Create: `tests/runtime/test_teardown.py`
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `src/realtime_action_spike/web/voice.js`
- Modify: `tests/runtime/test_controller.py`
- Modify: `tests/test_ui_contract.py`

**Teardown order:**

1. Mark local session stopping and reject new tool work.
2. Stop/suppress remote model playback.
3. Close browser data channel and `RTCPeerConnection`.
4. Stop every local microphone track.
5. Receive or time out waiting for `teardown_complete`.
6. Close sideband.
7. Close the dedicated Brave process group.
8. Persist final trace atomically.
9. Release the blocking activation handler with terminal outcome.
10. Return controller to idle so wake inference resumes.

`voice_end_session` may request one short model farewell, bounded by a small timeout, then enters the same teardown. Stop button and failures are immediate.

**Step 1: Write failing fault-injection tests**

Cover failure at every step, duplicate Stop, browser already gone, sideband already gone, missing teardown acknowledgement, close phrase during a tool call, activation client disconnect, and final idle state.

**Step 2: Implement one idempotent coordinator**

No endpoint or callback may manually reproduce teardown steps.

**Step 3: Verify**

```bash
uv run pytest tests/runtime/test_teardown.py tests/runtime/test_controller.py \
                  tests/test_ui_contract.py -q
uv run pytest -q
```

**Step 4: Commit**

```bash
git add src/realtime_action_spike/runtime src/realtime_action_spike/web/voice.js tests
git commit -m "feat: unify voice session teardown"
```

---

## Phase 4 — Add separately named native wakeword and tray components

### Task 14: Adapt the native PipeWire/ONNX wake listener

**Objective:** Add a low-idle-cost local wake listener without importing or modifying OHV at runtime.

**Reference only:**

- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/native/okay-hermes-wake-listener.c`
- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/native/build_wake_listener.sh`
- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/native/include/onnxruntime_c_api.h`
- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/tests/voice_conversation/test_native_pipewire_listener.py`

**Files:**

- Create: `native/okay-hermes-realtime-wake-listener.c`
- Create: `native/build_wake_listener.sh`
- Create: `native/include/onnxruntime_c_api.h`
- Create: `tests/native/test_pipewire_listener.py`
- Create: `THIRD_PARTY_NOTICES.md`

**Required retained properties:**

- PipeWire realtime callback performs bounded F32 downmix/resampling and ring writes only.
- ONNX inference and health file writes remain on the worker thread.
- Fixed 16 kHz mono three-second model input.
- Tiny-model ONNX arena/memory-pattern optimizations remain disabled.
- Handler is synchronous so no second wake inference occurs until the voice session ends.
- Signal cleanup and atomic health-state writing remain explicit.

**Required changes:**

- Binary and log labels use `okay-hermes-realtime-*`.
- No default `~/.hermes` config or OHV Python module.
- Model path and handler command are explicit command-line arguments.
- Capture-health path belongs to `~/.local/state/okay-hermes-realtime/` or an explicit argument.
- Default no-handler behavior prints activation JSON for direct testing.
- Preserve Apache-2.0 provenance in `THIRD_PARTY_NOTICES.md`.

**Step 1: Port architecture/build tests first**

Test callback boundaries, worker inference, health writes, CLI parsing, help output, self-test, and successful native compilation.

**Step 2: Verify red**

```bash
uv run pytest tests/native/test_pipewire_listener.py -q
```

**Step 3: Adapt the minimum source and build script**

Do not copy OHV’s activation flow, config loader, STT, popup, router, TTS, or service names.

**Step 4: Verify green**

```bash
uv run pytest tests/native/test_pipewire_listener.py -q
native/build_wake_listener.sh --output /tmp/okay-hermes-realtime-wake-listener
/tmp/okay-hermes-realtime-wake-listener --help
```

**Step 5: Commit**

```bash
git add native tests/native/test_pipewire_listener.py THIRD_PARTY_NOTICES.md
git commit -m "feat: add isolated native realtime wake listener"
```

---

### Task 15: Add collision-safe user services and installer

**Objective:** Install replacement components without changing existing OHV files or activation state.

**Files:**

- Create: `systemd/okay-hermes-realtime-controller.service`
- Create: `systemd/okay-hermes-realtime-wakeword.service`
- Create: `scripts/install_user_services.sh`
- Create: `tests/native/test_user_services.py`
- Create: `config.example.env`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Service contract:**

- Controller unit starts the FastAPI/controller process and activation socket.
- Wakeword unit uses `Requires=` and `After=` for the replacement controller plus PipeWire/WirePlumber.
- Wakeword `ExecStart` names the replacement binary, explicit model path, explicit handler command, threshold, consecutive windows, and inference interval.
- Runtime/state/profile paths are replacement-specific.
- No unit contains `hermes-wakeword.service`, `okay-hermes-voice`, OHV config paths, or terminal popup commands.
- Installer copies files and runs `systemctl --user daemon-reload` but does not enable/start units unless passed an explicit `--enable` flag.
- Installer never stops or disables existing OHV.

Make the project installable before writing the units:

- add a standard Python build backend and include `src/realtime_action_spike/web/**` as package data;
- replace `[tool.uv] package = false` with package mode;
- expose `okay-hermes-realtime-controller = realtime_action_spike.service:main` and `okay-hermes-realtime-activation = realtime_action_spike.activation_handler:main`;
- create a dedicated venv at `~/.local/share/okay-hermes-realtime/venv` and install this repository into it;
- point both systemd `ExecStart` and the listener handler command at executables in that dedicated venv, never the OHV or Hermes Agent venv.

**Step 1: Write failing service/installer contract tests**

Include explicit collision scans, wheel/package-data validation, console-entrypoint validation, and executable-path validation.

**Step 2: Implement and verify**

```bash
uv run pytest tests/native/test_user_services.py -q
uv build
python -m zipfile -l dist/*.whl
systemd-analyze --user verify \
  systemd/okay-hermes-realtime-controller.service \
  systemd/okay-hermes-realtime-wakeword.service
```

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock systemd scripts/install_user_services.sh \
        tests/native/test_user_services.py config.example.env
git commit -m "feat: add isolated realtime user services"
```

---

### Task 16: Adapt the event-driven Qt tray

**Objective:** Provide native On/Off/Open Voice Page/Exit controls and honest health state.

**Reference only:**

- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/native/wakeword-tray/main.cpp`
- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/native/wakeword-tray/tray_state.h`
- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/native/wakeword-tray/CMakeLists.txt`
- `/home/user/Documents/SideProjects/Public/okay-hermes-voice/tests/voice_conversation/test_wakeword_tray.py`

**Files:**

- Create: `native/realtime-tray/main.cpp`
- Create: `native/realtime-tray/tray_state.h`
- Create: `native/realtime-tray/CMakeLists.txt`
- Create: `scripts/install_realtime_tray.sh`
- Create: `tests/native/test_realtime_tray.py`

**Tray menu:**

```text
Turn ON
Turn OFF
Open Voice Page
────────
Exit
```

**State inputs:**

- replacement controller unit state through user-systemd DBus;
- replacement wakeword unit state through user-systemd DBus;
- native capture-health marker through `QFileSystemWatcher`;
- controller/session health marker through `QFileSystemWatcher`;
- microphone availability through PulseAudioQt events.

Use Qt Network for an explicit loopback `POST /internal/open` if Open Voice Page delegates launch to the controller. Do not call a shell, use periodic `systemctl`, or open a normal browser profile.

**Step 1: Port/adapt tests first**

Test unit names/object-path escaping, state reducer, no-microphone state, event-driven watchers, asynchronous DBus calls, menu labels, controller-owned open request, build, and collision-safe installer paths.

**Step 2: Implement and verify**

```bash
uv run pytest tests/native/test_realtime_tray.py -q
cmake -S native/realtime-tray -B /tmp/okay-hermes-realtime-tray-build -G Ninja
cmake --build /tmp/okay-hermes-realtime-tray-build
```

**Step 3: Commit**

```bash
git add native/realtime-tray scripts/install_realtime_tray.sh \
        tests/native/test_realtime_tray.py THIRD_PARTY_NOTICES.md
git commit -m "feat: add native realtime wakeword tray"
```

---

## Phase 5 — Prove the complete slice on the real machine

### Task 17: Add deterministic lifecycle integration tests

**Objective:** Prove wake → page → WebRTC-control setup → interruption → teardown → rearm without relying only on source-string tests.

**Files:**

- Create: `tests/integration/test_wake_session_lifecycle.py`
- Create: `tests/integration/test_session_failures.py`
- Create: `tests/fakes/fake_browser.py`
- Create: `tests/fakes/fake_sideband.py`
- Create: `tests/fakes/fake_activation_client.py`

**Scenarios:**

1. Activation launches one page and blocks the activation client.
2. Page consumes token, creates provider call, and reaches live.
3. Sideband function call returns current time exactly once.
4. Interruption events plus browser suppression produce one complete trace.
5. Stop tears down, closes browser, releases activation client, and returns idle.
6. Close phrase waits for bounded farewell then follows the same teardown.
7. Browser crash, sideband loss, and setup timeout each release activation and return to idle.
8. Second activation while live returns busy and launches nothing.
9. Stale page, sideband, tool, and timing events cannot mutate the replacement session.

**Step 1: Write failing integration tests using only injected fakes**

No network, microphone, browser, systemd, or OpenAI key in this task.

**Step 2: Implement only missing orchestration glue**

Do not add test-only branches to production state machines.

**Step 3: Verify**

```bash
uv run pytest tests/integration -q
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
```

**Step 4: Commit**

```bash
git add tests/integration tests/fakes src
git commit -m "test: prove wake session lifecycle"
```

---

### Task 18: Add runtime resource and latency measurement

**Objective:** Produce empirical RSS/PSS/CPU and lifecycle timing evidence for the replacement processes.

**Files:**

- Create: `scripts/measure_runtime.py`
- Create: `tests/test_measure_runtime.py`
- Create at smoke time, do not commit secrets/audio: `artifacts/stage-1-smoke/<timestamp>/summary.json`

**Measurement contract:**

Read Linux `/proc/<pid>/smaps_rollup` and `/proc/<pid>/stat` for explicitly supplied/tracked PIDs. Report separately:

- controller RSS/PSS/CPU;
- wake listener RSS/PSS/CPU;
- tray RSS/PSS/CPU;
- dedicated Brave process-tree RSS/PSS/CPU;
- combined replacement total.

Report lifecycle deltas from persisted traces:

- wake detected → page process started;
- page started → WebRTC live;
- speech stopped → first model audio/lifecycle marker;
- user interruption → local playback suppressed;
- Stop/close → microphone tracks stopped;
- teardown start → wake rearmed.

**Step 1: Test `/proc` parsing with fixtures**

Cover vanished processes, permission errors, child aggregation, and no silent zero substitution.

**Step 2: Implement and verify**

```bash
uv run pytest tests/test_measure_runtime.py -q
```

**Step 3: Commit**

```bash
git add scripts/measure_runtime.py tests/test_measure_runtime.py
git commit -m "feat: measure voice runtime resources and latency"
```

---

### Task 19: Run the real wakeword/WebRTC acceptance smoke

**Objective:** Exercise the artifact, not merely its mocks.

**Prerequisites:**

- Valid server-side `OPENAI_API_KEY` in the replacement environment.
- Existing wakeword ONNX artifact available read-only.
- Brave Origin Nightly at the configured path.
- Replacement native components built.
- One-time microphone permission granted to the dedicated profile.
- Existing OHV wake listener stopped only for the duration of this smoke.

**Step 1: Verify old OHV remains installed and record its initial state**

```bash
systemctl --user is-active hermes-wakeword.service || true
systemctl --user is-enabled hermes-wakeword.service || true
```

Save the state for restoration. Do not edit its unit or config.

**Step 2: Run the replacement without auto-enabling installation**

Use the replacement units or foreground controller/listener/tray build from the worktree. Verify `/health`, activation socket readiness, and tray state before speaking.

**Step 3: Complete the live matrix**

- Say “Okay Hermes”; page opens without Start.
- Complete a normal spoken turn.
- Ask for current time; observe one sideband tool execution and natural spoken result.
- Interrupt model speech twice: once early, once near sentence end.
- Inspect audible-stop timing and conversation continuity.
- Say an explicit close phrase; page closes and wake listener rearms.
- Wake again; press Stop; page closes and listener rearms.
- Force-close the dedicated page during a session; listener rearms with a visible error trace.
- Confirm no terminal popup and no Streamlit process.

**Step 4: Capture evidence**

Save sanitized event/timing JSONL, resource summary, controller/native logs, and manual observations under ignored `artifacts/stage-1-smoke/`. Do not save API keys, raw SDP, permanent browser profile data, or microphone audio.

**Step 5: Restore prior OHV state**

Stop replacement components. Restore the old unit to exactly the active/enabled state recorded in Step 1. Verify its source/config hashes were unchanged.

**Step 6: Acceptance decision**

Stage 1 passes only if:

- wake opens the visible page consistently;
- interruption-to-silence is measured and subjectively aligned with audible speech;
- close/Stop/failure all rearm wake detection;
- only the dedicated Brave process is closed;
- no stale tool result reaches a later session;
- replacement resource totals are recorded;
- existing OHV is restored unchanged.

If interruption timing fails, preserve the trace and debug the exact stage—VAD onset, sideband event delivery, browser suppression, or WebRTC buffered playback—before changing transport.

---

### Task 20: Final documentation and verification gate

**Objective:** Make the Stage 1 candidate reproducible and clearly separate from the old spike and OHV.

**Files:**

- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/design/2026-07-21-webrtc-wakeword-replacement.md` only for verified implementation deltas
- Create: `docs/runbooks/stage-1-install-and-smoke.md`

**Documentation must include:**

- architecture/process ownership;
- first-run microphone permission;
- run/install commands;
- separate unit/binary/config/state/profile names;
- how to toggle old versus replacement listener safely;
- interruption diagnostic fields;
- cleanup/uninstall limited to replacement files;
- known limitations and explicit Stage 2 deferrals;
- measured smoke results without overstating them.

**Step 1: Run final automated verification**

```bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
node --test tests/web/interruption_state.test.mjs
uv build
cmake -S native/realtime-tray -B /tmp/okay-hermes-realtime-tray-final -G Ninja
cmake --build /tmp/okay-hermes-realtime-tray-final
native/build_wake_listener.sh --output /tmp/okay-hermes-realtime-wake-listener-final
systemd-analyze --user verify \
  systemd/okay-hermes-realtime-controller.service \
  systemd/okay-hermes-realtime-wakeword.service
git diff --check
git status --short
```

**Step 2: Verify replacement isolation**

Search tracked files for forbidden installed-name collisions and confirm no changes exist in the OHV repository.

**Step 3: Request code review**

Use `requesting-code-review` against the full branch diff from `d9269bb`, then address findings through TDD.

**Step 4: Commit documentation**

```bash
git add README.md .env.example docs
git commit -m "docs: document WebRTC wakeword replacement"
```

**Step 5: Test the local-main merge before any push**

Create a temporary integration worktree/branch, merge the replacement branch into local `main`, run the complete final verification there, and remove the temporary worktree only after success. Do not push until this local-main merge gate passes.

---

## Explicit Stage 2 backlog — not part of this plan

After Stage 1 produces stable traces and resource measurements, write a separate reviewed plan for:

- real timers and cancellation;
- real media execution;
- Hermes foreground and durable background task IDs;
- pending-result persistence and next-wake reinjection;
- task cancellation/status UI;
- session archives beyond timing traces;
- comparison of hidden/persistent WebRTC worker versus open-on-wake latency;
- native WebSocket audio only if WebRTC cannot meet measured interruption or lifecycle requirements.

Do not prebuild provider abstractions for this backlog.
