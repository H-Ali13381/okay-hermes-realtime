# Direct WebRTC Lifecycle Simplification Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Restore the stable browser-owned direct WebRTC/data-channel path while preserving the native shell and making local tool/control failures non-fatal to healthy media.

**Architecture:** The browser creates one direct `RTCPeerConnection` to OpenAI through the loopback SDP endpoint and handles provider events on `oai-events`. Function calls are relayed to a session-scoped local `/execute` endpoint backed by the existing `CapabilityBroker`. Sideband, call-ID relay, and Agents SDK transport ownership are removed rather than retained behind flags.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest/pytest-asyncio, browser JavaScript, WebRTC, esbuild, systemd user services.

**Design source:** `docs/superpowers/specs/2026-07-23-direct-webrtc-lifecycle-simplification-design.md`

---

### Task 1: Add session-scoped local execution contracts

**Objective:** Restore server-authorized browser tool relay without exposing credentials or accepting stale/replayed scopes.

**Files:**
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `tests/test_gateway.py`

**Step 1: Write failing gateway tests**

Add tests proving:

- a locally bound successful `/session` response includes a bounded opaque `X-Okay-Hermes-Execution-Scope` header;
- manual diagnostic sessions receive no execution scope;
- `/execute` requires loopback origin, local-client header, active scope, `call_id`, name, and bounded arguments;
- identical duplicate `call_id` requests replay the cached response;
- changed-payload reuse returns `call_id_conflict`;
- stale/superseded scope returns `stale_session`;
- unknown tools and invalid arguments remain broker-rejected;
- the per-session call limit remains enforced.

Use the stable branch’s `ExecutionRequest`, fingerprint, cache, and error response shapes as behavioral reference, but bind execution to the current native controller session and a random scope rather than the provider session ID.

**Step 2: Verify RED**

Run:

```bash
uv run pytest -q tests/test_gateway.py -k 'execution_scope or execute'
```

Expected: FAIL because `/execute` and the execution-scope header do not exist in the native gateway.

**Step 3: Implement the minimal endpoint**

In `gateway.py`:

- define `LOCAL_EXECUTION_SCOPE_HEADER = "X-Okay-Hermes-Execution-Scope"`;
- add strict `ExecutionRequest` fields `scope`, `call_id`, `name`, and `arguments` with explicit bounds;
- add canonical request fingerprinting;
- issue a `secrets.token_urlsafe(24)` scope only after a locally bound SDP call succeeds;
- bind scope to `local_session_id` and clear scope/cache when superseded;
- require loopback plus `X-Okay-Hermes-Client: voice-page-v1` on `/execute`;
- verify the controller’s active session still matches the scope binding;
- delegate execution only to the injected `CapabilityBroker`;
- cache bounded results by `call_id`.

Do not add CORS, cookies, permanent browser credentials, generic tool routing, or shell execution.

**Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_gateway.py
uv run ruff check src/realtime_action_spike/gateway.py tests/test_gateway.py
```

Expected: gateway tests pass and Ruff reports no errors.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/gateway.py tests/test_gateway.py
git commit -m "feat: restore scoped local realtime tool execution"
```

### Task 2: Restore direct browser WebRTC and data-channel tools

**Objective:** Replace Agents SDK transport ownership with the stable direct `RTCPeerConnection`/`oai-events` flow while preserving the current native UI and diagnostics.

**Files:**
- Modify: `frontend/voice.js`
- Modify: `tests/test_ui_contract.py`
- Generated: `src/realtime_action_spike/web/voice.js`

**Step 1: Write failing browser contract tests**

Add contract assertions proving the built voice client contains:

