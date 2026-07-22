# OpenAI Realtime Voice Replacement — Remaining Tasks

Checkboxes mean the task is integrated and verified on
`feature/webrtc-wakeword-replacement`. Partially implemented work remains unchecked.

## Completed and Integrated

- [x] **Task 6 — Dedicated Brave window**
  - [x] Launch Brave in app mode with an isolated profile.
  - [x] Track and close only the process group created for the voice window.

- [x] **Task 7 — Native activation bridge**
  - [x] Connect the native wakeword listener to the Python controller.
  - [x] Block new wake detections until the active session closes.

- [x] **Task 8 — Preserve the OpenAI call handle**
  - [x] Capture the upstream Realtime `call_id` securely.
  - [x] Keep provider identifiers out of browser-visible messages.

- [x] **Task 9 — OpenAI sideband connection**
  - [x] Join the active Realtime call from the backend.
  - [x] Reject stale-session events and treat sideband loss as terminal.
  - [x] Close sockets established concurrently with shutdown.

- [x] **Task 11 — Interruption timeline**
  - [x] Track speech-start, cancellation, cutoff, and playback events.

- [x] **Task 12 — Residual playback suppression**
  - [x] Stop stale assistant audio immediately after interruption.
  - [x] Expose sanitized interruption timing data.

- [x] **Task 14 — Wakeword listener integration**
  - [x] Adapt the existing PipeWire/ONNX native listener for the replacement.

- [x] **Task 15 — Installer and user services**
  - [x] Install runtime dependencies and assets into replacement-owned paths.
  - [x] Add collision-safe systemd user services.
  - [x] Verify isolated venv install/import/entrypoint/uninstall behavior.

- [x] **Task 16 — Tray integration**
  - [x] Wire the replacement into the Qt tray icon.
  - [x] Add status, open, stop, and diagnostic actions.

- [x] **Task 18 — Resource and latency measurements**
  - [x] Measure PSS/RSS and CPU usage.
  - [x] Measure wake-to-page, connection, interruption, and teardown latency.

## In Progress

- [ ] **Task 10 — Move tool execution to sideband**
  - [x] Bind the OpenAI call handle to the exact local controller session.
  - [x] Add a bounded function-call event parser.
  - [x] Add controller-owned execution, canonical deduplication, and resumable output delivery.
  - [x] Add sanitized controller-to-page action-state messages.
  - [ ] Finish and verify parser/executor integration in `VoiceSessionController`.
  - [ ] Commit and integrate the Stage-1 catalog/browser de-execution slice.
  - [ ] Remove the `/execute` endpoint and obsolete gateway execution tests.
  - [ ] Run the complete Task 10 and repository verification gates.

## Remaining

- [ ] **Task 13 — Unified teardown**
  - [ ] Route button, transport, timeout, cancellation, tool-requested close, and error exits through one cleanup path.
  - [ ] Make shutdown bounded and idempotent.

- [ ] **Task 17 — Lifecycle integration tests**
  - [ ] Test activation through browser launch, WebRTC, sideband, and teardown.
  - [ ] Cover races, stale sessions, replayed tokens, and repeated cleanup.

- [ ] **Task 19 — Real acceptance test**
  - [x] Locate the wakeword model, API-key source, Brave binary, microphone, and reusable ONNX Runtime library.
  - [ ] Build and self-test the replacement listener against the located runtime.
  - [ ] Test actual wakeword activation.
  - [ ] Test microphone permission and OpenAI WebRTC.
  - [ ] Test interruption behavior and complete cleanup.
  - [ ] Confirm the original OHV remains untouched and recoverable.

- [ ] **Task 20 — Final documentation and verification**
  - [ ] Document installation, permissions, operation, recovery, and rollback.
  - [ ] Synchronize task status with final integrated history.
  - [ ] Run the final verification gate.

## Cleanup Debt

- [ ] Restore `feature/gpt-realtime-prototype` after parser commit `92e0375` was accidentally made there by a delegated worker. The parser files were already salvaged into the Task 10 branch.
- [ ] Verify and commit the currently modified Task 10 controller/tool-loop files.
- [ ] Verify and commit the currently modified browser/catalog slice.
- [ ] Integrate Task 10 into `feature/webrtc-wakeword-replacement` only after its full gate passes.

## Critical Path

1. Finish Task 10.
2. Implement Task 13.
3. Add Task 17 integration coverage.
4. Run Task 19 on real hardware and OpenAI WebRTC.
5. Complete Task 20 documentation and final verification.

## Shortest Path to Tray-Based Testing

- [x] Task 6 — Dedicated Brave window
- [x] Task 7 — Native activation bridge
- [x] Task 14 — Wakeword listener integration
- [x] Task 15 — Service/installation work
- [x] Task 16 — Tray integration
- [ ] Required Task 13 teardown work
- [ ] Task 19 real acceptance smoke
