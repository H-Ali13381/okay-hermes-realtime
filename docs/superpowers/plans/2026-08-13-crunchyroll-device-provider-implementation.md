# Crunchyroll Device Provider Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Replace the deployed Crunchyroll LAN remote-keyboard login with official device authorization, native exact episode resolution, and official DASH/Widevine playback in a dedicated Brave shell.

**Architecture:** Add a new `crunchyroll` provider package with strict auth, credential, catalog, resolver, and playback boundaries. Extend existing `PendingMediaCatalog` and exact-session control messages rather than creating a second confirmation system. Keep the currently deployed pairing path operational during isolated development; switch production atomically only after real auth, catalog, Widevine, and main-flow gates pass.

**Tech stack:** Python 3.12, FastAPI/httpx/Pydantic, Freedesktop Secret Service via `secret-tool`, existing QR generation, Brave Origin Chromium EME/Widevine, Shaka Player vendored into the wheel, pytest/pytest-asyncio, Node test runner/esbuild.

**Canonical design:** `docs/superpowers/specs/2026-08-13-crunchyroll-device-provider-design.md`

---

## Execution rules

- Work in `feature/crunchyroll-device-provider` under the repository's ignored `.worktrees/` directory.
- Use RED → GREEN → REFACTOR for every behavior change. Record the expected failing assertion/import before production edits.
- Never print or commit a real user code, device code, access token, refresh token, account ID, stream token, authorization header, manifest URL, or license payload.
- The Android TV client identity and private endpoint definitions are isolated in one transport module and treated as brittle compatibility data.
- No production switch, old-path deletion, UFW removal, or live deployment occurs before Gates 1–4 pass.
- Live tests are serialized and opt-in. Every playback attempt registers cleanup before asserting playback.

## Dependency order

```text
1 contracts
  ├── 2 Secret Service store
  ├── 3 device auth client
  └── 4 catalog/resolver
           └── 5 provider continuation + UI
                    └── 6 Brave EME feasibility player
                             └── 7 controller/service integration
                                      └── 8 live gates
                                               └── 9 atomic retirement/deploy
```

### Task 1: Add Crunchyroll media and control contracts

**Objective:** Represent Crunchyroll proposals and authorization state without exposing private IDs or secrets.

**Files:**
- Modify: `src/realtime_action_spike/media_catalog.py`
- Modify: `src/realtime_action_spike/runtime/protocol.py`
- Modify: `frontend/voice.js`
- Generated: `src/realtime_action_spike/web/voice.js`
- Test: `tests/test_media_catalog.py`
- Test: `tests/runtime/test_protocol.py`
- Test: `tests/web/media-confirmation.test.mjs`

**Steps:**

1. Add RED tests for `CrunchyrollPlaybackLocator(series_id, season_id, episode_id, content_id, audio_locale, subtitle_locale)`, `PendingMedia(source="crunchyroll")`, display projection secrecy, Crunchyroll thumbnail allowlists, and rejection of token/URL-shaped locator fields.
2. Run:
   ```bash
   uv run pytest tests/test_media_catalog.py tests/runtime/test_protocol.py -q -k crunchyroll
   npm run test:web -- --test-name-pattern=Crunchyroll
   ```
   Expected: FAIL because the source, locator, protocol message, and parser do not exist.
3. Extend `SourceKind`, `PlaybackLocator`, `PendingMedia` validation, and `MediaConfirmationMessage.source` with `crunchyroll`.
4. Add a strict controller-to-page `CrunchyrollAuthorizationMessage` containing only session ID, public flow ID, verification URL, user code, and expiry seconds. Add URL validation for exact `https://www.crunchyroll.com/activate` and ensure secret-key names remain forbidden.
5. Extend the source badge and thumbnail allowlist in `frontend/voice.js`; rebuild the checked-in bundle.
6. Run focused tests plus `npm run check:web`; expected: PASS.
7. Commit: `feat: add Crunchyroll provider contracts`.

### Task 2: Add a fail-closed Secret Service credential store

**Objective:** Persist refresh credentials without argv, environment, plaintext-file, or homemade-encryption exposure.

**Files:**
- Create: `src/realtime_action_spike/crunchyroll/credentials.py`
- Create: `src/realtime_action_spike/crunchyroll/__init__.py`
- Create: `tests/test_crunchyroll_credentials.py`
- Modify: `pyproject.toml` only if an in-process Secret Service dependency is justified by tests; otherwise use `/usr/bin/secret-tool` with stdin.

