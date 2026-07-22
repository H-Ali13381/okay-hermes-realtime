# OpenAI Realtime Voice Replacement — Stage 1 status

Status is synchronized to `feature/webrtc-wakeword-replacement` on 2026-07-22. A checked item is implemented and verified on this branch.

## Completed and integrated

- [x] **Task 6 — Dedicated Brave window**
  - Isolated app profile, actual Brave executable ownership, bounded main-PID TERM and owned-group KILL fallback.
- [x] **Task 7 — Native activation bridge**
  - Native listener activation socket, one active session, wake pause/rearm.
- [x] **Task 8 — Preserve the OpenAI call handle**
  - Exact local/provider session binding without exposing provider identifiers to the page.
- [x] **Task 9 — OpenAI sideband connection**
  - Server-owned sideband events, stale-session rejection, concurrent-shutdown cleanup.
- [x] **Task 10 — Move tool execution to sideband**
  - Bounded parser, controller-owned allowlisted execution, canonical deduplication, resumable output delivery, safe page action state.
- [x] **Task 11 — Interruption timeline**
  - Speech, suppression, cancellation, truncation, listening restoration, and next-response timing.
- [x] **Task 12 — Residual playback suppression**
  - Immediate local suppression with response-scoped restoration.
- [x] **Task 13 — Unified teardown**
  - Button, transport, timeout, cancellation, tool-close, service shutdown, and error exits use one bounded idempotent path.
- [x] **Task 14 — Wakeword listener integration**
  - PipeWire/ONNX native listener adapted to replacement-owned paths.
- [x] **Task 15 — Installer and user services**
  - Collision-safe isolated install, rollback/uninstall, venv/native assets, `KillMode=mixed` controller shutdown.
- [x] **Task 16 — Tray integration**
  - Native Qt tray state, health, open, Turn ON/OFF, and diagnostics.
- [x] **Task 17 — Lifecycle integration tests**
  - Activation, launch, WebRTC-control setup, teardown/rearm, races, stale/replayed scopes, repeated cleanup, and failure paths.
- [x] **Task 18 — Resource and latency measurements**
  - Explicit-PID RSS/PSS/CPU sampling and persisted trace latency extraction without silent zero substitution.
- [x] **Task 19 — Real acceptance smoke**
  - Actual tray Turn ON → Open Voice Page → OpenAI WebRTC `live` → Turn OFF.
  - Microphone capture and dedicated Brave profile verified.
  - Both replacement units stopped with `Result=success`.
  - Dedicated Brave tree reached zero processes with no new core dump.
  - Legacy OHV tracked repository state remained unchanged.

## Task 20 — Final documentation and verification

- [x] Replace the obsolete Streamlit README with the Stage 1 runtime, security, operation, paths, measurements, and limitations.
- [x] Add `docs/runbooks/stage-1-install-and-smoke.md` with install, permission, safe OHV switching, diagnostics, recovery, rollback, and uninstall.
- [x] Record verified implementation deltas in the accepted design.
- [x] Verify runtime/install/native paths contain no legacy OHV installed-name collisions.
- [x] Run Python, Node, lint, compile, wheel/sdist, native tray, native listener, systemd, and diff gates.
- [x] Complete independent full-diff review and address all blocking findings.
- [x] Pass the temporary local-`main` merge verification gate.

## Verified Stage 1 snapshot

A one-second live sample reported:

- combined PSS: 543,769 KiB;
- combined RSS: 1,694,988 KiB;
- combined CPU: 23.999%;
- Brave-tree PSS: 455,365 KiB.

These are one-machine smoke values, not a benchmark.

The old `hermes-wakeword.service` was enabled but inactive during the final smoke. The OHV repository had no tracked diff from this work; its pre-existing untracked `.superpowers/` directory remained untouched.

## Explicit Stage 2 backlog

Not part of Stage 1:

- real timers and local media execution;
- durable Hermes foreground/background task IDs;
- pending-result persistence and later-wake reinjection;
- task cancellation/status UI;
- long-term session archives;
- hidden/persistent WebRTC worker comparison;
- native WebSocket audio, unless WebRTC later fails measured interruption/lifecycle requirements;
- any shared realtime-provider abstraction.
