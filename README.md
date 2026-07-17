# OpenAI Realtime Action Spike

A local feasibility test for one concrete question: can OpenAI Realtime hold a natural full-duplex voice conversation while reliably emitting typed execution requests that a local assistant controls?

This is a private spike, not an OHV production integration. Tool side effects are simulated except for reading the current time.

## What it contains

- Streamlit page with one-tap persistent voice conversation
- browser WebRTC microphone and model audio
- server-side OpenAI Realtime session creation
- six typed assistant capabilities
- local allowlisted execution broker
- live execution-request and Realtime-event inspector
- clean Stop behavior for microphone, data channel, and peer connection

The default model is `gpt-realtime-2.1-mini` with minimal reasoning, `marin`, audio output, semantic VAD with high eagerness, automatic responses, and interruption enabled.

## Setup

```bash
cd /home/user/Documents/SideProjects/Private/openai-realtime-action-spike
cp .env.example .env
# Add a standard OpenAI API key to OPENAI_API_KEY in .env.
uv sync --all-groups
uv run python scripts/run.py
```

Open `http://127.0.0.1:8501`, press **Start conversation**, allow microphone access once, and speak normally. The session remains active until Stop, a failed connection, or a `voice_end_session` action completes.

A ChatGPT/Codex subscription token is not a standard OpenAI API key. The Realtime API must be enabled for the API project, and usage is billed to that project.

## Useful test phrases

Conversation without a tool:

- “Why do CRT televisions need a flyback transformer?”
- Interrupt the answer with: “Actually, explain only the high-voltage path.”

Typed local or simulated actions:

- “What time is it in Tokyo?”
- “Set a five-minute timer called tea.”
- “Play Daft Punk.”
- “Pause the music.”
- “Set the volume to thirty percent.”
- “Research powered HDMI to composite converters for me.”
- “End this conversation.”

The right-hand inspector shows the model's function name and arguments before displaying the local broker result. Simulated actions are deliberately reported as simulated.

## Capability contract

| Function | Execution in this spike |
|---|---|
| `assistant_get_current_time` | Real local clock read |
| `assistant_start_timer` | Simulated |
| `media_play` | Simulated |
| `media_control` | Simulated |
| `voice_end_session` | Simulated result; browser then disconnects |
| `agent_delegate_task` | Simulated durable-task acceptance |

The initial list comes from the existing OHV destination-router and capability-catalog work: time/timers, session control, media control, and deeper-agent handoff.

## Security boundary

The browser sends its SDP offer only to the loopback FastAPI gateway. The gateway combines that offer with the server-owned model, prompt, reasoning, VAD, voice, and tool configuration before calling OpenAI's unified Realtime endpoint.

The permanent API key:

- is read from `.env` or the gateway environment;
- never appears in the Streamlit HTML or browser JavaScript;
- never appears in tool schemas or execution results;
- is redacted from upstream error responses.

The execution broker rejects unknown capabilities, malformed JSON, extra fields, invalid enums, and bounded-value violations. It never evaluates model-generated code or shell commands.

## Architecture

```text
Browser / Streamlit iframe
  ├─ microphone + speaker over WebRTC
  ├─ Realtime data-channel events
  └─ execution inspector
          │
          ├─ SDP → loopback FastAPI → OpenAI Realtime
          └─ function call → allowlisted broker
                                ├─ local clock
                                └─ simulated side effects
```

## Verification

```bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
```

Health endpoints while running:

```text
http://127.0.0.1:8765/health
http://127.0.0.1:8501/_stcore/health
```

## Known limitations

- OpenAI performs intent recognition and chooses the tool in this Realtime mode.
- Tool selection is probabilistic; the inspector is intended to make failures visible.
- Side effects are mocks, so this does not yet prove Spotify, timers, or Hermes integration.
- The Streamlit UI is local HTTP. Remote microphone use requires trusted HTTPS termination.
- Browser iframe microphone policy still needs live verification in the target browser.
- This is a single-user loopback spike with no authentication or multi-session task registry.

## Source material

- OpenAI Realtime WebRTC: https://developers.openai.com/api/docs/guides/realtime-webrtc
- Realtime conversation and function calling: https://developers.openai.com/api/docs/guides/realtime-conversations
- Realtime VAD: https://developers.openai.com/api/docs/guides/realtime-vad
- Realtime model prompting: https://developers.openai.com/api/docs/guides/realtime-models-prompting
- Local OHV contract: `/home/user/wiki/artifacts/html-docs/drafts/2026-06-14-ohv-router-label-contract.md`
