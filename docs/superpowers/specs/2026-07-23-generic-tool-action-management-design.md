# Generic Tool Action Management Design

## Goal

Turn OpenAI Realtime function-call JSON into validated Python method calls and process their trusted results through one generic action-management path. `voice_end_session` is one ordinary capability, not a special case in the Realtime event loop.

The close capability must prevent accidental barge-in during its final spoken response without muting the browser, microphone track, or assistant output. It closes only after OpenAI confirms the matching WebRTC output buffer is fully drained.

## Scope

This change preserves:

- browser-owned WebRTC and the `oai-events` data channel;
- authenticated, active-session-scoped `POST /execute`;
- server-side Python validation, policy, idempotency, and execution;
- existing controller/browser teardown and native wakeword rearming;
- existing `call_id` deduplication and conflicting-reuse rejection.

This change does not add external scripts, shell execution, provider sideband, another transport, reconnect machinery, arbitrary close timers, or unrelated capabilities.

## Architecture

```text
Realtime function_call JSON
        |
        v
browser extracts call_id + name + arguments
        |
        v
authenticated, scoped POST /execute
        |
        v
Python CapabilityBroker registry
        |
        +-- validate function name
        +-- parse JSON object
        +-- validate strict Pydantic arguments
        +-- invoke registered Python handler
        |
        v
ToolOutcome(result, effects)
        |
        v
browser EffectDispatcher
        |
        +-- apply recognized trusted effects in order
        +-- bounded-log unknown/failed effects
        |
        v
function_call_output + sequenced response.create
```

The model supplies only the function name and schema-bound arguments. It cannot directly manufacture client effects. Effects originate from trusted Python capability handlers after validation.

## Python capability contract

A registered capability contains:

- provider-facing function name and description;
- strict Pydantic argument model with extra fields forbidden;
- Python handler method;
- execution classification for diagnostics.

Handlers return a typed `ToolOutcome`:

```python
ToolOutcome(
    result={"reason": "User requested close"},
    effects=(ConversationFinishEffect(),),
)
```

The serialized `/execute` response keeps the existing envelope and adds an `effects` array:

```json
{
  "ok": true,
  "call_id": "call_123",
  "capability": "voice_end_session",
  "execution": "local",
  "result": {
    "reason": "User requested close"
  },
  "effects": [
    {
      "type": "conversation.finish"
    }
  ]
}
```

Capabilities without client lifecycle work return an empty effect list. For example, `assistant_get_current_time` executes entirely in Python and returns data only.

## Generic effect dispatcher

The browser owns a small allowlisted effect registry keyed by `effect.type`. It does not branch on capability names or inspect ad hoc fields such as `result.end_session`.

For every successful `/execute` response:

1. validate that `effects` is an array;
2. process effects sequentially in response order;
3. dispatch each recognized effect to its handler;
4. bounded-log malformed, unknown, or failed effects;
5. include effect failure information in the tool output so the model cannot claim an unapplied side effect succeeded;
6. send exactly one `function_call_output` for the original `call_id`;
7. request continuation only after all pre-continuation effects settle.

The existing handled-call-ID set and gateway replay cache continue to make retries idempotent.

## `conversation.finish` effect

`voice_end_session` emits one `conversation.finish` effect. Its handler owns the close sequence; the generic Realtime function-call loop remains capability-agnostic.

### 1. Lock automatic interruption

The handler sends `session.update` preserving the active turn-detection type and tuning while setting:

```json
{
  "audio": {
    "input": {
      "turn_detection": {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": false,
        "interrupt_response": false
      }
    }
  }
}
```

Neither local nor remote media is muted, paused, stopped, or detached. User speech may still be detected, but it cannot cancel the farewell or automatically create another response.

The handler waits for a `session.updated` event whose effective turn-detection configuration confirms both flags are false. Merely sending `session.update` is not sufficient because WebRTC audio and control travel on separate channels.

### 2. Request the final response

After acknowledgement, the browser sends:

1. `conversation.item.create` with one JSON-string `function_call_output` using the original `call_id`;
2. exactly one `response.create`.

