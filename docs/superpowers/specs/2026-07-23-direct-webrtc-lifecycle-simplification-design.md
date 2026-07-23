# Direct WebRTC Lifecycle Simplification

**Date:** 2026-07-23
**Status:** Approved design; implementation not started
**Branch:** `refactor/simplify-realtime-lifecycle`

## Objective

Restore the stable media and tool lifecycle from the Streamlit prototype while retaining the native wakeword shell, tray, dedicated Brave window, server-owned credentials, allowlisted tool execution, traces, and deterministic wake rearm.

The conversation-critical path must contain only the browser's OpenAI WebRTC peer/data channel and the local controller that creates the call. Auxiliary diagnostics and tool delivery must not terminate otherwise usable voice media.

## Non-goals

- Reintroducing Streamlit.
- Replacing the native wake listener or tray.
- Changing the model, voice, prompt, VAD, or interruption policy in this slice.
- Adding tools beyond `assistant_get_current_time` and `voice_end_session`.
- Adding another provider or a generic provider abstraction.
- Changing existing OHV code, units, configuration, or installed paths.

## Selected architecture

```text
native wake listener
        |
        v
local controller ---- launches/stops ----> dedicated Brave page
        |                                      |
        | creates call                         | microphone + speaker
        |                                      v
        +-----------------------------> OpenAI WebRTC
                                               |
                                               +-- oai-events data channel

browser function call
        |
        v
authenticated loopback /execute
        |
        v
controller-owned allowlisted broker
        |
        v
browser returns function_call_output over oai-events
```

### Browser responsibilities

- Acquire microphone media with echo cancellation, noise suppression, and automatic gain control.
- Create and own a direct `RTCPeerConnection` and `oai-events` data channel.
- Apply the controller-provided SDP answer.
- Render remote model audio and visible transcript/state.
- Parse function-call events from the OpenAI data channel.
- Send bounded tool requests to the authenticated loopback execution endpoint.
- Send validated `function_call_output` and continuation events back to OpenAI.
- Stop tracks, data channel, peer connection, and audio on explicit teardown.

### Controller responsibilities

- Keep the permanent OpenAI API key server-side.
- Accept the browser SDP offer and create the OpenAI Realtime call through `/v1/realtime/calls`.
- Return only the SDP answer and an opaque local execution scope.
- Bind the scope to the current local activation/session.
- Validate tool name, argument schema, payload size, active scope, and duplicate `call_id` behavior.
- Execute only allowlisted capabilities.
- Launch and close only the dedicated Brave process.
- Coordinate explicit Stop, close phrase, service shutdown, and wakeword rearm.
- Persist bounded lifecycle and timing diagnostics.

## Removed conversation dependencies

The following are removed from session startup and active-conversation health:

- OpenAI sideband WebSocket attachment;
- sideband reconnect and HTTP 404 recovery behavior;
- provider `call_id` relay from browser to controller;
- sideband function-call parser and tool loop;
- sideband-driven action-state relay;
- sideband failure as a teardown trigger;
- Agents SDK WebRTC transport ownership.

Code that becomes unreachable after the migration must be deleted rather than retained behind flags. Tests specific to removed behavior must also be deleted or replaced with direct-WebRTC contracts.

## Session establishment

1. Native listener detects the wake phrase and pauses inference.
2. Activation handler requests a session from the controller.
3. Controller launches the dedicated Brave page with an opaque activation credential.
4. Page authenticates to the loopback controller.
5. Page acquires microphone media and creates a direct WebRTC offer.
6. Page posts the SDP offer to the controller.
7. Controller creates the OpenAI Realtime call using the server-owned session configuration.
8. Controller binds an opaque local execution scope to the active session and returns SDP answer plus scope.
9. Page applies the answer and treats the data channel opening as media readiness.
10. Activation startup completes when the direct peer/data channel is live; no sideband acknowledgement is required.

Late SDP answers, stale activation credentials, and superseded local scopes remain rejected.

## Tool flow

1. Browser observes a completed OpenAI function-call item.
2. Browser validates that `call_id`, function name, and argument text are present and bounded.
3. Browser sends `{scope, call_id, name, arguments}` to the authenticated loopback `/execute` endpoint.
4. Controller verifies the active scope and uses the existing allowlisted capability broker.
5. Controller returns a structured result. Identical duplicate `call_id` requests replay the cached result; changed-payload reuse is rejected.
6. Browser sends a `conversation.item.create` event containing a `function_call_output` item with the same `call_id` and JSON-string output.
7. Browser sends `response.create` unless the tool is `voice_end_session`.
8. A successful `voice_end_session` result enters the normal explicit teardown path.

