# Farewell Playback Close Design

## Goal

Make `voice_end_session` stop accepting microphone input immediately while allowing the model's final spoken farewell to finish before the existing deterministic teardown closes WebRTC, the app window, and its dedicated Brave process.

## Scope

This change is browser lifecycle sequencing only. It preserves direct browser-owned WebRTC, scoped local `/execute`, controller teardown, and native wakeword rearming. It does not add timers, reconnect logic, provider sideband, or new capabilities.

## Close state

Replace the boolean `disconnectAfterResponse` with explicit pending-close state:

- `idle`: normal bidirectional conversation;
- `awaiting_farewell`: a successful `voice_end_session` result was accepted, microphone tracks are disabled, and the continuation response is pending;
- `awaiting_playback_stop`: the farewell response completed successfully and buffered WebRTC playback is draining.

The state records the accepted tool `call_id` for diagnostics and deduplication.

## Event flow

1. The model emits `voice_end_session` function-call arguments.
2. The browser invokes authenticated, session-scoped `POST /execute`.
3. A failed or malformed execution is rendered and logged but does not alter media or close state.
4. When execution succeeds with `result.end_session === true`, the browser:
   - disables every local audio track immediately;
   - records the accepted close `call_id`;
   - enters `awaiting_farewell`;
   - sends `function_call_output` and one `response.create` over the existing data channel.
5. The function-call response's own `response.done` cannot close the session because it contains a function-call item.
6. A successful farewell `response.done` with no function calls advances the state to `awaiting_playback_stop`. It does not tear down transport.
7. A later `output_audio_buffer.stopped` event closes through the existing `stopConversation({ reason: "model_request" })` path.
8. Existing teardown closes data channel and peer connection, stops local tracks, sends controller acknowledgements, closes the dedicated app window, and permits wakeword rearming.

A playback-stopped event received before successful farewell completion is stale for this purpose and must not close the session.

## Failure behavior

- Invalid or failed close action: log/render the failure and remain fully interactive.
- Farewell `response.done` with failed, cancelled, or incomplete status: log a bounded `close.failed` diagnostic, clear pending-close state, re-enable live local audio tracks, and remain open.
- Provider `error` while a close is pending: log the provider error, cancel pending close, restore input, and remain open.
- Missing `output_audio_buffer.stopped`: remain muted and open rather than guessing that playback ended. No arbitrary teardown timeout is added.
- Duplicate function-call delivery remains suppressed by the existing handled-call-ID set.

## Model wording

The `voice_end_session` tool description must state that a successful local result schedules a real window/session close after a brief spoken goodbye. The model must not describe the action as simulated.

## Verification

Automated contract coverage must prove:

1. successful close execution disables local audio tracks immediately;
2. failed execution does not disable input or queue close;
3. farewell `response.done` does not call teardown;
4. stale `output_audio_buffer.stopped` does not close;
5. successful farewell followed by `output_audio_buffer.stopped` invokes normal teardown exactly once;
6. failed/cancelled farewell restores microphone input and logs failure;
7. the tool description represents a real local close;
8. browser build, UI contracts, full Python suite, Ruff, compileall, and diff checks pass.

Installed verification must confirm the deployed bundle matches the tested artifact, controller/listener health is clean, one live direct-WebRTC activation reaches `live`, and closing after a spoken farewell produces clean controller/browser exit with no loose dedicated-browser process.
