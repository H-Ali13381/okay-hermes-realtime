# Stage 1 install, smoke, recovery, and rollback

This runbook operates only the independently named Okay Hermes Realtime replacement. It must not edit the existing OHV repository, `~/.hermes/wakeword/`, or legacy OHV units.

## 1. Prerequisites

Required:

- user systemd;
- PipeWire and WirePlumber;
- Brave Origin Nightly actual executable at `/opt/brave.com/brave-origin-nightly/brave` (or set `BRAVE_BIN` to another real Chromium executable, not a retained shell wrapper);
- CMake, Ninja, a C compiler, Qt 6 Widgets/DBus, and PulseAudioQt for the tray;
- `uv` or Python 3 for the isolated venv;
- a wakeword ONNX model;
- an ONNX Runtime tree containing headers and `lib/libonnxruntime.so*`;
- a standard `OPENAI_API_KEY` with Realtime API access.

A ChatGPT/Codex subscription token is not an API project key.

## 2. Record legacy OHV state

Never run both wake listeners simultaneously.

```bash
OLD_ACTIVE=$(systemctl --user is-active hermes-wakeword.service || true)
OLD_ENABLED=$(systemctl --user is-enabled hermes-wakeword.service || true)
printf 'legacy active=%s enabled=%s\n' "$OLD_ACTIVE" "$OLD_ENABLED"

systemctl --user stop hermes-wakeword.service
```

Record those two values outside the repository if this is more than a short smoke. Do not edit or disable the old unit merely to test the replacement.

## 3. Install replacement services

Use absolute source paths:

```bash
MODEL=/absolute/path/to/okay-hermes.onnx
ORT=/absolute/path/to/onnxruntime

bash scripts/install_user_services.sh \
  --model "$MODEL" \
  --onnxruntime-root "$ORT"
```

To install an already-built listener instead of rebuilding it:

```bash
bash scripts/install_user_services.sh \
  --model "$MODEL" \
  --listener /absolute/path/to/okay-hermes-realtime-wake-listener \
  --onnxruntime-root "$ORT"
```

The installer is collision-safe by default. Use `--force` only after reviewing the target paths. `--skip-systemctl` is for isolated installer tests, not normal operation.

Install the tray:

```bash
bash scripts/install_realtime_tray.sh
```

This installs:

- `~/.local/lib/okay-hermes-realtime/okay-hermes-realtime-tray`;
- `~/.config/autostart/okay-hermes-realtime-tray.desktop`.

Log out/in to use autostart, or launch the installed tray directly for the current desktop session.

## 4. Configure

Edit the newly installed mode-0600 file:

```bash
$EDITOR ~/.config/okay-hermes-realtime/config.env
```

Set at least:

```dotenv
OPENAI_API_KEY=...
BRAVE_BIN=/opt/brave.com/brave-origin-nightly/brave
```

Keep these replacement defaults unless intentionally relocating the install:

```dotenv
VOICE_BROWSER_PROFILE=~/.local/share/okay-hermes-realtime/brave-profile
VOICE_PAGE_URL=http://127.0.0.1:8765/voice
WAKEWORD_MODEL_PATH=~/.local/share/okay-hermes-realtime/models/okay-hermes-realtime-wakeword.onnx
```

## 5. Start and inspect

```bash
systemctl --user enable --now \
  okay-hermes-realtime-controller.service \
  okay-hermes-realtime-wakeword.service

systemctl --user status \
  okay-hermes-realtime-controller.service \
  okay-hermes-realtime-wakeword.service --no-pager

curl -fsS http://127.0.0.1:8765/health
cat ~/.local/state/okay-hermes-realtime/controller-health
cat ~/.local/state/okay-hermes-realtime/capture-health
```

Expected idle markers:

- controller: `ready`;
- capture: `healthy`;
- health endpoint: `status=ok`, API key configured, controller `idle`.

The tray should show the replacement as ON only after controller and microphone health are ready.

## 6. First-run microphone permission

Use the tray's **Open Voice Page** action once. The app page uses the dedicated profile:

```text
~/.local/share/okay-hermes-realtime/brave-profile
```

The launcher uses `--use-fake-ui-for-media-stream` for this isolated loopback app. If Brave still prompts, allow microphone access once. Verify the page reports one live audio input track and the health endpoint reaches `controller_status=live`.

Do not grant permission in or point the replacement at a personal Brave profile.

## 7. Real acceptance smoke

1. Confirm legacy OHV is stopped.
2. Use the replacement tray **Turn ON**.
3. Say “Okay Hermes” or use **Open Voice Page** to isolate browser/WebRTC testing.
4. Wait for `controller_status=live`.
5. Speak and hear one response.
6. Interrupt an audible response and confirm playback stops promptly while the microphone session remains active.
7. Exercise “What time is it?” and “End this conversation.”
8. Start another session to prove wake rearm and stale-session rejection.
9. With the voice page live, use tray **Turn OFF**.
10. Verify clean shutdown:

```bash
systemctl --user show okay-hermes-realtime-controller.service -p Result
systemctl --user show okay-hermes-realtime-wakeword.service -p Result

ps -eo args= | grep '[u]ser-data-dir=.*/okay-hermes-realtime/brave-profile' || true
coredumpctl --no-pager --since '5 minutes ago' list | grep brave || true
journalctl --user -u okay-hermes-realtime-controller.service --since '5 minutes ago' --no-pager \
  | grep -Ei 'sideband|provider_call_id|reattach' || true
```

