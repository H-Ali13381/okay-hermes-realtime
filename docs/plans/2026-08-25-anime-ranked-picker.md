# Anime Ranked Picker (AniDB, No Crunchyroll) Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Fix the anime voice path's dead-end ambiguity behavior by ranking AniDB search candidates deterministically and letting the realtime model disambiguate through a strict typed choice tool — with no Crunchyroll involvement.

**Architecture:** Extend `AnimeSourceAdapter` with a pure ranked-candidates resolver (`resolve_candidates`) that scores AniDB search results with the fixed title-similarity rules already proven in this repo (exact > containment > prefix > token overlap), then falls back to AniDB search order. The controller keeps its single-pending-media model: a high-confidence unique match resolves directly to today's confirmation card; otherwise it stores up to five controller-private `PendingMedia` proposals and publishes one new `media_choices` display message. A new `media_choose` capability consumes exactly one displayed choice and reuses the existing accept/handoff path unchanged. All Crunchyroll surfaces stay intact but untouched.

**Tech Stack:** Python 3.12, stdlib dataclasses, pytest/pytest-asyncio, existing FastAPI gateway, vanilla JS frontend bundle (esbuild).

**Non-goals / constraints:**
- No Crunchyroll code changes. `media_prepare_crunchyroll`, `crunchyroll_remote`, and their tests remain as-is on main.
- No new browser window in this pass; choices render inline in the existing voice page card area.
- No external ranking services (no MAL/AniList); AniDB search order plus local deterministic scoring only.
- The playback pipeline (mpv handoff, resume, progress) is not modified.

---

### Task 1: Add `AnimeChoice` model and `resolve_candidates` to the anime adapter

**Objective:** Produce deterministically ranked candidate titles from an AniDB search without resolving streams.

**Files:**
- Modify: `src/realtime_action_spike/anime.py`
- Test: `tests/test_anime.py`

**Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_resolve_candidates_ranks_exact_match_first() -> None:
    transport = FixtureTransport()
    adapter = AnimeSourceAdapter(transport=transport, preference_provider=lambda: "sub")

    choices = await adapter.resolve_candidates("Cowboy Bebop")

    assert [item.title for item in choices] == ["Cowboy Bebop", "Cowboy Bebop: The Movie"]
    assert all(isinstance(item, AnimeChoice) for item in choices)


@pytest.mark.asyncio
async def test_resolve_candidates_prefix_query_keeps_source_order_under_equal_scores() -> None:
    adapter = AnimeSourceAdapter(transport=FixtureTransport(), preference_provider=lambda: "sub")

    choices = await adapter.resolve_candidates("cowboy")

    # Neither exact nor contained; stable AniDB order decides.
    assert [item.title for item in choices] == ["Cowboy Bebop", "Cowboy Bebop: The Movie"]


def test_anime_choice_is_bounded_and_immutable() -> None:
    choice = AnimeChoice(series_id="cowboy-bebop-1065", title="Cowboy Bebop")
    assert choice.series_id == "cowboy-bebop-1065"
    with pytest.raises(FrozenInstanceError):
        choice.title = "changed"  # type: ignore[misc]
```

**Step 2: Run tests to verify RED**

Run: `uv run pytest -q tests/test_anime.py -k resolve_candidates or anime_choice`
Expected: FAIL — `AnimeChoice` and `resolve_candidates` do not exist.

**Step 3: Implement**

Add after `AnimeResolution`:

```python
@dataclass(frozen=True, slots=True)
class AnimeChoice:
    series_id: str
    title: str

    def __post_init__(self) -> None:
        if not _SERIES_ID_RE.fullmatch(self.series_id):
            raise AnimeSourceError("invalid anime series id")
        if (
            not self.title
            or len(self.title) > 300
            or _CONTROL_RE.search(self.title)
        ):
            raise AnimeSourceError("invalid anime title")