- `new RTCPeerConnection()`;
- `createDataChannel("oai-events")`;
- direct `POST /session` with `application/sdp` and local-session binding;
- extraction of `X-Okay-Hermes-Execution-Scope`;
- scoped `POST /execute` with the local-client header;
- `conversation.item.create` with `function_call_output` followed by `response.create`;
- explicit peer/data-channel/track cleanup;
- no `OpenAIRealtimeWebRTC`, `fetchClientSecret`, `/client-secret`, or provider-call-ID relay.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_ui_contract.py
```

Expected: FAIL on direct WebRTC and local tool relay assertions.

**Step 3: Implement direct transport**

Port the stable prototype behavior into the current `frontend/voice.js` without replacing current DOM/status/transcript/control helpers:

- replace `realtimeTransport` with `peerConnection`, `dataChannel`, `localStream`, execution scope, and handled-call state;
- acquire microphone constraints as today;
- attach tracks and remote audio directly;
- post local SDP offer to `/session` with `X-Okay-Hermes-Session-ID`;
- apply the SDP answer;
- mark `page_started` only when `oai-events` opens;
- process provider transcript, VAD, response, error, and function-call events through existing bounded diagnostics;
- relay tool calls to `/execute` and return structured output over the data channel;
- on tool fetch/authorization failure, show **Tools unavailable**, send a structured tool failure when possible, and keep media open;
- route successful `voice_end_session` through existing explicit teardown after the model’s final response;
- keep idempotent Stop/X/pagehide cleanup.

Do not port Streamlit DOM, manual Start semantics, CORS, or permanent provider session IDs.

**Step 4: Verify GREEN**

```bash
npm run check:web
uv run pytest -q tests/test_ui_contract.py
```

Expected: bundle builds, JavaScript syntax passes, and UI contracts pass.

**Step 5: Commit**

```bash
git add frontend/voice.js src/realtime_action_spike/web/voice.js tests/test_ui_contract.py
git commit -m "refactor: restore direct realtime WebRTC transport"
```

### Task 3: Restore transport grace and degraded control behavior

**Objective:** Prevent transient media/control faults from immediately terminating a usable conversation while retaining bounded cleanup.

**Files:**
- Modify: `frontend/voice.js`
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `tests/test_ui_contract.py`
- Modify: `tests/test_gateway.py`
- Modify: `tests/integration/test_session_failures.py`

**Step 1: Write failing failure-policy tests**

Prove:

- peer `disconnected` starts a 3,000 ms timer;
- reconnect before expiry clears the timer;
- sustained disconnect, peer `failed`, and unexpected data-channel close remain fatal;
- `/execute` failure changes visible state to **Tools unavailable** without `stopConversation`;
- control WebSocket loss before media readiness fails startup;
- control loss after media readiness does not immediately call `request_teardown`;
- bounded control reconnection can rebind only the active local session using the active execution scope;
- stale reconnect attempts are rejected;
- unrecovered control loss still has a bounded final cleanup path and cannot orphan the browser indefinitely.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_ui_contract.py tests/test_gateway.py tests/integration/test_session_failures.py
```

Expected: FAIL on immediate disconnect/control teardown behavior.

**Step 3: Implement minimal policy**

- Restore the stable `scheduleTransportFailure`/`clearTransportFailureTimer` semantics.
- Keep fatal handling for actual peer/data-channel failure only.
- Let an active browser rebind `/control` with its current execution scope; do not make launch activation tokens reusable.
- On post-readiness control loss, schedule a bounded reconnect window instead of immediate global teardown.
- Cancel the guard on successful rebind.
- If the guard expires, perform the existing bounded teardown to prevent an orphaned session.
- Keep explicit Stop/X local cleanup even while control is degraded.

**Step 4: Verify GREEN**

```bash
npm run check:web
uv run pytest -q tests/test_ui_contract.py tests/test_gateway.py tests/integration/test_session_failures.py
```

Expected: focused failure-policy suite passes.

**Step 5: Commit**

```bash
git add frontend/voice.js src/realtime_action_spike/web/voice.js src/realtime_action_spike/gateway.py src/realtime_action_spike/runtime/controller.py src/realtime_action_spike/runtime/tokens.py tests/test_ui_contract.py tests/test_gateway.py tests/integration/test_session_failures.py
git commit -m "fix: decouple auxiliary control faults from voice media"
```

### Task 4: Remove sideband and provider call-ID lifecycle

**Objective:** Delete the duplicate OpenAI connection and all now-unreachable controller/protocol state.

**Files:**
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `src/realtime_action_spike/runtime/protocol.py`
- Modify: `tests/integration/test_wake_session_lifecycle.py`
- Modify: `tests/integration/test_session_failures.py`
- Modify: `tests/runtime/test_controller.py`
- Modify: `tests/runtime/test_protocol.py`
- Delete: `src/realtime_action_spike/openai/sideband.py`
- Delete: `src/realtime_action_spike/openai/events.py`
- Delete: `src/realtime_action_spike/openai/tool_loop.py`
- Delete: `src/realtime_action_spike/openai/calls.py` if no remaining call-handle consumer exists
- Delete: corresponding `tests/openai/test_sideband.py`, parser/tool-loop/call-handle tests, and `tests/fakes/fake_sideband.py`

**Step 1: Replace sideband-centric integration tests**

Rewrite the wake lifecycle test to prove:

- activation → page/control → direct media readiness;
- local tool execution is tested through gateway `/execute`, not controller sideband events;
- explicit model-request teardown still closes browser and rearms wake;
- a removed sideband failure cannot be a terminal outcome.

