# OpenAI Realtime Action Spike Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a local Streamlit feasibility spike that holds a natural OpenAI Realtime voice conversation, exposes a small typed assistant-action contract, executes safe mock handlers, and shows incoming execution requests and Realtime events live.

**Architecture:** A Streamlit page embeds a browser WebRTC panel. The panel sends its SDP offer to a loopback FastAPI gateway, which creates the OpenAI Realtime call through the unified `/v1/realtime/calls` interface so the permanent API key never reaches the browser. Realtime function calls are displayed in the panel, sent to the gateway's allowlisted execution broker, returned to the model as `function_call_output`, and followed by `response.create` for a natural spoken acknowledgement.

**Tech Stack:** Python 3.12, uv, Streamlit, FastAPI, httpx, Pydantic, browser WebRTC/JavaScript, pytest, Ruff.

**Status:** Working demo/prototype. Initial implementation is complete; continued work follows the accepted provider-boundary decision recorded on 2026-07-21.

---

## Accepted continuation decision — 2026-07-21

The prototype will support only OpenAI `gpt-realtime`. This is both a focused demo and an evolving prototype; fidelity to OpenAI's native contract takes priority over provider interchangeability.

Continued implementation rules:

1. Perform continued `gpt-realtime` work on a dedicated branch, separate from shared/default OHV integration work.
2. Keep WebRTC negotiation, session configuration, OpenAI event handling, transcription, interruption, function-call extraction, `function_call_output`, and response continuation in an explicitly OpenAI-specific path.
3. Do not introduce a shared realtime-provider adapter, base client, normalized provider event bus, common session manager, or provider-selection conditionals.
4. Do not rename concrete OpenAI concepts into generic names merely to imply future portability.
5. If another provider is considered later, build and test its complete lifecycle on its own branch and in its own parallel code path.
6. Provider paths may call stable external OHV services for authorization, local capability execution, logging, and policy. Those service boundaries do not make provider transport or conversation logic shared.
7. Select the provider-specific lifecycle before opening the microphone or creating a remote session. Do not switch providers inside an active conversation.

This decision deliberately rejects an adapter architecture. The provider APIs differ in transport, session state, reconnection, event ordering, interruption, transcripts, and tool continuation; hiding those differences would make the prototype harder to understand and debug.

Design details and acceptance criteria: [`../design/2026-07-21-gpt-realtime-prototype-boundary.md`](../design/2026-07-21-gpt-realtime-prototype-boundary.md).

## Scope and safety

This is an OpenAI `gpt-realtime` feasibility spike, demo, and prototype—not an OHV production integration or a multi-provider abstraction.

Initial capabilities are drawn from the OHV router contract:

- `assistant_get_current_time`
- `assistant_start_timer`
- `media_play`
- `media_control`
- `voice_end_session`
- `agent_delegate_task`

All handlers are safe simulations except current-time lookup. No shell commands, OS application launch, Spotify credentials, destructive actions, or Hermes dispatch are allowed in this spike. The browser may end its own call after a successful `voice_end_session` result.

The model is `gpt-realtime-2.1-mini` with `reasoning.effort=minimal`, audio output, `marin`, semantic VAD with high eagerness, automatic responses and interruption enabled, and `tool_choice=auto`.

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `.env.example`

**Steps:**
1. Declare runtime and test dependencies.
2. Pin the local runtime to Python 3.12.
3. Ignore `.env`, virtual environments, caches, logs, and generated artifacts.
4. Run `uv sync --all-groups` and verify dependency installation.
5. Commit the scaffold.

## Task 2: Capability contract and execution broker

**Files:**
- Test: `tests/test_capabilities.py`
- Create: `src/realtime_action_spike/capabilities.py`
- Create: `src/realtime_action_spike/__init__.py`

**Steps:**
1. Write failing tests proving the OpenAI tool list contains the six allowlisted functions with strict object schemas.
2. Write failing tests for valid execution, invalid JSON, unknown functions, missing required arguments, extra arguments, timer bounds, and deterministic structured output.
3. Run the focused tests and verify RED because the package is absent.
4. Implement immutable capability definitions, Pydantic argument models, and a broker that maps names to handlers without `eval`, shell execution, or arbitrary imports.
5. Run the focused tests and verify GREEN.
6. Commit the capability contract.

