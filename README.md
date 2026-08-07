# Okay Hermes Realtime

A hands-free voice assistant for Linux. Say the wake word, talk naturally to OpenAI Realtime over WebRTC, and hand complex requests to the full [Hermes Agent](https://github.com/NousResearch/hermes-agent) through a Kanban board, without blocking the conversation.

This project is an OpenAI-specific, independently installable successor to [Okay Hermes Voice (OHV)](https://github.com/H-Ali13381/okay-hermes-voice). It does not modify or reuse the OHV runtime: it has its own units, binaries, config, state, browser profile, and installer paths.

## What it does

- **Native wake word listener.** A small C listener captures the microphone through PipeWire and runs a local ONNX wake-word model. No cloud wake detection, no polling loop.
- **Scoped tool execution.** The model sees a small allowlist of functions and nothing else. The local controller validates every call: scope, function name, bounded JSON arguments, call-ID replay. Model-generated shell or code is never evaluated.
- **Heavy-agent handoff.** Complex requests (coding, filesystem, web lookups, automation, deep reasoning) become Hermes Kanban cards instead of failing inside the voice session. The voice reply is immediate; the heavy work runs asynchronously. A status tool reports what happened with the task.

## Runtime topology

```text
native wake listener ──activation socket──> local controller
       ▲                                      │
       │ pause/rearm                          ├─ scoped /execute broker
       │                                      └─ dedicated Brave app process
system tray                                         │
                                             WebRTC + oai-events
                                                    │
                                           OpenAI Realtime call
```

The controller launches a dedicated Brave app window in its own process group and profile. The page owns one direct WebRTC conversation with the Realtime API; the controller owns session scope, tool authorization, traces, and teardown. Wake detection is paused during a session and re-armed on teardown, including failure paths.

## Install

Runtime prerequisites: a Linux user systemd session, PipeWire/WirePlumber, Brave (configurable via `BRAVE_BIN`; the default resolves the packaged `brave-origin` command rather than a version-specific `/opt` path), Qt 6 development packages with CMake/Ninja for the tray, a wake-word ONNX model, ONNX Runtime, `uv`, and an OpenAI API key with Realtime access. Rebuilding the committed browser bundle additionally requires Node.js and npm.

```bash
git clone https://github.com/H-Ali13381/okay-hermes-realtime.git
cd okay-hermes-realtime

MODEL=/absolute/path/to/okay-hermes.onnx
ORT=/absolute/path/to/onnxruntime

bash scripts/install_user_services.sh \
  --model "$MODEL" \
  --onnxruntime-root "$ORT" \
  --enable

bash scripts/install_realtime_tray.sh

# Add OPENAI_API_KEY to the newly installed mode-0600 config:
$EDITOR ~/.config/okay-hermes-realtime/config.env
```

Do not run the old OHV wake listener and this one at the same time; record the old unit state before switching. The complete procedure, including smoke test, recovery, and rollback, is in [the install runbook](docs/runbooks/stage-1-install-and-smoke.md).

**Naming layers.** The repository and installed services are `okay-hermes-realtime`. The importable Python package is `realtime_action_spike` (a name kept from the original spike). Installed runtime files live under `~/.local/share/okay-hermes-realtime/`.

**First microphone permission.** The assistant uses an isolated Brave profile at `~/.local/share/okay-hermes-realtime/brave-profile`. The launcher auto-accepts the microphone request for its loopback-only page; if Brave still prompts, allow it once in that profile. Do not reuse a personal browser profile.

## Tools

| Capability | What it does | Execution |
|---|---|---|
| `assistant_get_current_time` | Current time, local or by IANA timezone | Local |
| `voice_end_session` | Ends the voice session when the user asks to stop | Local |
| `handoff_to_heavy_agent` | Queues a complex request as a Hermes Kanban card and dispatches a worker | Kanban |
| `check_heavy_agent_task` | Reports status and summary for a handed-off task, defaulting to the most recent | Kanban |
| `resolve_heavy_agent_block` | Records an explicit one-shot approval or denial for the exact blocked event | Kanban |

### Heavy-agent handoff

`handoff_to_heavy_agent` runs `hermes kanban create` with the request, acceptance criteria, and bounded timeouts, then optionally dispatches one worker. The voice session replies that the task is queued and continues; it does not block on the heavy agent. The controller then watches only that card in its resolved board database. Completion and human-input blocks are queued into the originating live voice session and spoken on the next safe model turn; user or assistant speech is never interrupted. If the original session has ended, voice delivery is dropped.

For `needs_input` and `capability` blocks, the assistant asks one bounded question. `resolve_heavy_agent_block` accepts only `approve_once` or `deny`, verifies the exact task, board, session, and `task_events` row, records the user's answer through `hermes kanban unblock --reason`, and lets normal dispatch resume the worker. Blanket approval and stale block responses are rejected.

Task state changes also use the desktop's native `org.freedesktop.Notifications` service through `notify-send`; Plasma owns presentation, history, quiet mode, and dismissal. Set `KDE_TASK_NOTIFICATIONS=false` to disable this secondary path. No custom popup stack is included.

Environment overrides (set in `~/.config/okay-hermes-realtime/config.env`):

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_KANBAN_BIN` / `HERMES_BIN` | `hermes` on PATH, then known install paths | Hermes executable |
| `HERMES_KANBAN_HEAVY_ASSIGNEE` | `default` | Kanban assignee profile |
| `HERMES_KANBAN_HEAVY_MODEL` / `HERMES_KANBAN_HEAVY_PROVIDER` | unset | Model override for the worker |
| `HERMES_KANBAN_HEAVY_MAX_RUNTIME` | `30m` | Worker runtime budget |
| `HERMES_KANBAN_CREATE_TIMEOUT_SECONDS` | `30` | Card creation timeout |
| `HERMES_KANBAN_DISPATCH_AFTER_CREATE` | `1` | Set to `0` to queue without dispatching |
| `KDE_TASK_NOTIFICATIONS` | `true` | Show secondary task events through Plasma's native notification service |

The controller resolves the Hermes binary through these overrides, PATH, and absolute fallback paths, because systemd units do not inherit the user shell PATH. Missing binary or timeout surfaces as a controlled tool error, never a gateway 500.

## Operation

```bash
# Start
systemctl --user start \
  okay-hermes-realtime-controller.service \
  okay-hermes-realtime-wakeword.service

# Health
curl http://127.0.0.1:8765/health
cat ~/.local/state/okay-hermes-realtime/controller-health
cat ~/.local/state/okay-hermes-realtime/capture-health

# Stop
systemctl --user stop \
  okay-hermes-realtime-wakeword.service \
  okay-hermes-realtime-controller.service
```

The tray performs the same ordered start/stop operations, and its icon tracks controller health, capture health, and unit state. During a session, the Stop button, tray Turn OFF, transport failure, and tool-requested close all use the same controller teardown path.

Session lifecycle is guarded end to end: the page confirms the provider's `session.updated` before reporting ready, the farewell on close locks VAD and waits for playback to finish with bounded fallbacks, and transport states (ICE, data channel, control socket close codes, page errors) are traced for diagnostics.

## Security and privacy

- `OPENAI_API_KEY` stays in the mode-0600 server config and never reaches browser JavaScript. The server posts the browser SDP to OpenAI; no provider credential enters the page.
- The gateway binds to loopback only. A bound local session receives a short-lived opaque execution scope, and the controller validates scope, function names, bounded arguments, and call-ID replay consistency on every `/execute`.
- Tool execution is allowlisted from a static contract. Model-generated shell or code is never evaluated.
- Wake detection runs entirely locally. During a conversation, microphone audio flows to OpenAI Realtime, as with any Realtime client. Session traces and health markers stay in `~/.local/state/okay-hermes-realtime/`.

## Installed paths

| Purpose | Path |
|---|---|
| Runtime config | `~/.config/okay-hermes-realtime/config.env` |
| User units | `~/.config/systemd/user/okay-hermes-realtime-*.service` |
| Python venv/package | `~/.local/share/okay-hermes-realtime/venv` |
| Wakeword model | `~/.local/share/okay-hermes-realtime/models/okay-hermes-realtime-wakeword.onnx` |
| Brave profile | `~/.local/share/okay-hermes-realtime/brave-profile` |
| Listener, ONNX libraries, tray | `~/.local/lib/okay-hermes-realtime/` |
| Tray autostart | `~/.config/autostart/okay-hermes-realtime-tray.desktop` |
| Health markers and traces | `~/.local/state/okay-hermes-realtime/` |
| Activation socket | `$XDG_RUNTIME_DIR/okay-hermes-realtime/activation.sock` |

The installer refuses collisions unless `--force` is explicit, and it records installation state for `--rollback` and `--uninstall`.

## Verification

```bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
npm ci && npm run check:web
bash scripts/build-package.sh
cmake -S native/realtime-tray -B /tmp/okay-hermes-realtime-tray-final -G Ninja
cmake --build /tmp/okay-hermes-realtime-tray-final
native/build_wake_listener.sh \
  --output /tmp/okay-hermes-realtime-wake-listener-final \
  --onnxruntime-root "$ORT"
systemd-analyze --user verify \
  systemd/okay-hermes-realtime-controller.service \
  systemd/okay-hermes-realtime-wakeword.service
```

The suite covers the capability contract, gateway authorization and replay rules, lifecycle races, teardown ordering, protocol schemas, installer behavior, and the native listener's capture pipeline.

### Resource footprint

One one-second live-session sample on the development machine:

| Component | PSS KiB | RSS KiB | CPU % |
|---|---:|---:|---:|
| Controller | 43,692 | 57,780 | 0.000 |
| Wake listener | 25,274 | 31,652 | 5.000 |
| Tray | 19,438 | 74,164 | 0.000 |
| Brave tree | 455,365 | 1,531,392 | 18.999 |
| Combined | 543,769 | 1,694,988 | 23.999 |

This is a single short sample, not a benchmark. Brave dominates residency, and CPU varies with connection and audio activity.

## Documentation

- [Install, smoke, recovery, and rollback runbook](docs/runbooks/stage-1-install-and-smoke.md)
- [Design notes](docs/design/)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Related projects

- [Okay Hermes Voice](https://github.com/H-Ali13381/okay-hermes-voice): the original voice assistant this project succeeds.
- [Hermes Agent](https://github.com/NousResearch/hermes-agent): the heavy agent that handles Kanban handoffs.

## Limitations

- OpenAI Realtime only. There is no provider abstraction and none is planned here.
- Single user, loopback-only.
- Requires Brave for the voice page and an ONNX wake-word model for activation.
- Handoff is asynchronous. Automatic voice delivery requires the originating session to remain open; ended sessions receive only the optional native desktop notification.
- The resource figures above are machine- and session-specific.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