Add protocol assertions that `RealtimeConnectedMessage`, provider call-ID validation, and sideband action-state messages are absent when no longer consumed.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/integration tests/runtime tests/openai
```

Expected: FAIL while controller and protocol still require sideband behavior.

**Step 3: Delete unreachable production paths**

Remove:

- sideband imports and client/parser/tool-loop maps;
- `start_realtime_sideband`, `process_sideband_event`, reattach/failure handlers, and detach teardown steps;
- gateway call-handle parsing/registry and sideband binders;
- provider call-ID control message and browser relay;
- sideband action-state relay if the browser now owns tool status;
- all imports and tests whose only purpose was the removed connection.

Preserve controller activation serialization, browser process ownership, outbound Stop, teardown acknowledgement, terminal results, interruption traces, and wakeword rearm.

**Step 4: Verify GREEN**

```bash
uv run pytest -q tests/integration tests/runtime tests/openai tests/test_gateway.py
uv run python -m compileall -q src scripts
```

Expected: focused Python suites pass and source compiles.

**Step 5: Commit**

```bash
git add -A src tests
git commit -m "refactor: remove realtime sideband lifecycle"
```

### Task 5: Remove Agents SDK and obsolete client-secret path

**Objective:** Make the dependency graph match the single direct transport.

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `tests/test_gateway.py`
- Generated: `src/realtime_action_spike/web/voice.js`

**Step 1: Add absence tests**

Assert that:

- `/client-secret` is no longer exposed;
- no source/bundle contains `@openai/agents-realtime`, `OpenAIRealtimeWebRTC`, or `/client-secret`;
- package metadata has no `@openai/agents-realtime` or transitive `zod` dependency unless independently required.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_gateway.py tests/test_ui_contract.py
```

Expected: FAIL while the endpoint/dependencies remain.

**Step 3: Remove obsolete code and dependencies**

Delete `_post_client_secret`, client-secret constants/route/tests, and run:

```bash
npm uninstall @openai/agents-realtime zod
npm run check:web
```

**Step 4: Verify GREEN**

```bash
uv run pytest -q tests/test_gateway.py tests/test_ui_contract.py
npm run check:web
```

Expected: focused tests and web build pass.

**Step 5: Commit**

```bash
git add package.json package-lock.json frontend/voice.js src/realtime_action_spike/gateway.py src/realtime_action_spike/web/voice.js tests/test_gateway.py tests/test_ui_contract.py
git commit -m "build: remove obsolete realtime SDK dependency"
```

### Task 6: Update architecture and operational documentation

**Objective:** Remove instructions that describe sideband as an active dependency and document the one-transport lifecycle.

**Files:**
- Modify: `README.md`
- Modify: `docs/design/2026-07-21-webrtc-wakeword-replacement.md`
- Modify: `docs/runbooks/stage-1-install-and-smoke.md`
- Modify: `docs/remaining-tasks.md`
- Preserve: approved design spec and this plan as historical decision records

**Step 1: Write documentation contract checks where existing UI/runbook tests support them**

Require the active runbook to name direct WebRTC readiness, scoped `/execute`, degraded tools, and the installed smoke sequence; reject active sideband attach/reconnect instructions.

**Step 2: Verify RED**

```bash
uv run pytest -q tests/test_ui_contract.py -k 'runbook or architecture'
```

Expected: FAIL if existing contract coverage is extended; otherwise document the manual review check in the commit body.

**Step 3: Update docs**

Describe:

- one OpenAI WebRTC/data-channel transport;
- local authenticated tool relay;
- fatal versus degraded failures;
- three-second disconnect grace;
- sequential service restart/start requirement;
- smoke checks for time tool, forced tool degradation, interruption, Stop, cleanup, and wake rearm.

Mark superseded sideband design sections explicitly rather than rewriting historical commits as if they never existed.

**Step 4: Verify docs and commit**

```bash
git diff --check
uv run pytest -q tests/test_ui_contract.py

git add README.md docs tests/test_ui_contract.py
git commit -m "docs: document single-transport realtime lifecycle"
```

### Task 7: Full verification and deployment gate

**Objective:** Prove the branch, built artifact, installed runtime, and live native path satisfy the approved acceptance criteria.

**Files:**
- Modify only if verification exposes a tested defect.

**Step 1: Run complete automated verification**

```bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
npm run check:web
git diff --check
git status --short
```

Expected: all tests pass, lint/compile/build checks exit 0, and only intentional files are present.

**Step 2: Request two-stage review**

Run an independent spec-compliance review against the approved design, then a code-quality review. Fix findings test-first and repeat the full gate.

**Step 3: Merge and deploy only after review**

Use the repository’s existing install/deploy command from the active runbook. Restart controller and wakeword sequentially, waiting for controller health before starting wakeword; do not use one combined restart transaction.

**Step 4: Verify installed artifacts**

Confirm installed Python/browser/package files contain direct `RTCPeerConnection`, scoped `/execute`, and no sideband/Agents SDK imports or reconnect markers. Confirm both services remain active beyond the startup window and health reports `gpt-realtime-2.1`.

**Step 5: Run installed live smoke**

Exercise, with real journal/trace evidence:

1. wake phrase opens the dedicated page;
2. two ordinary speech turns complete;
3. `assistant_get_current_time` succeeds;
4. simulated local tool failure shows **Tools unavailable** without closing voice;
5. one interrupted response continues normally;
6. explicit Stop closes media/browser and rearms wakeword;
7. no sideband attach/reconnect marker appears.

If any live criterion fails, do not call deployment complete; preserve logs, write a failing regression test, fix, redeploy, and repeat.
