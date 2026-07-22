# GPT Realtime Prototype Boundary

**Status:** Accepted

**Date:** 2026-07-21

**Applies to:** OpenAI Realtime Action Spike and any later OHV integration derived from it

## Decision

The current demo and prototype supports only OpenAI `gpt-realtime`.

Continued work will happen on a dedicated branch. The implementation will follow OpenAI's native API contract directly and will be polished over time without introducing a generic realtime-provider adapter.

Future realtime providers, if explored, will use independent branches and parallel provider-specific code paths. They will not share transport, session, event, transcript, interruption, reconnection, or tool-continuation implementations with the OpenAI path.

## Why

Realtime voice providers expose superficially similar capabilities but materially different lifecycles:

- browser and server transports;
- session creation and authentication;
- state ownership and reconnection;
- audio framing and playback;
- event identity and ordering;
- voice activity detection and interruption;
- input and output transcript timing;
- function-call continuation;
- hosted tools and approval semantics.

A shared adapter would either expose only the lowest common denominator or leak provider-specific exceptions through a supposedly generic interface. Both outcomes make failures harder to diagnose and encourage accidental coupling before a second implementation has produced evidence of genuine commonality.

This prototype should optimize for clarity, observability, and faithful use of `gpt-realtime`, not speculative portability.

## OpenAI-specific ownership

The `gpt-realtime` path owns its complete remote-conversation lifecycle:

1. Capture microphone media in the browser.
2. Create the OpenAI Realtime call through the loopback gateway and `/v1/realtime/calls`.
3. Configure the OpenAI model, voice, reasoning, transcription, VAD, interruption, prompt, and tools.
4. Send and receive media through WebRTC.
5. Process OpenAI data-channel events using their native event names and identifiers.
6. Correlate input transcripts, output transcripts, response items, and function calls.
7. Send allowlisted calls to the local execution boundary.
8. Return results as OpenAI `function_call_output` items.
9. Issue `response.create` when OpenAI requires explicit continuation.
10. Cancel, stop, and clean up the OpenAI call, data channel, peer connection, and microphone tracks.

These responsibilities must not be moved behind generic names such as `RealtimeProvider`, `ProviderSession`, `NormalizedRealtimeEvent`, or `UniversalToolCall`.

## Local execution boundary

OHV retains control over:

- capability authorization;
- argument validation;
- local execution;
- idempotency;
- policy and confirmation;
- audit logging;
- user-visible action state.

The OpenAI path may call those stable services as external dependencies. That boundary is not a provider adapter: the OpenAI path still owns how an OpenAI function call is detected, correlated, converted into an authorized local request, and returned to the model.

The model never receives arbitrary shell access, evaluates code, or bypasses the allowlist.

## Branch and code-path policy

- Keep this private spike as the evidence-producing OpenAI prototype.
- Perform continued `gpt-realtime` development on a dedicated branch rather than shared/default OHV integration work.
- Do not add another provider to the `realtime_action_spike` package.
- Do not add provider-selection conditionals to the OpenAI gateway or browser panel.
- Do not create a provider registry, provider dropdown, fallback chain, or mid-session provider switching.
- If another provider is prototyped, give it its own entry point, session implementation, browser/server transport, event handling, tests, and branch.
- Choose the provider-specific path before microphone capture and remote-session creation.

A future OHV source layout may contain sibling provider areas, for example:

```text
voice/
  openai_realtime/
    session
    transport
    events
    tools
    ui
  future_provider_realtime/
    session
    transport
    events
    tools
    ui
```

This illustrates isolation, not a request to restructure the current spike prematurely. There should be no `voice/base.py`, `voice/adapter.py`, or shared provider event hierarchy.

## Allowed and disallowed reuse

Allowed external dependencies:

- OHV authorization and policy services;
- local media, timer, system, and Hermes task services;
- logging and metrics sinks;
- general-purpose libraries that know nothing about realtime providers.

Disallowed cross-provider reuse:

- connection and authentication flows;
- audio transport and buffering;
- session lifecycle management;
- provider event parsing or normalization;
- transcript correlation;
- interruption and cancellation state machines;
- remote tool-call continuation;
- provider-specific UI state machines;
- tests that run multiple providers through one behavioral fixture.

Code should be duplicated rather than prematurely shared when the apparent similarity is provider-contract behavior. Genuine product services remain separate dependencies and do not belong inside either provider implementation.

## Current OpenAI lifecycle

```text
User explicitly starts OpenAI Realtime mode
  → browser captures microphone
  → loopback gateway creates /v1/realtime/calls session
  → WebRTC carries microphone and model audio
  → OpenAI data channel emits conversation and function-call events
  → OpenAI-specific handler validates and forwards an allowlisted request
  → local execution boundary authorizes and executes it
  → OpenAI-specific handler sends function_call_output
  → OpenAI-specific handler sends response.create
  → model acknowledges in the same conversation
  → Stop tears down tracks, data channel, peer connection, and local state
```

The user must enter this mode explicitly. Audio already sent to OpenAI cannot also be treated as though OHV had performed a local-first pre-route.

## Non-goals

- Gemini Live, Grok Voice, Nova Sonic, Azure Voice Live, or other provider support.
- An OpenAI-compatible provider abstraction.
- Runtime provider fallback or load balancing.
- A normalized realtime event schema.
- A shared reconnect/session-resumption mechanism.
- Production OHV migration as part of the current spike.
- Refactoring concrete OpenAI terminology for hypothetical reuse.

## Acceptance criteria

The architecture remains compliant when:

1. Only OpenAI `gpt-realtime` can be selected or started.
2. OpenAI session and event semantics are visible directly in code and diagnostics.
3. The browser and gateway contain no provider switch or generic provider interface.
4. Tool execution remains allowlisted, validated, local, and observable.
5. Provider-owned conversation logic cannot authorize its own side effects.
6. Continued work occurs on the dedicated `gpt-realtime` branch.
7. A future provider can be developed and removed without editing the OpenAI implementation or tests.
8. Tests exercise OpenAI behavior directly rather than asserting a lowest-common-denominator provider contract.

## Revisit conditions

Revisit this decision only after at least two provider-specific prototypes exist and have been exercised against real audio, interruption, transcript, reconnect, and tool workflows. Even then, extract only demonstrably identical product-level utilities; do not replace the parallel provider lifecycles with an adapter merely to reduce line count.