```

Add scoring helpers (fixed weights, no fuzzy dependency) and the adapter method:

```python
def _title_fit_score(query: str, candidate_title: str) -> float:
    """Fixed deterministic fit score: 1.0 exact, then containment, then token overlap."""
    query_tokens = tuple(_normalized_tokens(query))
    candidate_tokens = tuple(_normalized_tokens(candidate_title))
    if not query_tokens or not candidate_tokens:
        return 0.0
    if query_tokens == candidate_tokens:
        return 1.0
    query_norm = " ".join(query_tokens)
    candidate_norm = " ".join(candidate_tokens)
    if query_norm == candidate_norm:
        return 1.0
    if _contains_contiguous_span(candidate_tokens, query_tokens):
        coverage = len(query_tokens) / len(candidate_tokens)
        return min(0.92 + 0.06 * coverage, 0.999999)
    intersection = _token_intersection(query_tokens, candidate_tokens)
    union = len(set(query_tokens) | set(candidate_tokens))
    if not union:
        return 0.0
    return round(0.78 * (intersection / union), 6)
```

Add to `AnimeSourceAdapter`:

```python
async def resolve_candidates(
    self,
    query: str,
    *,
    limit: int = 5,
) -> tuple[AnimeChoice, ...]:
    normalized_query = " ".join(query.split())
    if (
        not normalized_query
        or len(normalized_query) > 300
        or _CONTROL_RE.search(normalized_query)
    ):
        raise AnimeSourceError("invalid anime search query")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise AnimeSourceError("invalid candidate limit")
    candidates = parse_search_html(await self._transport.search(normalized_query))
    scored = [
        (_title_fit_score(normalized_query, item.title), index, item)
        for index, item in enumerate(candidates)
    ]
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return tuple(
        AnimeChoice(series_id=item.series_id, title=item.title)
        for _score, _index, item in scored[:limit]
    )
```

Keep `_select_candidate` and `resolve` unchanged for now (Task 3 rewires them).

**Step 4: Run tests to verify GREEN**

Run: `uv run pytest -q tests/test_anime.py`
Expected: PASS (all existing + new).

**Step 5: Commit**

```bash
git add src/realtime_action_spike/anime.py tests/test_anime.py
git commit -m "feat: rank AniDB search candidates deterministically"
```

---

### Task 2: Auto-select when one candidate clearly wins

**Objective:** Preserve today's zero-question behavior for exact and unambiguous matches while replacing guess-prone ambiguity with a typed multi-choice result.

**Files:**
- Modify: `src/realtime_action_spike/anime.py`
- Test: `tests/test_anime.py`

**Step 1: Write failing tests**

```python
@dataclass(frozen=True, slots=True)
class RankedResolution:
    kind: Literal["resolved", "choices"]
    resolution: AnimeResolution | None
    choices: tuple[AnimeChoice, ...]


@pytest.mark.asyncio
async def test_resolve_or_rank_returns_resolution_for_exact_unique_match() -> None:
    adapter = AnimeSourceAdapter(transport=FixtureTransport(), preference_provider=lambda: "sub")

    result = await adapter.resolve_or_rank("Cowboy Bebop", episode=2, audio_mode="auto")

    assert result.kind == "resolved"
    assert result.resolution is not None
    assert result.resolution.series_id == "cowboy-bebop-1065"
    assert result.choices == ()


@pytest.mark.asyncio
async def test_resolve_or_rank_returns_choices_instead_of_raising() -> None:
    adapter = AnimeSourceAdapter(transport=FixtureTransport(), preference_provider=lambda: "sub")

    result = await adapter.resolve_or_rank("cowboy", episode=1, audio_mode="auto")

    assert result.kind == "choices"
    assert result.resolution is None
    assert len(result.choices) == 2


@pytest.mark.asyncio
async def test_resolve_or_rank_single_candidate_resolves_directly() -> None:
    single = FixtureTransport(search=(
        b'<a href="/anime/cowboy-bebop-1065"><img alt="Cowboy Bebop" '
        b'src="https://cdn.xlsbox.com/poster/small/1782735600/1065.jpg"></a>'
    ))
    adapter = AnimeSourceAdapter(transport=single, preference_provider=lambda: "sub")

    result = await adapter.resolve_or_rank("Cowboy Bebop", episode=1, audio_mode="sub")

    assert result.kind == "resolved"
    assert result.resolution is not None
```

**Step 2: Run tests to verify RED**

Run: `uv run pytest -q tests/test_anime.py -k resolve_or_rank`
Expected: FAIL — method missing.

**Step 3: Implement**

```python
_CLEAR_WIN_MARGIN = 0.15