**Steps:**

1. Add RED tests for `load`, `store`, `delete`, unavailable service, nonzero exit, empty output, bounded token length, and secret absence from command arguments/environment/error strings.
2. Run `uv run pytest tests/test_crunchyroll_credentials.py -q`; expected: import failure.
3. Implement an injected async subprocess runner. For `secret-tool store`, pass the token only through stdin. Use fixed schema attributes such as service `okay-hermes-realtime`, provider `crunchyroll`, kind `refresh-token`; pass no token in argv/environment.
4. Store the stable device identifier as a separate Secret Service item. Validate it as a UUID before use.
5. Map all backend failures to `CrunchyrollCredentialError` with non-sensitive messages. Do not add a file fallback.
6. Run focused tests and `uv run ruff check`; expected: PASS.
7. Commit: `feat: store Crunchyroll authorization in Secret Service`.

### Task 3: Implement strict device authorization and refresh

**Objective:** Request and poll Crunchyroll device authorization, keep access tokens memory-only, and recover from revoked refresh tokens.

**Files:**
- Create: `src/realtime_action_spike/crunchyroll/models.py`
- Create: `src/realtime_action_spike/crunchyroll/transport.py`
- Create: `src/realtime_action_spike/crunchyroll/auth.py`
- Create: `tests/test_crunchyroll_auth.py`
- Create: `tests/fixtures/crunchyroll/device_code.json`
- Create: `tests/fixtures/crunchyroll/token_success.json`

**Steps:**

1. Add sanitized response fixtures containing obvious fake values.
2. Add RED tests for strict device-code shape, exact activation URL, expiry/interval bounds, ordered polling, `authorization_pending`, slowdown, denied, expired, success, refresh rotation, 60-second access-token margin, revoked refresh deletion, cancellation, and redacted failures.
3. Run `uv run pytest tests/test_crunchyroll_auth.py -q`; expected: import failure.
4. Implement one `httpx.AsyncClient` transport with exact endpoint/origin constants, bounded timeouts, no redirect following across origins, fixed device client identity, and response-size limits.
5. Implement `DeviceAuthClient` state transitions. Poll at the server interval; cap backoff at 30 seconds and total lifetime at the server expiry/300 seconds.
6. Keep `AccessCredential` only in memory. Rotate persisted refresh token only after a complete validated token response.
7. Add a central response/error redactor and test that fake tokens never appear in exceptions or log records.
8. Run focused tests and ruff; expected: PASS.
9. Commit: `feat: implement Crunchyroll device authorization`.

### Task 4: Implement catalog contracts and exact resolver

**Objective:** Resolve spoken title/season/episode/language constraints into one stable official episode or a bounded typed alternative.

**Files:**
- Create: `src/realtime_action_spike/crunchyroll/catalog.py`
- Create: `src/realtime_action_spike/crunchyroll/resolver.py`
- Create: `tests/test_crunchyroll_catalog.py`
- Create: `tests/test_crunchyroll_resolver.py`
- Create: sanitized fixtures under `tests/fixtures/crunchyroll/`

**Steps:**

1. Add RED tests for strict index/search/season/episode response parsing, URL/image origin allowlists, response bounds, malformed upstream data, exact match, aliases/punctuation normalization, ambiguity (maximum five), explicit episode, unavailable episode, audio locale, subtitle locale, and no silent guess.
2. Run focused tests; expected: import failure.
3. Implement typed DTOs and endpoint methods for index, search, seasons, and episodes. Keep raw JSON inside the catalog boundary.
4. Implement deterministic resolver scoring with exact normalized title identity as the only auto-selection tier; otherwise return `ambiguous`.
5. Return `ResolvedCrunchyrollEpisode`, `AmbiguousCrunchyrollResult`, or `UnavailableCrunchyrollResult`; do not return raw dictionaries.
6. Build `PendingMedia` only in the controller/provider layer, never inside HTTP transport.
7. Run focused tests and ruff; expected: PASS.
8. Commit: `feat: resolve exact Crunchyroll episodes`.

### Task 5: Add authorization continuation and activation UI

**Objective:** Let an auth-required preparation return immediately, show official activation details, and resume the exact structured request after authorization succeeds.

**Files:**
- Create: `src/realtime_action_spike/crunchyroll/provider.py`
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `src/realtime_action_spike/runtime/protocol.py`
- Modify: `frontend/voice.js`
- Modify: `frontend/voice.css`
- Generated: `src/realtime_action_spike/web/voice.js`
- Test: `tests/test_crunchyroll_provider.py`
- Test: `tests/runtime/test_controller.py`
- Test: `tests/web/*.test.mjs`

