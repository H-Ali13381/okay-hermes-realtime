# Generic Tool Actions and Final Capability Skeleton Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Implement the approved generic trusted-effect path and prepare explicit, non-exposed Python service skeletons for the finalized timer, media, and Hermes-task capability families.

**Architecture:** Keep OpenAI function-call handling native and directly inspectable. The existing `CapabilityBroker` remains the only model-facing allowlist. Registered Python handlers return typed outcomes containing data and trusted effects; a browser effect registry processes those effects without branching on capability names. Future product actions live behind typed local Python service methods and remain absent from `CAPABILITIES` until real implementations and end-to-end tests exist.

**Tech Stack:** Python 3.12, Pydantic, dataclasses, FastAPI, browser JavaScript/WebRTC, pytest, Ruff.

**Design sources:**

- `docs/design/2026-07-21-webrtc-wakeword-replacement.md:178-192`
- `docs/plans/2026-07-17-openai-realtime-action-spike.md:33-46`
- `docs/superpowers/specs/2026-07-23-generic-tool-action-management-design.md`

---

### Task 1: Scaffold finalized local capability services without exposing them

**Objective:** Establish stable Python method and request boundaries for timer, media, and Hermes task actions while making every unfinished operation fail explicitly.

**Files:**

- Create: `src/realtime_action_spike/capability_services.py`
- Create: `tests/test_capability_services.py`
- Verify: `tests/test_capabilities.py`

**Step 1: Write failing skeleton-contract tests**

Add tests that import:

- `ActionNotImplementedError`;
- `TimerStartRequest`, `TimerCancelRequest`, and `TimerActions`;
- `MediaPlayRequest`, `MediaSkipRequest`, and `MediaActions`;
- `HermesTaskStartRequest`, `HermesTaskReference`, and `HermesTaskActions`.

Assert that each unfinished method raises `ActionNotImplementedError` naming the exact action:

- `timer.start`, `timer.cancel`;
- `media.play`, `media.pause`, `media.resume`, `media.skip`;
- `hermes.task.start`, `hermes.task.cancel`, `hermes.task.status`.

Assert request models reject extra fields and enforce bounded existing contract fields.

**Step 2: Run the focused test to verify RED**

Run:

```bash
uv run pytest -q tests/test_capability_services.py
```

Expected: collection failure because `realtime_action_spike.capability_services` does not exist.

**Step 3: Implement the minimal scaffold**

Create one flat module with strict Pydantic request models and three service classes. Use a shared helper returning `NoReturn` so methods cannot accidentally return `None`:

```python
class ActionNotImplementedError(RuntimeError):
    """A finalized capability has a contract but no executable service yet."""


def _not_implemented(action: str) -> NoReturn:
    raise ActionNotImplementedError(f"action is not implemented: {action}")
```

The service methods must contain no simulation, shell execution, subprocess invocation, network call, generated IDs, or success-shaped return value.

**Step 4: Run focused tests to verify GREEN**

```bash
uv run pytest -q tests/test_capability_services.py tests/test_capabilities.py
uv run ruff check src/realtime_action_spike/capability_services.py tests/test_capability_services.py
```

Expected: all tests pass and Ruff reports no errors.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/capability_services.py tests/test_capability_services.py
git commit -m "feat: scaffold finalized capability services"
```

---

### Task 2: Add typed Python tool outcomes and trusted effects

**Objective:** Replace arbitrary handler dictionaries with a typed outcome that separates tool data from allowlisted effects.

**Files:**

- Modify: `src/realtime_action_spike/capabilities.py`
- Modify: `tests/test_capabilities.py`
- Modify: `tests/test_gateway.py`

**Steps:**

1. Write failing tests for `ToolOutcome`, empty effects on current time, and one `conversation.finish` effect on session end.
2. Verify RED with focused tests.
3. Implement the smallest immutable outcome/effect types and serialization.
4. Preserve gateway scope, replay cache, conflict rejection, and output shape except for the new `effects` array.
5. Run focused capability and gateway suites.
6. Commit as `feat: add trusted realtime tool effects`.

---

### Task 3: Extract a testable browser effect dispatcher

**Objective:** Process trusted effects sequentially without capability-name checks in the Realtime function-call loop.

**Files:**

- Create: `frontend/effects.js`
- Create: `tests/web/effects.test.mjs`
- Modify: `frontend/voice.js`
- Modify: `package.json`
- Modify: `tests/test_ui_contract.py`

**Steps:**

1. Write Node tests for ordered dispatch, unknown-effect logging, malformed effects, and effect failure reporting.
2. Verify RED because the module does not exist.
3. Implement a small dependency-injected registry with no DOM or WebRTC globals.
4. Integrate it into `executeFunctionCall` while retaining one tool output and one continuation.
5. Remove `output.result?.end_session` and capability-name branching.
6. Run Node and Python UI-contract tests.
7. Commit as `refactor: dispatch trusted realtime effects`.

---

### Task 4: Implement playback-safe `conversation.finish`

**Objective:** Prevent provider-side barge-in during the final response and close only after its matching WebRTC output buffer drains.

**Files:**

- Create: `frontend/conversation-finish.js`
- Create: `tests/web/conversation-finish.test.mjs`
- Modify: `frontend/voice.js`
- Modify: `tests/test_ui_contract.py`

**Steps:**

1. Write reducer tests for VAD-lock request, effective `session.updated` acknowledgement, response-ID capture, stale playback-stop rejection, matching playback-stop completion, failure restore, and exactly-once teardown.
2. Verify RED.
3. Implement the reducer/state machine without muting or stopping either media track.
4. Send the final tool output and `response.create` only after effective VAD lock acknowledgement.
5. Restore prior turn detection on failed/cancelled farewell.
6. Run focused browser and UI-contract tests.
7. Commit as `feat: finish realtime sessions after playback`.

---

### Task 5: Correct tool prompt and action wording

**Objective:** Remove obsolete simulation claims and make success/failure language follow authoritative tool results.

**Files:**

- Modify: `src/realtime_action_spike/config.py`
- Modify: `src/realtime_action_spike/capabilities.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_capabilities.py`

**Steps:**

1. Write failing assertions for authoritative result wording and explicit close-tool usage.
2. Verify RED.
3. Remove “most side effects are simulated” and return/result fields that falsely imply simulation.
4. Run focused tests.
5. Commit as `fix: align realtime prompt with local actions`.

---

### Task 6: Rebuild and verify the complete change

**Objective:** Prove source, generated browser asset, Python runtime, and repository hygiene agree.

**Files:**

- Generated: `src/realtime_action_spike/web/voice.js`
- Verify: entire repository

**Steps:**

1. Run `npm ci` and browser build/check/test commands.
2. Run targeted capability, gateway, UI, and browser-action suites.
3. Run full `uv run pytest -q`.
4. Run `uv run ruff check .`.
5. Run `uv run python -m compileall -q src scripts`.
6. Run `git diff --check` and verify source/bundle parity.
7. Commit generated assets if changed.

---

### Task 7: Deploy and live-smoke

**Objective:** Verify one data-only tool and one lifecycle tool through the installed local runtime.

**Steps:**

1. Install from the tested worktree using the existing isolated-venv installer path.
2. Restart only the dedicated okay-hermes-realtime services.
3. Verify health and controller readiness.
4. Live-smoke `assistant_get_current_time` through Realtime.
5. Live-smoke explicit session close and confirm the farewell plays fully.
6. Confirm browser/controller teardown, wakeword rearm, and no loose dedicated-session PID.
7. Record exact trace paths and measured outcomes; do not claim unsupported future skeleton actions work.