async def resolve_or_rank(
    self,
    query: str,
    *,
    episode: int = 1,
    audio_mode: Literal["auto", "sub", "dub"] = "auto",
) -> RankedResolution:
    normalized_query = " ".join(query.split())
    candidates = parse_search_html(await self._transport.search(normalized_query))
    if len(candidates) == 1:
        resolved = await self._resolve_candidate(candidates[0], normalized_query, episode, audio_mode)
        return RankedResolution(kind="resolved", resolution=resolved, choices=())
    exact = tuple(item for item in candidates if _title_fit_score(normalized_query, item.title) >= 1.0)
    if len(exact) == 1:
        resolved = await self._resolve_candidate(exact[0], normalized_query, episode, audio_mode)
        return RankedResolution(kind="resolved", resolution=resolved, choices=())
    choices = await self.resolve_candidates(normalized_query)
    top_score = _title_fit_score(normalized_query, choices[0].title)
    runner_up = _title_fit_score(normalized_query, choices[1].title) if len(choices) > 1 else 0.0
    if top_score >= 1.0 or (top_score - runner_up) >= _CLEAR_WIN_MARGIN:
        winner = next(item for item in candidates if item.series_id == choices[0].series_id)
        resolved = await self._resolve_candidate(winner, normalized_query, episode, audio_mode)
        return RankedResolution(kind="resolved", resolution=resolved, choices=())
    return RankedResolution(kind="choices", resolution=None, choices=choices)
```

Extract the body of the current `resolve` after `_select_candidate` into `_resolve_candidate(candidate, query, episode, audio_mode) -> AnimeResolution`; make `resolve` delegate through `resolve_or_rank` so behavior stays compatible for callers that expect a resolution or exception. Keep raising `MissingAnimeEpisodeError`/`MissingAnimeLanguageError` from `_resolve_candidate`.

**Step 4: Run tests to verify GREEN**

Run: `uv run pytest -q tests/test_anime.py`
Expected: PASS. The old `test_adapter_returns_typed_ambiguity_instead_of_guessing` must be updated: `"cowboy"` now yields `kind == "choices"` instead of raising.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/anime.py tests/test_anime.py
git commit -m "feat: resolve clear anime winners and rank ambiguous requests"
```

---

### Task 3: Controller stores and displays ranked anime choices

**Objective:** On a `choices` outcome, keep every proposal private and publish one bounded display message.

**Files:**
- Modify: `src/realtime_action_spike/runtime/controller.py`
- Modify: `src/realtime_action_spike/runtime/protocol.py`
- Test: `tests/runtime/test_controller.py`

**Step 1: Write failing protocol test first** (`tests/runtime/test_protocol.py`)

```python
def test_media_choices_message_is_strict_and_bounded() -> None:
    message = MediaChoicesMessage.model_validate({
        "session_id": "local-session-01",
        "query": "cowboy",
        "pending_ids": ["pending-a-000001", "pending-b-000002"],
        "choices": [
            {"position": 1, "series_id": "cowboy-bebop-1065", "title": "Cowboy Bebop"},
            {"position": 2, "series_id": "cowboy-bebop-the-movie-1066", "title": "Cowboy Bebop: The Movie"},
        ],
    })
    assert message.type == "media_choices"

    with pytest.raises(ValidationError):
        MediaChoicesMessage.model_validate({
            "session_id": "local-session-01",
            "query": "cowboy",
            "pending_ids": ["p1", "p2"],
            "choices": [{"position": 1, "series_id": "x-1", "title": "X"}],
            "extra": True,
        })
```

Protocol additions:

```python
class AnimeChoiceDisplay(_StrictBaseModel):
    position: int = Field(ge=1, le=5)
    series_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d+$")
    title: str = Field(min_length=1, max_length=300)


class MediaChoicesMessage(_StrictBaseModel):
    type: Literal["media_choices"] = "media_choices"
    session_id: str
    query: str = Field(min_length=1, max_length=300)
    pending_ids: tuple[str, ...] = Field(min_length=2, max_length=5)
    choices: tuple[AnimeChoiceDisplay, ...] = Field(min_length=2, max_length=5)
```