The browser never receives the permanent API key and never executes model-generated shell or code.

## Failure semantics

### Fatal to the active conversation

- WebRTC peer state remains `disconnected` for three continuous seconds.
- WebRTC peer state becomes `failed`.
- OpenAI data channel closes or errors unexpectedly.
- Initial call/SDP setup cannot complete within the startup deadline.
- Explicit Stop, X-close, close phrase, tray Turn Off, or service shutdown requests teardown.

### Degraded but non-fatal

- Local `/execute` request fails, times out, or returns an authorization/execution error.
- Diagnostic persistence fails.
- Action-state rendering fails.
- A nonessential local control message cannot be delivered after media is live.

For degraded tool failures, the page shows **Tools unavailable** and voice conversation continues. If a pending model tool call cannot be fulfilled, the browser returns a structured failure output to OpenAI when the data channel remains available so the model can explain the limitation naturally.

### Control-channel loss

The local control WebSocket remains responsible for controller-requested Stop and teardown acknowledgement. Once WebRTC is live, transient control loss does not immediately terminate media. The page attempts bounded local control reconnection. If reconnection remains unavailable, the page keeps the conversation usable but retains local Stop/X cleanup. Wakeword rearm occurs when the browser session ends or the controller's bounded session deadline expires.

This slice does not permit an unbounded orphaned browser session.

## Interruption behavior

The provider session configuration remains unchanged:

- `semantic_vad`;
- `eagerness: high`;
- `interrupt_response: true`;
- `create_response: true`.

The direct WebRTC implementation records provider speech-start and response terminal status. VAD tuning is deliberately deferred until this architecture change is deployed and interruption traces can be evaluated independently.

## Diagnostics

Persist bounded, sanitized markers for:

- browser launch;
- peer connection states;
- data-channel open/error/close;
- control WebSocket open/error/close/reconnect;
- session ready;
- provider response status;
- VAD speech start/stop;
- tool request/result/failure without secrets or raw audio;
- teardown initiator and completion.

Embedded audio and permanent credentials must never be logged or rendered.

## Migration strategy

1. Add regression tests for the desired direct session, tool, degraded-mode, and transport-grace contracts.
2. Restore direct SDP/data-channel behavior using the proven prototype implementation as the behavioral reference.
3. Add authenticated controller-side execution scope and endpoint using existing capability validation/deduplication primitives.
4. Remove sideband from startup, controller ownership, teardown, package dependencies, and browser bundle.
5. Remove Agents SDK transport wiring and its package dependency.
6. Preserve native launch/process/teardown boundaries that are independent of sideband.
7. Rebuild the browser bundle and update architecture/runbook documentation.

No compatibility flag or dual sideband/direct mode will be retained.

## Verification gates

### Automated

- Direct SDP session establishment and stale-scope rejection.
- Browser data-channel function call to authenticated local execution and provider continuation.
- Duplicate `call_id` replay and changed-payload rejection.
- Tool failure enters degraded mode without teardown.
- Control-channel loss after media readiness does not immediately tear down WebRTC.
- Transient WebRTC disconnect shorter than three seconds recovers.
- Sustained disconnect and failed/data-channel-close states tear down.
- Sideband is absent from startup and failure paths.
- No browser bundle import of `@openai/agents/realtime` remains.
- Full Python suite, Ruff, Python compilation, web build/check, and `git diff --check` pass.

### Installed smoke

1. Deploy only after automated gates pass.
2. Verify controller and wakeword services remain active beyond the startup window.
3. Wake phrase opens the dedicated page.
4. Complete at least two ordinary speech turns.
5. Execute `assistant_get_current_time` successfully.
6. Confirm a simulated local tool failure does not close voice.
7. Interrupt one response and observe continued conversation.
8. Stop explicitly and confirm browser cleanup plus wakeword rearm.
9. Inspect installed files and journal for sideband attachment/reconnect markers; none may occur.

## Acceptance criteria

- Ordinary conversations no longer depend on an OpenAI sideband connection.
- A local tool/control failure cannot terminate healthy WebRTC media.
- Actual media failure remains bounded and observable.
- Native wake, dedicated page, explicit teardown, and rearm remain functional.
- The deployed runtime contains one OpenAI conversation transport rather than parallel WebRTC and sideband connections.
