# Natural Media Confirmation and Anime Playback Implementation Plan

> **For Hermes:** Use subagent-driven-development to implement this plan task-by-task. Every production change follows a witnessed RED → GREEN cycle, then spec-compliance and code-quality review.

**Goal:** Replace paused-player previews with a shared inline confirmation card, let the Realtime model interpret natural-language decisions into strict controller actions, and add native anime playback with saved sub/dub preference, progress, resume policy, and next-episode confirmation.

**Architecture:** Source adapters resolve immutable metadata-only `PendingMedia` records. The active controller owns one exact pending generation and accepts only `accept | reject | cancel | revise` for its `pending_id`; only `accept` may create `mpv`. The browser renders sanitized controller state and never resolves sources or launches players. Anime source fragility is isolated behind a bounded argv-only curl transport and typed parser.

**Tech stack:** Python 3.12, asyncio, Pydantic 2, FastAPI, pytest/pytest-asyncio, vanilla JavaScript/CSS, Node test runner, esbuild, mpv JSON IPC, yt-dlp, curl, JSON state under XDG state home.

**Approved spec:** `docs/superpowers/specs/2026-08-12-realtime-media-confirmation-anime-design.md`

---

## Safety and lifecycle invariants

1. No source adapter starts a player.
2. The model never receives or submits a playback URL; it references the exact opaque `pending_id`.
3. Only the controller validates pending generation/session and launches the player.
4. `revise` atomically invalidates the old generation before resolution; stale accepts cannot play.
5. No shell strings. Resolver subprocesses use argv, timeouts, output caps, cancellation, terminate/kill/reap.
6. During playback, existing microphone/VAD suppression and exact/reconstructed continuation behavior remain authoritative.
7. Tray Exit, service shutdown, browser failure, and controller teardown converge to zero owned resolver/player processes and suppress next-episode prompts.
8. Preference/progress writes are owner-only and atomic; explicit request-local mode does not rewrite the global default.

## Task 1: Strict model-facing contracts and prompt policy

**Objective:** Expose metadata preparation and natural-language decision tools with schemas strict enough for controller execution.

**Files:**
- Modify: `src/realtime_action_spike/contracts.py`
- Modify: `src/realtime_action_spike/capabilities.py`
- Modify: `src/realtime_action_spike/config.py`
- Modify: `tests/test_capabilities.py`
- Modify: `tests/test_config.py`

**RED:** Add tests requiring capabilities `media_prepare_youtube`, `media_prepare_anime`, and `media_decide`; `media_decide` action enum `accept | reject | cancel | revise`; revision fields forbidden unless action is `revise`; bounded `query`, positive `episode`, bounded `episode_offset`, `audio_mode=auto|sub|dub`; descriptions explicitly tell the model to interpret conversational replies such as “yep, that's it” and compound revisions such as “no, play the next episode.” Remove the immediate `media_play_youtube` contract.

Run:
```bash
uv run pytest tests/test_capabilities.py tests/test_config.py -q
```
Expected RED: missing contracts/capabilities and stale prompt assertions.

**GREEN:** Implement Pydantic contracts and parsers, static capability definitions that require live-session binding, and Realtime instructions that preserve natural interpretation while forbidding invented actions/IDs.

Verify:
```bash
uv run pytest tests/test_capabilities.py tests/test_config.py -q
uv run ruff check src/realtime_action_spike/contracts.py src/realtime_action_spike/capabilities.py src/realtime_action_spike/config.py tests/test_capabilities.py tests/test_config.py
```

**Commit:** `feat: define natural media decision contracts`

## Task 2: Shared pending-media catalog and metadata-only YouTube preparation

**Objective:** Resolve YouTube metadata into a pending record without spawning `mpv`.

**Files:**
- Create: `src/realtime_action_spike/media_catalog.py`
- Modify: `src/realtime_action_spike/media.py`
- Modify: `tests/test_media.py`
- Create: `tests/test_media_catalog.py`

**RED:** Test immutable `PendingMedia` display projection; server-private locator exclusion; allowlisted YouTube thumbnail derivation; exact session/generation checks; replace/clear behavior; metadata preparation creates no player process; accepting later launches exactly one player; duplicate/stale accept cannot launch another.

Run:
```bash
uv run pytest tests/test_media_catalog.py tests/test_media.py -q
```
Expected RED: missing catalog and current prepare path starts `mpv`.

**GREEN:** Add typed source/episode/resume/display models and a controller-facing pending catalog. Refactor `MediaPlaybackManager` so launch accepts a validated private locator and start offset; retain one owned player/process-group/IPC watcher. Keep YouTube resolution in `YouTubeResolver`, include thumbnail metadata, but do not spawn during prepare.

Verify focused tests and Ruff.

**Commit:** `refactor: separate media resolution from playback`

## Task 3: Persistent global preference, progress, and resume policy