Add it to the `LoopbackMessage` union.

**Step 2: Write failing controller tests**

```python
class AmbiguousAnimeAdapter:
    def __init__(self, choices: tuple[AnimeChoice, ...]) -> None:
        self._choices = choices
        self.resolved_with: tuple[str, int, str] | None = None

    async def resolve(self, query: str, **kwargs: object) -> AnimeResolution:
        self.resolved_with = (query, int(kwargs.get("episode", 1)), str(kwargs.get("audio_mode")))
        raise AssertionError("ambiguous request should not resolve a stream")

    async def resolve_or_rank(self, query: str, **kwargs: object):
        return RankedResolution(kind="choices", resolution=None, choices=self._choices)


class ResolvingAnimeAdapter(AmbiguousAnimeAdapter):
    async def resolve_or_rank(self, query: str, **kwargs: object):
        mode = kwargs.get("audio_mode", "auto")
        resolved_mode = "sub" if mode == "auto" else str(mode)
        episode = int(kwargs.get("episode", 1))
        return RankedResolution(
            kind="resolved",
            resolution=AnimeResolution(
                series_id="cowboy-bebop-1065",
                series_title="Cowboy Bebop",
                episode=episode,
                audio_mode=resolved_mode,  # type: ignore[arg-type]
                subtitle=f"Episode {episode} · Subbed",
                poster_url="https://cdn.xlsbox.com/poster/small/1065.jpg",
                stream_url="https://hls.anidb.app/stream/example/index-1080.m3u8",
                available_episode_numbers=(1, 2, 3, 4),
            ),
            choices=(),
        )


@pytest.mark.asyncio
async def test_ambiguous_anime_prepares_choices_and_resolves_selected_once() -> None:
    media = RecordingMediaManager()
    controller = VoiceSessionController(
        DeterministicLauncher(),
        token_store=LaunchTokenStore(token_factory=SequenceFactory(["token-choice"])),
        session_id_factory=SequenceFactory(["local-choice-01"]),
        pending_id_factory=SequenceFactory(
            ["pending-ch-1", "pending-ch-2", "pending-sel-1"]
        ),
        media_manager=media,  # type: ignore[arg-type]
        youtube_resolver=RecordingYouTubeResolver(),
        anime_adapter=AmbiguousAnimeAdapter((
            AnimeChoice(series_id="cowboy-bebop-1065", title="Cowboy Bebop"),
            AnimeChoice(series_id="cowboy-bebop-the-movie-1066", title="Cowboy Bebop: The Movie"),
        )),  # type: ignore[arg-type]
    )
    activation = await controller.activate("http://127.0.0.1:8765/voice")
    session_id = activation.session_id

    result = await controller.prepare_anime_media(
        session_id=session_id, query="cowboy", episode=None,
        episode_offset=None, audio_mode="auto",
    )

    assert result["status"] == "awaiting_choice"
    assert result["choice_count"] == 2
    outbound = await controller.wait_for_outbound_message(session_id)
    assert outbound.type == "media_choices"

    chosen = await controller.choose_anime_media(
        session_id=session_id, position=2,
    )
    assert chosen["status"] == "awaiting_confirmation"
    confirm = await controller.wait_for_outbound_message(session_id)
    assert confirm.type == "media_confirmation"
    assert confirm.title == "Cowboy Bebop: The Movie"
    assert confirm.pending_id == "pending-sel-1"


@pytest.mark.asyncio
async def test_stale_choice_position_fails_closed_without_launch() -> None:
    # After a superseding prepare, old positions are gone; choose fails ValueError;
    # media manager never launched.
```

**Step 3: Implement controller changes**

- New injected state: `self._anime_choices: dict[str, tuple[str, tuple[tuple[str, PendingMedia], ...]]]` mapping session → `(query, ((choice_key, pending), ...))`. Cleared in `clear_session` paths alongside `_accepted_media`.
- In `prepare_anime_media`, call `resolve_or_rank` instead of `resolve`:
  - `kind == "resolved"` → build the same `PendingMedia` as today and `_publish_pending_media`.
  - `kind == "choices"` → for each choice, build a lightweight `PendingMedia` via `_resolve_candidate` **only at selection time**, not now. Store `(series_id, title)` pairs plus the query; publish `MediaChoicesMessage`; return `{"status": "awaiting_choice", "query": ..., "choice_count": n}`. Superseding prepares replace the stored set.