**Steps:**

1. Add RED tests for one auth task per voice session, exact structured continuation, immediate `authentication_required`, activation message projection, success resumption into confirmation, cancellation/expiry, stale-session suppression, task replacement, teardown cancellation, and no model reconstruction.
2. Run focused Python/web tests; expected: FAIL.
3. Implement `CrunchyrollProvider.prepare(...)` returning typed provider outcomes and owning one bounded authorization task. Inject a callback that publishes the resumed exact episode through the controller only if the original session remains active.
4. Add an activation panel to the existing voice page with provider URL, user code, countdown, and locally generated QR data supplied as display-safe content. Do not host a phone application.
5. Clear the activation surface on success, cancel, expiry, teardown, or supersession.
6. Ensure `prepare_crunchyroll_media` creates the same `PendingMediaCatalog` proposal as other sources and supports exact revision without unofficial fallback.
7. Run focused tests, web checks, then combined controller/gateway tests; expected: PASS.
8. Commit: `feat: resume Crunchyroll requests after device authorization`.

### Task 6: Prove and implement the Brave EME/Widevine player backend

**Objective:** Play one confirmed official encrypted Crunchyroll stream through Chromium Widevine without exposing tokens to browser JavaScript.

**Files:**
- Create: `src/realtime_action_spike/crunchyroll/playback.py`
- Create: `src/realtime_action_spike/crunchyroll/player_server.py`
- Create: `frontend/crunchyroll-player.js`
- Create: `frontend/crunchyroll-player.css`
- Create: bundled assets under `src/realtime_action_spike/crunchyroll/player_assets/`
- Modify: `package.json`, `package-lock.json`, `pyproject.toml`
- Create: `tests/test_crunchyroll_playback.py`
- Create: `tests/test_crunchyroll_player_server.py`
- Create: `tests/web/crunchyroll-player.test.mjs`

**Steps:**

1. Pin Shaka Player from its upstream Apache-2.0 package, record notice/license, and bundle it locally; no CDN runtime.
2. Add RED tests for loopback-only bind, random high-entropy path, no-store/CSP headers, exact opaque session handle, manifest/license route allowlists, missing/foreign session rejection, no arbitrary upstream URL, no token in HTML/JS/argv/environment, and idempotent release.
3. Add RED lifecycle tests for reserve-after-confirmation, partial startup unwind, ready/playing timeout, browser close, process-group TERM/KILL/reap, stop/replacement/shutdown, and same-device stale-session cleanup.
4. Implement `BraveWidevinePlaybackManager` as a separate backend from `MediaPlaybackManager`.
5. Implement narrowly scoped manifest and license mediation. Validate returned CDN origins from trusted playback metadata; never accept a caller-provided URL.
6. Launch Brave app mode with no remote debugging port, no profile shared with voice or the old pairing prototype, and no credential-bearing argv/environment.
7. Run unit/web tests and build the wheel.
8. **Live Gate 3 (interactive, serialized):** after Gate 1 authorization exists, request one known episode only after explicit confirmation; verify video/audio, immediate playback, seek, pause, volume, fullscreen, subtitles, clean close, upstream release, and no leftover process/listener. If this fails, stop integration and retain the old deployed path.
9. Commit only after unit/build checks and the real feasibility result are recorded: `feat: play Crunchyroll streams through Brave Widevine`.

### Task 7: Route Crunchyroll through controller, gateway, service, and shutdown

**Objective:** Integrate the verified provider/backend while preserving exact-session media semantics and existing YouTube/anime behavior.

**Files:**
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `src/realtime_action_spike/service.py`
- Modify: `src/realtime_action_spike/config.py`
- Modify: `src/realtime_action_spike/capabilities.py`
- Modify: `tests/runtime/test_controller.py`
- Modify: `tests/test_gateway.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_capabilities.py`

**Steps:**