**Objective:** Persist sub/dub preference and anime progress with the exact 5% + 90-second rule.

**Files:**
- Create: `src/realtime_action_spike/media_state.py`
- Create: `tests/test_media_state.py`

**RED:** Test default `sub`; explicit request override leaves global default unchanged; explicit preference mutation persists; owner-only directories/files; atomic replacement; malformed state falls back safely with bounded diagnostics; per-series progress; resume same episode only when both remaining fraction >5% and remaining seconds >90; otherwise propose next; exact threshold boundaries; EOF wins over last sample.

Run:
```bash
uv run pytest tests/test_media_state.py -q
```
Expected RED: module missing.

**GREEN:** Implement small JSON stores with injected paths/clock, typed records, atomic temp-file + `os.replace`, `0600` files/`0700` directories, and pure resume proposal function.

**Commit:** `feat: persist anime preference and progress`

## Task 4: Native anime source adapter

**Objective:** Resolve an anime query/episode/mode into one typed pending item without wrapping stock ani-cli.

**Files:**
- Create: `src/realtime_action_spike/anime.py`
- Create: `tests/test_anime.py`
- Create fixtures under: `tests/fixtures/anime/`

**RED:** Fixture tests for search candidates, canonical IDs/posters, episode map, sub/dub language selection, HLS extraction/quality selection, ambiguity, no result, missing episode/language, malformed JSON/HTML/playlist, host allowlist, URL control characters, argv-only curl invocation, fixed browser UA, timeout/cancellation/nonzero/oversize/stubborn-process reap, and no secret/stderr leakage. The anime transport accepts no cookie, credential, or arbitrary-header inputs and never accepts a model-controlled fetch URL; dynamic embed/playlist URLs must come from validated source responses and pass exact host/path checks before spawn.

Run:
```bash
uv run pytest tests/test_anime.py -q
```
Expected RED: module missing.

**GREEN:** Implement `CurlAnimeTransport` using `asyncio.create_subprocess_exec`, fixed `https://anidb.app` endpoint builders and narrowly validated embed/playlist hosts discovered from fixture payloads, byte caps, timeout/reaping, plus typed parsers and `AnimeSourceAdapter`. Use the global preference only when request mode is `auto`. Return private locator + safe display projection; never launch `mpv`.

**Commit:** `feat: add bounded native anime adapter`

## Task 5: Controller and gateway pending-decision lifecycle

**Objective:** Route preparation and strict decisions through the exact active voice session.

**Files:**
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `src/realtime_action_spike/runtime/protocol.py`
- Modify: `src/realtime_action_spike/service.py`
- Modify: `src/realtime_action_spike/config.py`
- Modify: `config.example.env`
- Modify: `tests/test_gateway.py`
- Modify: `tests/runtime/test_controller.py`
- Modify: `tests/runtime/test_protocol.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_config.py`

**RED:** Test prepare emits sanitized `media_confirmation` controller message and keeps voice live; accept current pending ID launches once and initiates existing playback handoff/suppression sequence; reject clears pending, keeps voice live, and returns a bounded instruction to ask one short follow-up when no corrected criteria were supplied; cancel clears only media request without a follow-up; revise invalidates first, resolves replacement, emits atomically, and refuses revision fields on other actions; an anime request with no history and no episode resolves episode 1; old/foreign/duplicate IDs fail; teardown cancels resolver and pending state; controller-only outbound messages are rejected inbound; config wires curl/state paths/timeouts.

Run:
```bash
uv run pytest tests/test_gateway.py tests/runtime/test_controller.py tests/runtime/test_protocol.py tests/test_service.py tests/test_config.py -q
```
Expected RED: stale immediate-play route and missing messages/methods.

**GREEN:** Bind source adapters/state/catalog in runtime service; add `prepare_youtube_media`, `prepare_anime_media`, and `decide_media`; queue display-safe messages; use short critical sections around generation checks but perform slow resolution outside lock with post-await lease checks. Maintain gateway call-id replay protections. Do not let `media_decide` accept raw locators.

**Commit:** `feat: coordinate pending media decisions`

## Task 6: Inline browser confirmation card

**Objective:** Render controller-owned pending media inline while preserving voice as the primary interface.

**Files:**
- Modify: `src/realtime_action_spike/web/index.html`
- Modify: `src/realtime_action_spike/web/voice.css`
- Modify: `frontend/voice.js`
- Create: `frontend/media-confirmation.js`
- Create: `tests/web/media-confirmation.test.mjs`
- Modify: `tests/test_ui_contract.py`
- Generated by build: `src/realtime_action_spike/web/voice.js`

**RED:** Node tests require escaped text, allowed/invalid thumbnail handling, replacement/clear, stale ID ignore, and accessible fallback controls. UI contract tests require card IDs/classes, “What would you like to do?” / “Respond naturally,” controller message handling, no literal phrase parser, no browser player execution, and no `media_ready` before accepted player generation exists.