- New `choose_anime_media(session_id, *, position)` under the same active-session/stale guards:
  - Validate `position` against the current set; unknown/expired → `ValueError`.
  - Call `self._anime_adapter`'s candidate resolution for the chosen series id (add `resolve_choice(choice, query, episode, audio_mode)` to the adapter that runs the existing episodes→languages→embed→playlist flow for a known `AnimeCandidate`).
  - Build `PendingMedia`, store it as the single pending generation, publish the normal confirmation card, return `{"status": "awaiting_confirmation", ...}`.
  - Consuming the set clears it; replay fails closed.
- Resume logic only applies to direct resolutions (unchanged path).

**Step 4: Run tests to verify GREEN**

Run: `uv run pytest -q tests/runtime/test_controller.py tests/runtime/test_protocol.py`
Expected: PASS including all pre-existing tests.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/runtime/controller.py src/realtime_action_spike/runtime/protocol.py tests/runtime/
git commit -m "feat: publish ranked anime choices from controller"
```

---

### Task 4: Strict `media_choose` capability and gateway dispatch

**Objective:** Give the realtime model exactly one typed way to select a displayed choice.

**Files:**
- Modify: `src/realtime_action_spike/capabilities.py`
- Modify: `src/realtime_action_spike/contracts.py`
- Modify: `src/realtime_action_spike/gateway.py`
- Modify: `src/realtime_action_spike/config.py` (instructions text)
- Test: `tests/test_capabilities.py`, `tests/test_gateway.py`, `tests/test_config.py`

**Step 1: Write failing contract tests**

```python
def test_media_choose_contract_is_strict_and_position_bounded() -> None:
    tool = next(tool for tool in build_openai_tools() if tool["name"] == "media_choose")
    assert tool["parameters"]["required"] == ["position"]
    props = tool["parameters"]["properties"]
    assert list(props) == ["position"]
    parsed = parse_media_choose_arguments({"position": 2})
    assert parsed.position == 2
    with pytest.raises(ExecutionContractError):
        CapabilityBroker().execute("media_choose", {"position": 9})
    with pytest.raises(ExecutionContractError):
        CapabilityBroker().execute("media_choose", {"position": 1, "title": "x"})
```

Contracts addition:

```python
class MediaChooseArguments(StrictArguments):
    position: int = Field(
        ge=1,
        le=5,
        description="The displayed number of the anime choice to play.",
    )
```

Capability definition between `media_prepare_anime` and `media_prepare_crunchyroll`:

```python
CapabilityDefinition(
    name="media_choose",
    description=(
        "Select one displayed anime choice by its spoken number after media_prepare_anime "
        "returned awaiting_choice. Requires the originating live session."
    ),
    arguments_model=MediaChooseArguments,
    execution="local",
    handler=_media_action_requires_live_session,
),
```

Gateway branch mirrors `media_decide`:

```python
elif execution_request.name == "media_choose":
    chosen = parse_media_choose_arguments(execution_request.arguments)
    chosen_result = await _controller.choose_anime_media(
        session_id=active_session_id,
        position=chosen.position,
    )
    result = {
        "ok": True,
        "capability": execution_request.name,
        "execution": "local",
        "result": chosen_result,
    }
```

Instructions update (config.py `DEFAULT_INSTRUCTIONS`, Tools section):

```text
- If media_prepare_anime returns awaiting_choice, say the numbered options once, briefly, then
  stop listening for the number. Use media_choose with only that position. Never re-resolve or
  invent titles that were not displayed.
```

**Step 2: Verify RED** — run the three test files; capability/gateway/instruction assertions fail.

**Step 3: Implement minimal models/parser/capability/gateway/instructions.**

**Step 4: Verify GREEN**

Run: `uv run pytest -q tests/test_capabilities.py tests/test_gateway.py tests/test_config.py`
Expected: PASS. Update the existing instructions assertion to include `media_choose`.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/capabilities.py src/realtime_action_spike/contracts.py \
  src/realtime_action_spike/gateway.py src/realtime_action_spike/config.py \
  tests/test_capabilities.py tests/test_gateway.py tests/test_config.py
git commit -m "feat: add strict media_choose voice selection"
```