Expected:

- both `Result=success`;
- no replacement-profile Brave process;
- no new Brave core dump;
- no sideband attachment, provider call-ID relay, or sideband reattach marker;
- a final `teardown_complete` event in the session trace;
- wakeword can be turned ON and activated again.

Traces are written under:

```text
~/.local/state/okay-hermes-realtime/traces/
```

## 8. Interruption diagnostics

An interruption trace is scoped to one local session and provider response. Fields may include:

- `response_id`;
- `user_speech_onset_ms`;
- `speech_started_received_ns`;
- `provider_audio_start_ms`;
- `playback_suppressed_ns`;
- `response_cancelled_ns`;
- `truncation_observed_ns`;
- `listening_restored_ns`;
- `next_response_first_audio_ns`;
- derived `speech_start_to_audible_silence_ms`.

A missing observation remains null/missing. Do not treat absent timing as zero latency.

## 9. Resource measurement

Capture tracked PIDs only:

```bash
CONTROLLER=$(systemctl --user show okay-hermes-realtime-controller.service -p MainPID --value)
LISTENER=$(systemctl --user show okay-hermes-realtime-wakeword.service -p MainPID --value)
TRAY=$(pgrep -n -f '/okay-hermes-realtime-tray$')
BRAVE=$(ps -eo pid=,args= | \
  grep '/opt/brave.com/brave-origin-nightly/brave --user-data-dir=.*/okay-hermes-realtime/brave-profile' | \
  grep -v -- '--type=' | awk 'NR == 1 {print $1}')

uv run python scripts/measure_runtime.py \
  --controller-pid "$CONTROLLER" \
  --listener-pid "$LISTENER" \
  --tray-pid "$TRAY" \
  --brave-pid "$BRAVE" \
  --sample-seconds 1 \
  --output /tmp/okay-hermes-runtime.json
```

If supplying `--trace`, use a trace containing only events supported by the measurement schema. The script reports missing/vanished processes as errors rather than silently substituting zero.

## 10. Recovery

### Controller or listener will not start

```bash
journalctl --user \
  -u okay-hermes-realtime-controller.service \
  -u okay-hermes-realtime-wakeword.service \
  -n 200 --no-pager

systemctl --user reset-failed \
  okay-hermes-realtime-controller.service \
  okay-hermes-realtime-wakeword.service
```

Check API key, model path, actual Brave executable, ONNX shared libraries, PipeWire, and health-marker permissions before restarting.

### Browser remains after Stop

Do not use broad `pkill brave`. Identify only the replacement profile/process group. The controller normally performs page cleanup, main-PID TERM, then owned-group KILL. A broad Chromium group TERM can create a Crashpad SIGTRAP report.

Confirm the installed controller unit contains:

```ini
KillMode=mixed
TimeoutStopSec=10s
```

Then reload after any unit repair:

```bash
systemctl --user daemon-reload
```

### Tray state is stale

Restart only the replacement tray process. Unit state and health markers are authoritative; the tray does not own session data.

## 11. Stop, rollback, and uninstall

Stop replacement units first:

```bash
systemctl --user stop \
  okay-hermes-realtime-wakeword.service \
  okay-hermes-realtime-controller.service
```

Rollback an interrupted/tracked installation:

```bash
bash scripts/install_user_services.sh --rollback
```

Uninstall replacement service-owned files:

```bash
bash scripts/install_user_services.sh --uninstall
```

The tray installer has no uninstall flag. Remove only its replacement-owned files:

```bash
rm -f ~/.config/autostart/okay-hermes-realtime-tray.desktop
rm -f ~/.local/lib/okay-hermes-realtime/okay-hermes-realtime-tray
rmdir --ignore-fail-on-non-empty ~/.local/lib/okay-hermes-realtime || true
```

Do not remove:

- `~/.hermes/wakeword/`;
- `hermes-wakeword.service`;
- the OHV repository or its config/state.

Restore the old listener according to the state recorded in step 2:

```bash
# Only if it was active before the smoke:
systemctl --user start hermes-wakeword.service

# Preserve its prior enabled/disabled state; do not guess.
```

## 12. Verified Stage 1 snapshot

On 2026-07-22, the actual tray path completed Turn ON → Open Voice Page → WebRTC `live` → Turn OFF. Both replacement units ended with `Result=success`, the dedicated Brave tree reached zero processes, and no new Brave core appeared.

A one-second live sample reported 543,769 KiB combined PSS, 1,694,988 KiB combined RSS, and 23.999% combined CPU. Brave accounted for 455,365 KiB PSS. Treat these as one-machine smoke values, not a benchmark.

The legacy `hermes-wakeword.service` was enabled but inactive during the final smoke. The OHV repository had no tracked diff from this work; a pre-existing untracked `.superpowers/` directory remained untouched.

## 13. Known limitations / explicit Stage 2 deferrals

Stage 1 does not prove:

- provider-agnostic realtime transport;
- real Spotify/volume/timer side effects;
- durable Hermes foreground/background task IDs and cancellation UI;
- pending-result persistence/reinjection across later wakes;
- long-term session archives;
- a hidden persistent WebRTC worker;
- multi-user or remote authenticated operation.

These require a separate Stage 2 plan and evidence. Do not prebuild shared provider abstractions in this branch.