1. Add RED tests proving Crunchyroll preparation creates an exact pending proposal, revision stays on Crunchyroll, stale acceptance is rejected, `media_ready` routes only Crunchyroll locators to the Brave manager, YouTube/anime remain on `mpv`, and every controller/service teardown path shuts down provider and player.
2. Add RED gateway tests for typed `authentication_required`, `ambiguous`, `unavailable`, provider failure, auth failure, and playback failure without leaking response bodies.
3. Implement injected provider/playback protocols and source-based routing in `VoiceSessionController`.
4. Replace `CrunchyrollLoginManager` construction with the new provider stack only behind an explicit migration-ready configuration flag until live Gates 1–3 pass.
5. Keep shutdown order deterministic: voice browser/session, Crunchyroll provider/player, generic media, HTTP, activation socket.
6. Run focused combined tests, then `uv run pytest -q`, `npm run test:web`, `npm run check:web`, `uv build`.
7. Commit: `feat: integrate Crunchyroll provider into voice flow`.

### Task 8: Execute real authorization and catalog gates

**Objective:** Verify the undocumented provider contract against Crunchyroll without persisting test secrets or leaking account data.

**Files:**
- Create: `scripts/smoke_crunchyroll_provider.py`
- Create: `tests/integration/test_crunchyroll_live.py` guarded by explicit opt-in environment flag
- Create: `docs/agent-notes/crunchyroll-provider-verification.md` containing only sanitized outcomes

**Steps:**

1. Gate 1: request a real device code while printing only shape, URL, lengths, expiry, and polling interval. Show the official code/QR through the application UI; user approves on Crunchyroll.
2. Prove refresh token storage via Secret Service, restart refresh without a second login, and explicit disconnect deletion. Do not print or inspect secret values.
3. Gate 2: resolve a known title and episode; verify exact identity, ambiguity, and unavailable behavior using only display-safe metadata.
4. Record endpoint/schema fingerprints and sanitized pass/fail evidence; never response bodies or account identifiers.
5. If auth/catalog contracts fail, stop before playback/integration migration and leave the old deployed path intact.
6. Commit: `test: verify Crunchyroll provider contracts`.

### Task 9: Atomic migration, cleanup, deployment, and main-flow gate

**Objective:** Remove the obsolete LAN authentication surface only after the replacement works end to end in the deployed service.

**Files:**
- Delete: `src/realtime_action_spike/crunchyroll_remote/`
- Delete/replace: `tests/test_crunchyroll_remote*.py`
- Modify: `pyproject.toml`, `uv.lock`, service/controller imports, docs
- Modify: `docs/superpowers/specs/2026-08-12-crunchyroll-brave-player-shell-design.md` with a superseded banner/link
- Modify: `docs/agent-notes/crunchyroll-phone-login-prototype.md` with retirement status

**Steps:**

1. Confirm Gates 1–3 have real evidence and all automated tests pass.
2. Add RED architecture/source-structure tests requiring no old CLI, LAN bind, CDP login input, phone assets, encrypted keyboard protocol, or old manager imports.
3. Delete the old runtime/tests/assets, remove PyNaCl only if no remaining consumer exists, retain QR generation for direct official activation, and rebuild locks/assets.
4. Build the wheel, reinstall into `/home/neos/.local/share/okay-hermes-realtime/venv`, restart `okay-hermes-realtime-controller.service`, and verify health/capability state.
5. Gate 4: exercise a real spoken explicit request through `ask -> resolve -> preview -> accept -> play -> close -> voice restored`; also manually verify reject/cancel/revise and one controlled failure.
6. Verify no listener on 42877 or 49200 and no obsolete Brave profile process.
7. Remove the exact UFW rule by its current numbered/tuple identity only after verifying the old listener can no longer be launched. Do not remove unrelated firewall rules.
8. Remove the old dedicated pairing profile only after inspecting that it contains no state needed by the new Secret Service authorization.
9. Run final gates: `uv run pytest -q`, `npm run test:web`, `npm run check:web`, `uv build`, deployed health, real main flow, process/listener cleanup, and `git diff --check`.
10. Commit coherent cleanup/deploy source changes: `refactor: retire Crunchyroll LAN pairing`.

## Completion criteria

- Real device authorization works through Crunchyroll HTTPS without this application receiving credentials.
- Refresh survives controller restart through Secret Service and disconnect deletes it.
- Exact official episode resolution produces the existing confirmation surface.
- Only exact acceptance reserves and launches playback.
- Brave plays official encrypted content through Widevine; no DRM keys are extracted.
- Stop, close, replacement, failure, controller teardown, and service shutdown release stream/process/server state.
- Explicit Crunchyroll requests never silently route to unofficial anime.
- Full Python/web/build gates pass from a clean process.
- The deployed spoken E2E passes.
- The old LAN pairing runtime, firewall rule, fixed ports, and obsolete browser profile are removed only after replacement acceptance.