---

### Task 5: Inline choices rendering in the voice page

**Objective:** Display the ranked choices in the existing card so the user can see what the numbers mean.

**Files:**
- Modify: `frontend/media-confirmation.js` (add `parseMediaChoices`)
- Modify: `frontend/voice.js` (handle `media_choices` event)
- Modify: `src/realtime_action_spike/web/index.html` + rebuild bundle
- Modify: `src/realtime_action_spike/web/voice.css` (small list styles)
- Test: `tests/web/media-confirmation.test.mjs`, `tests/test_ui_contract.py`

**Step 1: Write failing web test**

```js
test("parses strict bounded media choices", () => {
  const parsed = parseMediaChoices({
    type: "media_choices",
    session_id: SESSION_ID,
    query: "cowboy",
    pending_ids: ["aaaaaaaaaaaaaaaaaaaaaa1", "aaaaaaaaaaaaaaaaaaaaaa2"],
    choices: [
      { position: 1, series_id: "cowboy-bebop-1065", title: "Cowboy Bebop" },
      { position: 2, series_id: "cowboy-bebop-the-movie-1066", title: "Cowboy Bebop: The Movie" },
    ],
  }, SESSION_ID);
  assert.equal(parsed.choices.length, 2);
  assert.equal(parsed.choices[0].position, 1);
  assert.throws(() => parseMediaChoices({ type: "media_choices" }, SESSION_ID));
});
```

Parser rules mirror the existing confirmation parser: exact keys, safe ids, 2–5 items, https allowlisted thumbnails not needed here (titles only), positions strictly ascending starting at 1.

**Step 2: UI additions**

- Reuse `#media-confirmation` region: badge becomes "Anime · pick one", title shows the query, and a `<ol id="media-choice-list">` renders `textContent`-built rows "1. Cowboy Bebop".
- `voice.js`: `if (event.type === "media_choices") { showMediaChoices(event); return; }` placed beside the confirmation branch; hide choices when any later `media_confirmation` arrives.
- No click handlers; voice remains the interaction mechanism.

**Step 3: Verify**

Run: `npm run test:web && npm run check:web && uv run pytest -q tests/test_ui_contract.py`
Expected: PASS.

**Step 4: Commit**

```bash
git add frontend/ src/realtime_action_spike/web/ tests/web/ tests/test_ui_contract.py package.json
git commit -m "feat: render ranked anime choices inline"
```

---

### Task 6: Full verification and live deploy

**Objective:** Prove the committed tree passes all gates, install it, and verify with real services.

**Step 1: Full source gates**

```bash
uv run pytest -q
uv run ruff check .
uv run python -m compileall -q src scripts
npm ci && npm run check:web
git diff --check && git status --short
```

Expected: all pass; clean status.

**Step 2: Package and deploy from this worktree**

```bash
bash scripts/install_user_services.sh --model ~/.local/share/okay-hermes-realtime/models/<existing>.onnx
systemctl --user restart okay-hermes-realtime-controller.service
systemctl --user restart okay-hermes-realtime-wakeword.service
systemctl --user is-active okay-hermes-realtime-controller.service
curl --fail --silent http://127.0.0.1:8765/health
```

Expected: both units active; `/health` lists `media_choose` among capabilities.

**Step 3: Live voice verification**

1. "Play Cowboy Bebop." → single confirmation card (exact match auto-selects; no extra question).
2. "Play Frieren." → choices card listing Frieren / Season 2 / Mini Anime; say "two"; Season 2 confirmation card appears; accept launches mpv once.
3. Say a stale number after a new request → tool fails closed; nothing launches.

Verify traces record `media_confirmation_prepared` and no unexpected loopback listeners or leaked stream URLs appear in journal output.

---

## Required final evidence

- Green full suite, ruff, compileall, web checks on the committed tree.
- Installed service health showing `media_choose`.
- Live traces: choices flow resolving exactly the selected series once; stale selection rejected.
