# Okay Hermes Realtime — Stage 1 replacement candidate

An OpenAI-specific, independently installable replacement candidate for Okay Hermes Voice (OHV). A native wake listener and tray launch a dedicated Brave app window; OpenAI's maintained `OpenAIRealtimeWebRTC` transport owns microphone, model audio, interruption, and response sequencing while the local controller owns session scope, sideband tools, authorization, traces, and teardown.

This branch does not modify or reuse the OHV runtime. It has separate units, binaries, config, state, browser profile, and installer paths.

## Runtime topology

```text
native wake listener ──activation socket──> local controller
       ▲                                      │
       │ pause/rearm                          ├─ OpenAI Realtime sideband
       │                                      ├─ allowlisted tool execution
replacement tray                              └─ dedicated Brave app process
                                                       │
                                                WebRTC mic + audio
                                                       │
                                              OpenAI Realtime call
```

The controller launches the real Brave binary in a new process group and enforces a bounded deadline for the page to reach `page_started`. The page first tears down media and acknowledges Stop; fallback TERM targets only the browser main PID. The systemd controller unit uses `KillMode=mixed`, reserving cgroup-wide KILL for a bounded failure fallback. This avoids Chromium/Crashpad SIGTRAP reports caused by terminating the whole browser tree simultaneously.

## Stage 1 scope

Implemented and exercised:

- local ONNX wakeword detection over PipeWire;
- native Qt tray with Turn ON, Turn OFF, Open Voice Page, status, and diagnostics;
- dedicated Brave app profile and real microphone capture;
- OpenAI `gpt-realtime-2.1-mini` WebRTC conversation;
- server-side OpenAI sideband connection bound from the SDK's authenticated call ID;
- controller-owned `assistant_get_current_time` and `voice_end_session` execution;
- SDK-owned interruption handling with passive browser/provider diagnostics;
- bounded, idempotent teardown and wake rearm;
- deterministic lifecycle, race, replay, failure, and installer tests.

This remains an OpenAI-only prototype. It is not a generic realtime-provider layer.

## Quick install

Runtime prerequisites: Linux user systemd, PipeWire/WirePlumber, Brave Origin Nightly, Qt 6 development packages, CMake/Ninja, a wakeword ONNX model, ONNX Runtime, `uv`, and an OpenAI API project with Realtime access. Rebuilding the committed browser bundle additionally requires Node.js and npm.

```bash
MODEL=/absolute/path/to/okay-hermes.onnx
ORT=/absolute/path/to/onnxruntime

bash scripts/install_user_services.sh \
  --model "$MODEL" \
  --onnxruntime-root "$ORT"

bash scripts/install_realtime_tray.sh

# Add OPENAI_API_KEY to the newly installed mode-0600 config:
$EDITOR ~/.config/okay-hermes-realtime/config.env

systemctl --user enable --now \
  okay-hermes-realtime-controller.service \
  okay-hermes-realtime-wakeword.service
```

Do not run the old OHV and replacement wake listeners together. Record the old unit state before switching. The complete procedure is in [docs/runbooks/stage-1-install-and-smoke.md](docs/runbooks/stage-1-install-and-smoke.md).

## First microphone permission

The replacement uses an isolated profile at:

```text
~/.local/share/okay-hermes-realtime/brave-profile
```

The launcher auto-accepts the microphone request for its loopback-only app page. If Brave still presents a permission prompt, allow it once in that dedicated profile. Do not reuse a personal browser profile.

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

The tray performs the same ordered start/stop operations. During a session, Stop, tray Turn OFF, transport failure, and tool-requested close all use the same controller teardown path.

## Replacement-owned paths

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

The installer refuses collisions unless `--force` is explicit and records replacement-owned installation state for rollback/uninstall.

## Security boundary

- `OPENAI_API_KEY` remains in the mode-0600 server config and never enters browser JavaScript.
- The page receives a short-lived OpenAI `ek_...` client secret and the SDK's call ID. The standard API key never enters browser JavaScript, and the call ID is sent only over the activation-authenticated loopback control socket.
- The controller validates session IDs, sideband events, function names, and bounded JSON arguments.
- Tool execution is allowlisted; model-generated shell/code is never evaluated.
- Late/stale events and changed-payload call-ID reuse are rejected.
- The gateway binds to loopback only.

## Interruption diagnostics

The current diagnostics include:

- SDK connection-state changes;
- response completion status and output types;
- structured Realtime error type, code, and bounded message;
- browser-observed time from speech start to provider cancellation, output-buffer clear, and next response audio in the visible diagnostics panel.

The application does not mute, pause, or manually resume model audio. Missing observations remain missing; the runtime does not substitute plausible zeroes or claim an audible-silence measurement it did not observe.

## Verification

```bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
npm ci
npm run check:web
bash scripts/build-package.sh
cmake -S native/realtime-tray -B /tmp/okay-hermes-realtime-tray-final -G Ninja
cmake --build /tmp/okay-hermes-realtime-tray-final
ORT=/absolute/path/to/onnxruntime
native/build_wake_listener.sh \
  --output /tmp/okay-hermes-realtime-wake-listener-final \
  --onnxruntime-root "$ORT"
systemd-analyze --user verify \
  systemd/okay-hermes-realtime-controller.service \
  systemd/okay-hermes-realtime-wakeword.service
git diff --check
```

## Verified smoke snapshot

On 2026-07-22, the replacement was exercised through the actual tray: Turn ON → Open Voice Page → OpenAI WebRTC `live` → Turn OFF. Both units stopped with `Result=success`; the dedicated Brave tree reached zero processes; no new Brave core dump was created.

One one-second live-session sample on the test machine reported:

| Component | PSS KiB | RSS KiB | CPU % |
|---|---:|---:|---:|
| Controller | 43,692 | 57,780 | 0.000 |
| Wake listener | 25,274 | 31,652 | 5.000 |
| Tray | 19,438 | 74,164 | 0.000 |
| Brave tree | 455,365 | 1,531,392 | 18.999 |
| Combined | 543,769 | 1,694,988 | 23.999 |

This is a single short sample, not a benchmark. Brave dominates residency, and CPU varies with connection/audio activity.

## Known limitations / Stage 2 deferrals

- Only OpenAI Realtime is supported; no provider abstraction or fallback is planned here.
- Real side effects beyond the local clock and session close remain deferred.
- Durable Hermes foreground/background task IDs, cancellation/status UI, pending-result reinjection, and long-term session archives are Stage 2 work.
- Real media service integration (Spotify/volume), timers, and richer local actions are Stage 2 work.
- A persistent hidden WebRTC worker is only a future measurement candidate; Stage 1 opens a visible app window per activation.
- The runtime is single-user and loopback-only.
- The resource figures above are machine/session-specific.

## Design and operations

- [Accepted Stage 1 design](docs/design/2026-07-21-webrtc-wakeword-replacement.md)
- [Implementation plan](docs/plans/2026-07-21-webrtc-wakeword-replacement-stage-1.md)
- [Install, smoke, recovery, and rollback runbook](docs/runbooks/stage-1-install-and-smoke.md)