## Task 3: Realtime session configuration and gateway

**Files:**
- Test: `tests/test_gateway.py`
- Create: `src/realtime_action_spike/config.py`
- Create: `src/realtime_action_spike/gateway.py`

**Steps:**
1. Write failing tests for the session payload, missing-key behavior, health response, successful SDP relay, and upstream error propagation without leaking credentials.
2. Run the focused tests and verify RED.
3. Implement environment loading and the server-owned session configuration.
4. Implement `GET /health`, `POST /session` for raw SDP relay, and `POST /execute` for allowlisted calls.
5. Bind CORS only to loopback Streamlit origins.
6. Run the focused tests and verify GREEN.
7. Commit the gateway.

## Task 4: Streamlit WebRTC panel

**Files:**
- Test: `tests/test_ui_contract.py`
- Create: `src/realtime_action_spike/web/realtime_panel.html`
- Create: `src/realtime_action_spike/streamlit_app.py`

**Steps:**
1. Write failing static contract tests for Start/Stop controls, WebRTC setup, loopback `/session` and `/execute` calls, `response.done` function-call handling, deduplication by `call_id`, `function_call_output`, `response.create`, execution inspector, event log, and cleanup of microphone tracks.
2. Run the focused tests and verify RED.
3. Implement a single Streamlit page embedding the panel at full width.
4. Implement WebRTC media, data-channel events, tool execution, visible request/result cards, event filtering, error states, and complete disconnect cleanup.
5. Run the focused tests and verify GREEN.
6. Commit the UI.

## Task 5: Launcher and documentation

**Files:**
- Create: `scripts/run.py`
- Create: `README.md`

**Steps:**
1. Implement a launcher that starts the gateway on `127.0.0.1:8765`, waits for `/health`, then starts Streamlit on `127.0.0.1:8501`; forward signals and terminate both children on exit.
2. Document environment setup, `uv run python scripts/run.py`, the mock action phrases, security boundaries, and expected limitations.
3. Add a launcher dry-run/import test if practical.
4. Run Ruff and the full pytest suite.
5. Commit the runnable spike.

## Task 6: Verification

1. Run `uv run pytest -q`.
2. Run `uv run ruff check .`.
3. Run `uv run python -m compileall src scripts`.
4. Start the launcher and verify `GET http://127.0.0.1:8765/health` and `GET http://127.0.0.1:8501/_stcore/health` return healthy responses.
5. If `OPENAI_API_KEY` is available, submit a synthetic SDP/API session request or connect in the browser and confirm a provider session is created. Never print the key.
6. In the browser, verify microphone permission, several conversational turns, interruption, at least one tool request in the inspector, matching execution output, spoken acknowledgement, and clean Stop behavior.
7. Record any credential, account-tier, microphone, browser-policy, or provider-schema blocker precisely rather than claiming an unperformed live voice test.

### Continuation verification — 2026-07-21

- `uv run pytest -q`: 44 passed.
- `uv run ruff check .`: passed.
- `uv run python -m compileall -q src scripts`: passed.
- Gateway and Streamlit health endpoints returned healthy responses.
- An isolated Brave session using fake microphone input established a real OpenAI Realtime connection.
- A text turn sent through the live Realtime data channel selected `assistant_get_current_time`; the local broker completed the Tokyo-time request, returned `function_call_output`, and the session continued without an error.
- Stop returned the panel to `Not connected`, re-enabled Start, disabled Stop, and left no visible error.

## Continuation gates

Before expanding the prototype:

1. Finish and verify the current responsive-layout and transcription checkpoint without mixing it with architecture refactoring.
2. Begin subsequent prototype work on the dedicated `gpt-realtime` branch from a clean, verified baseline.
3. Preserve the existing OpenAI-specific flow: browser WebRTC → `/v1/realtime/calls` → OpenAI events → local allowlisted execution → `function_call_output` → `response.create`.
4. Add production-like actions one at a time, with focused broker, gateway, UI-contract, and live-provider verification.
5. Keep any future provider experiment out of this module, its tests, and its branch.

The prototype remains acceptable only while:

- no generic provider adapter or base class exists;
- no provider dropdown or runtime provider switch exists;
- OpenAI event names and lifecycle transitions remain directly inspectable;
- local authorization and execution remain outside model control;
- removing a future provider path would require no changes to this OpenAI path.