The handler records the next farewell `response.id` from `response.created` and enters an awaiting-playback state.

### 3. Wait for audible completion

`response.done` means generation completed; it does not mean buffered audio finished playing. It records terminal response status but never closes a successful audio response by itself.

The close handler accepts only:

```text
output_audio_buffer.stopped.response_id == recorded farewell response.id
```

OpenAI defines this WebRTC/SIP event as the point where the server output buffer is completely drained and no more audio is forthcoming. A stale event for another response is logged and ignored.

On the matching event, the handler invokes the existing ordinary teardown once. Teardown closes the data channel and peer connection, stops local tracks, acknowledges controller teardown, closes the dedicated app window/process, and allows wakeword rearming.

## Failure and invalid-input policy

Failures do not silently mutate lifecycle state.

| Failure | Behavior |
|---|---|
| Unknown capability or invalid arguments | Gateway returns structured error; browser logs/renders it and continues |
| Duplicate identical `call_id` | Return cached result; do not execute again |
| Conflicting reuse of `call_id` | Reject and log conflict |
| Malformed or unknown effect | Bounded-log and ignore it; report effect failure in tool output |
| VAD lock rejected or unacknowledged | Do not queue close; log failure; continue with normal VAD |
| Farewell response failed, cancelled, or incomplete | Restore the previous turn-detection configuration, log failure, remain open |
| Provider error during pending close | Restore previous turn detection when transport permits, log failure, remain open |
| Stale playback-stopped event | Log/ignore; remain pending |
| Matching playback-stopped event | Run teardown exactly once |

There is no arbitrary close timeout. Transport failure and explicit native/user Stop continue through their existing teardown paths.

## Prompt and tool wording

The session prompt must not claim that most actions are simulated. It should say that each tool result is authoritative about whether execution was local, simulated, rejected, or failed.

The `voice_end_session` description must tell the model to call it only after an explicit user request and, after a successful result, provide one brief natural goodbye. It must not describe a successful local close as simulated.

## Testing

### Python contract tests

- Provider tool schemas come from the registered typed capabilities.
- Valid JSON invokes the correct Python handler.
- Unknown names, malformed JSON, non-object JSON, extra fields, and invalid values are rejected.
- `assistant_get_current_time` returns data with no effects.
- `voice_end_session` returns one typed `conversation.finish` effect.
- `/execute` preserves scope checks, replay semantics, call limits, and structured errors.

### Browser action tests

Tests must exercise reducer/handler behavior rather than only search source strings:

- recognized effects dispatch sequentially;
- malformed/unknown effects are logged and ignored;
- capability names are absent from the generic function-call loop;
- finish sends a VAD-lock `session.update` without muting/stopping either media path;
- continuation waits for effective `session.updated` acknowledgement;
- successful acknowledgement emits one tool output and one `response.create`;
- `response.done` alone never closes;
- stale playback-stop response IDs never close;
- matching playback-stop closes exactly once;
- failed/cancelled farewell restores previous VAD policy and remains open;
- duplicate function-call delivery does not execute or close twice.

### Final gates

- browser build/check;
- targeted capability, gateway, and browser action suites;
- full Python suite;
- Ruff and compileall;
- `git diff --check`;
- installed bundle hash matches the tested artifact;
- live smoke of one data-only capability and one close capability;
- close phrase produces a full audible farewell, clean controller/browser exit, wakeword rearm, and no loose dedicated-session PID.

## Authoritative API evidence

OpenAI documents that:

- VAD interruption automatically cancels an in-progress response;
- `turn_detection.interrupt_response` and `create_response` can be disabled with `session.update` while retaining VAD;
- `session.updated` reports the effective configuration;
- `output_audio_buffer.stopped` is WebRTC/SIP-only, occurs after `response.done`, includes `response_id`, and means the server output buffer is completely drained with no more audio forthcoming.

References:

- <https://developers.openai.com/api/docs/guides/realtime-conversations>
- <https://developers.openai.com/api/reference/resources/realtime/client-events>
- <https://developers.openai.com/api/reference/resources/realtime/server-events>