Run:
```bash
npm run test:web
uv run pytest tests/test_ui_contract.py -q
```
Expected RED: missing module/DOM/message handlers.

**GREEN:** Implement a pure view-state/render helper and inline card matching approved mockup. Controller messages render/replace/clear the card. Buttons call the same strict `/execute` `media_decide` route as accessibility fallbacks, but normal spoken replies continue through the Realtime model. Rebuild bundle.

Verify:
```bash
npm run check:web
npm run test:web
uv run pytest tests/test_ui_contract.py -q
```

**Commit:** `feat: render inline media confirmation`

## Task 7: Playback progress and episode-completion continuation

**Objective:** Save playback position and turn anime EOF into a natural next-episode confirmation.

**Files:**
- Modify: `src/realtime_action_spike/media.py`
- Modify: `src/realtime_action_spike/media_state.py`
- Modify: `src/realtime_action_spike/media_catalog.py`
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `src/realtime_action_spike/runtime/protocol.py`
- Modify: `frontend/voice.js`
- Modify: `tests/test_media.py`
- Modify: `tests/test_media_state.py`
- Modify: `tests/runtime/test_controller.py`
- Modify: `tests/runtime/test_protocol.py`
- Modify: `tests/test_ui_contract.py`

**RED:** Test periodic `time-pos`/`duration` IPC sampling, final checkpoint, start offset argv/property, failed checkpoint not blocking teardown, natural EOF marks completion then queues next episode, manual close preserves partial progress, exact/reconstructed voice resume happens before follow-up, next episode never autoplays, and shutdown races suppress follow-up.

Run focused Python/UI tests; expected RED on missing progress and follow-up behavior.

**GREEN:** Add bounded IPC property reads/sampler task tied to exact player generation. Extend completion metadata with source/series/episode/position. On natural anime EOF, persist completion, restore the existing voice session, then prepare/display the next episode as a new pending generation. Keep YouTube’s generic contextual follow-up behavior. Cancel sampler/follow-up on any terminal shutdown.

**Commit:** `feat: resume anime and confirm next episode`

## Task 8: Integration, packaging, and documentation

**Objective:** Prove source/build/install contracts and document operational behavior.

**Files:**
- Modify: `README.md`
- Treat `docs/superpowers/specs/2026-08-12-realtime-media-confirmation-anime-design.md` as immutable after approval; any behavior or naming amendment requires separate user approval before editing it
- Modify installer/config tests as required
- Add targeted architecture/source-structure assertions only where they protect real boundaries

**RED/GREEN:** Add installer/package tests proving new Python modules, web bundle, config keys, and state paths survive wheel install; ensure stock ani-cli is not a runtime dependency; document source fragility and exact live smoke procedure.

Verify:
```bash
uv run pytest -q
uv run ruff check src tests
npm run check:web
npm run test:web
uv build
bash scripts/build-package.sh
git diff --check
```

Then inspect wheel contents and install into a temporary venv; import modules and serve `/voice`/`/health` from the built wheel.

**Commit:** `docs: document confirmed anime playback flow`

## Review gates

After every task:
1. Verify commit diff and focused test evidence locally.
2. Spec-compliance review against the exact task and approved spec.
3. Fix every gap and re-review until PASS.
4. Code-quality/security/lifecycle review.
5. Fix Critical/Important issues and re-review until APPROVED.

Final integration review must inspect the complete range from commit `01380a6` to branch HEAD and specifically challenge stale-generation races, process reaping, source URL confinement, model-visible locator leakage, UI escaping, state-file permissions, and teardown suppression.

## Deployment and live verification

Deployment is a separate final gate after branch verification:

1. Build wheel and web bundle from this worktree.
2. Install into the existing isolated Realtime venv using the repository’s normal `uv build` → `uv pip install` path.
3. Preserve the tuned wakeword threshold (`0.85`) and unrelated live config.
4. Restart controller/listener through user systemd.
5. Verify unit active state, `/health`, capability names, media idle state, and zero stale resolver/player processes.
6. Run live voice scenarios:
   - YouTube → preview card → “no, find a different video” → replacement → “yep, that’s it” → fullscreen play → close → contextual voice continuation.
   - Anime → natural title → correct subbed proposal → “sure, play it” → fullscreen play.
   - Anime revision → “no, play the next episode” → replacement card, no stale player launch.
   - Resume >5% and >90s; near-complete next-episode proposal; explicit dubbed override without changing global sub preference.
   - Natural EOF → voice resumes → next episode card; decline leaves voice active.
   - Tray Exit/controller stop → zero owned browser/player/resolver processes and no follow-up.

Do not claim audible playback or end-to-end voice success without observing the live run; if user speech/physical audio confirmation is required, present the exact test and wait for the user’s result rather than fabricating it.
