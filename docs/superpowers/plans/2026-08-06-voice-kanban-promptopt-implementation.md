# Voice Kanban PromptOpt Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make `gpt-realtime-2.1` route explicit Kanban/Hermes requests and consented unsupported work to Hermes while submitting only the direct task objective, validated through an adapted SkillOpt train/held-out execution gate.

**Architecture:** Add a repository-local Realtime WebSocket evaluator that runs text turns against the exact production instructions and tool schema without executing tools. Establish the current prompt as a baseline, use failures to make bounded prompt/schema edits, then require held-out non-regression before porting the winning contract. Keep deterministic backend safeguards narrow: rename `request` to `task`, reject obvious routing wrappers, preserve legitimate Kanban/Hermes subject matter, and derive card titles directly from the work.

**Tech Stack:** Python 3.12, Pydantic 2, `websockets` 15, pytest/pytest-asyncio, OpenAI Realtime WebSocket API, Hermes Kanban CLI, SkillOpt validation-gate methodology.

**Design:** `docs/superpowers/specs/2026-08-06-voice-kanban-promptopt-design.md`

**Official API references:**
- https://developers.openai.com/api/docs/guides/realtime-models-prompting
- https://developers.openai.com/api/docs/guides/realtime-conversations
- https://developers.openai.com/api/docs/guides/realtime-websocket

---

### Task 1: Create an isolated implementation worktree

**Objective:** Keep PromptOpt experiments and production edits isolated from the locally integrated `main` branch.

**Files:**
- No code files yet
- Worktree: `.worktrees/voice-kanban-promptopt`
- Branch: `feature/voice-kanban-promptopt`

**Step 1: Verify the base checkout**

Run:

```bash
git status --short --branch
git log -4 --oneline
```

Expected: `main` at or after the approved design commit; only the pre-existing `.hermes/` and `docs/agent-notes/` paths are untracked.

**Step 2: Create the worktree**

Run:

```bash
git worktree add .worktrees/voice-kanban-promptopt -b feature/voice-kanban-promptopt main
```

Expected: clean feature worktree at the same commit as `main`.

**Step 3: Verify the baseline tests**

Run:

```bash
uv run pytest -q tests/test_capabilities.py tests/test_config.py tests/test_kanban_handoff.py
```

Expected: all selected tests pass before implementation.

---

### Task 2: Define the PromptOpt case contract and balanced corpus

**Objective:** Create distinct training and held-out validation suites with machine-checkable routing expectations.

**Files:**
- Create: `evals/voice_kanban_promptopt/train.json`
- Create: `evals/voice_kanban_promptopt/validation.json`
- Create: `src/realtime_action_spike/prompt_eval.py`
- Test: `tests/test_prompt_eval.py`

**Step 1: Write failing schema/scorer tests**

Cover:

```python
def test_case_requires_distinct_id_turns_and_expected_action(): ...
def test_suite_rejects_duplicate_case_ids(): ...
def test_suite_rejects_train_validation_utterance_overlap(): ...
def test_tool_call_score_requires_direct_task_and_preserved_constraints(): ...
def test_spoken_confirmation_score_rejects_premature_handoff(): ...
def test_literal_kanban_subject_is_not_treated_as_routing_wrapper(): ...
```

The scorer should classify one of:

```python
Literal["handoff", "ask_handoff", "ask_consequence", "direct_tool", "answer", "refuse"]
```

Each case records:
- `id`
- one or more user `turns`
- expected action per evaluated turn
- expected tool name when applicable
- required/forbidden task substrings
- routing-wrapper exclusions
- tags

**Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest -q tests/test_prompt_eval.py
```

Expected: collection/import failure because `prompt_eval.py` does not exist.

**Step 3: Implement minimal typed case loading and scoring**

Use strict Pydantic models. Do not invoke a model or execute a tool in this module. Return structured per-case results with hard pass/fail and a bounded quality score.

**Step 4: Add 12 training and 12 held-out cases**

Balance both files across:
- explicit Kanban/Hermes, harmless
- explicit Kanban/Hermes, consequential
- unsupported but Hermes-capable
- direct lightweight tool
- simple answer
- impossible/unsafe refusal
- literal Kanban/Hermes subject matter
- constraint-heavy wrapper removal

Training and validation must differ in domain, wording, and wrapper position—not paraphrases.

**Step 5: Run tests to verify GREEN**

Run:

```bash
uv run pytest -q tests/test_prompt_eval.py
```

Expected: all scorer and corpus-integrity tests pass.

**Step 6: Commit**

```bash
git add evals/voice_kanban_promptopt src/realtime_action_spike/prompt_eval.py tests/test_prompt_eval.py
git commit -m "test: define voice Kanban PromptOpt corpus"
```

---

### Task 3: Build the no-side-effect Realtime rollout runner

**Objective:** Execute corpus turns against the actual Realtime model while recording decisions without running any capability or creating Kanban cards.

**Files:**
- Create: `scripts/run_voice_kanban_promptopt.py`
- Modify: `src/realtime_action_spike/prompt_eval.py`
- Test: `tests/test_prompt_eval.py`

**Step 1: Write failing protocol parsing tests**

Cover fixtures for:
- `session.created` then `session.updated`
- text response in `response.done.response.output`
- function call output containing `name` and JSON `arguments`
- provider `error` event tied to a client `event_id`
- timeout before `response.done`
- no API key: explicit skip/error without exposing secret text

**Step 2: Run tests to verify RED**

Run:

```bash
uv run pytest -q tests/test_prompt_eval.py -k realtime
```

Expected: FAIL because the rollout protocol parser/client does not exist.

**Step 3: Implement the runner**

Use the current official contract:

```json
{"type":"conversation.item.create","item":{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}}
{"type":"response.create","response":{"output_modalities":["text"]}}
```

Connect to:

```text
wss://api.openai.com/v1/realtime?model=<configured model>
```

Send a `session.update` containing:
- `type: realtime`
- candidate instructions
- text output modality
- exact production tools
- `tool_choice: auto`

Requirements:
- never execute returned function calls
- one fresh session per case to prevent cross-case contamination
- bounded connection/response timeouts
- artifacts contain candidate hash, model, case ID, output classification, arguments, elapsed time, and errors
- never write API keys or full environment data

**Step 4: Add deterministic offline runner tests**

Inject a fake WebSocket/event stream; do not call OpenAI during normal pytest.

**Step 5: Run tests to verify GREEN**

Run:

```bash
uv run pytest -q tests/test_prompt_eval.py
uv run ruff check scripts/run_voice_kanban_promptopt.py src/realtime_action_spike/prompt_eval.py tests/test_prompt_eval.py
```

Expected: all tests and lint pass.

**Step 6: Commit**

```bash
git add scripts/run_voice_kanban_promptopt.py src/realtime_action_spike/prompt_eval.py tests/test_prompt_eval.py
git commit -m "feat: add Realtime PromptOpt rollout runner"
```

---

### Task 4: Measure the current production prompt baseline

**Objective:** Capture evidence of current routing/tool-argument failures before editing production behavior.

**Files:**
- Generated, untracked: `artifacts/promptopt/voice-kanban/baseline-training.json`
- Generated, untracked: `artifacts/promptopt/voice-kanban/baseline-validation.json`

**Step 1: Verify the active model and API key without printing the key**

Run the runner's preflight mode:

```bash
uv run python scripts/run_voice_kanban_promptopt.py --preflight --model gpt-realtime-2.1
```

Expected: model reachable and credentials available; no secret output.

**Step 2: Run the training baseline**

```bash
uv run python scripts/run_voice_kanban_promptopt.py \
  --suite evals/voice_kanban_promptopt/train.json \
  --output artifacts/promptopt/voice-kanban/baseline-training.json \
  --model gpt-realtime-2.1
```

Expected: 12 completed cases with scored decisions and no executed tools.

**Step 3: Freeze the held-out baseline**

```bash
uv run python scripts/run_voice_kanban_promptopt.py \
  --suite evals/voice_kanban_promptopt/validation.json \
  --output artifacts/promptopt/voice-kanban/baseline-validation.json \
  --model gpt-realtime-2.1
```

Expected: 12 completed held-out cases. Do not use validation failures to author case-specific wording; retain them for candidate comparison.

**Step 4: Summarize failure families**

Produce a compact artifact summary grouped by:
- missed explicit handoff
- premature unsupported handoff
- missing consequential confirmation
- routing-wrapper leakage
- constraint loss
- false-positive handoff

Do not commit API outputs containing private task text unless reviewed and sanitized.

---

### Task 5: Migrate the handoff contract to a direct task

**Objective:** Make malformed routing wrappers invalid and make the Kanban card objective transport-neutral.

**Files:**
- Modify: `src/realtime_action_spike/capabilities.py:48-53,200-249,316-340,414-423`
- Modify: `tests/test_kanban_handoff.py`
- Modify: `tests/test_capabilities.py`

**Step 1: Write failing contract tests**

Add tests that require:

```python
CapabilityBroker().execute(
    "handoff_to_heavy_agent",
    {"task": "Change the desktop wallpaper and preserve the old path."},
)
```

Assertions:
- old `request` field is rejected
- title is `Change the desktop wallpaper and preserve the old path.` with no `Voice handoff:` prefix
- body uses `Task:` and preserves the exact detailed task
- obvious wrappers (`Have Hermes...`, `Add a Kanban task to...`, `Put this request on Kanban...`) raise `ExecutionContractError` before subprocess execution
- legitimate work such as `Build a Kanban dashboard` and `Audit Hermes Agent configuration` remains valid

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/test_kanban_handoff.py tests/test_capabilities.py -k "handoff or wrapper"
```

Expected: failures because the current schema still requires `request` and prefixes titles.

**Step 3: Implement the minimal contract migration**

- Rename `HermesAgentArguments.request` to `task`.
- Add a narrow Pydantic field validator using anchored, case-insensitive routing-prefix patterns.
- Keep the maximum at 4,000 characters.
- Rename internal parameters from `request` to `task`.
- `_kanban_title(task)` returns normalized task text truncated to the existing 80-character bound.
- Card body retains `Voice handoff from Okay Hermes Realtime` as provenance and labels the objective `Task:`.
- Do not add a general-purpose rewriter.

**Step 4: Run tests to verify GREEN**

```bash
uv run pytest -q tests/test_kanban_handoff.py tests/test_capabilities.py
uv run ruff check src/realtime_action_spike/capabilities.py tests/test_kanban_handoff.py tests/test_capabilities.py
```

Expected: all selected tests pass.

**Step 5: Commit**

```bash
git add src/realtime_action_spike/capabilities.py tests/test_kanban_handoff.py tests/test_capabilities.py
git commit -m "feat: require direct tasks for Kanban handoff"
```

---

### Task 6: Optimize and apply the routing prompt/tool wording

**Objective:** Use baseline failures to make bounded wording edits that implement explicit routing and consent semantics.

**Files:**
- Modify: `src/realtime_action_spike/config.py:13-39`
- Modify: `src/realtime_action_spike/capabilities.py:48-53,414-423`
- Modify: `tests/test_ui_contract.py` or `tests/test_gateway.py` for serialized session contract
- Modify: `tests/test_capabilities.py`

**Step 1: Write failing static contract tests**

Assert the generated session/tool contract communicates:
- explicit Kanban/Hermes requests route to `handoff_to_heavy_agent`
- explicit consequential requests require confirmation
- unsupported-but-plausible requests offer handoff and wait for consent
- impossible/unsafe requests are not delegated
- the tool's `task` argument must exclude routing language and preserve constraints
- direct/simple tasks remain direct

These tests guard required concepts, not one exact paragraph, so later PromptOpt edits remain possible.

**Step 2: Run tests to verify RED**

```bash
uv run pytest -q tests/test_capabilities.py tests/test_gateway.py tests/test_ui_contract.py -k "handoff or instruction or session"
```

Expected: at least the new routing-policy assertions fail against current wording.

**Step 3: Apply no more than four bounded text edits**

Use the baseline reflection, not intuition alone. Prefer clear bullet rules and labeled sections per the official Realtime prompting guide. Keep the prompt concise and avoid duplicated policy between system instructions and tool description.

**Step 4: Run static tests to verify GREEN**

```bash
uv run pytest -q tests/test_capabilities.py tests/test_gateway.py tests/test_ui_contract.py
uv run ruff check src/realtime_action_spike/config.py src/realtime_action_spike/capabilities.py
```

Expected: all selected tests pass.

**Step 5: Commit the candidate**

```bash
git add src/realtime_action_spike/config.py src/realtime_action_spike/capabilities.py tests
git commit -m "feat: route consented voice work to Hermes"
```

---

### Task 7: Run held-out validation and enforce the SkillOpt gate

**Objective:** Prove the candidate does not regress routing behavior before deployment.

**Files:**
- Generated, untracked: `artifacts/promptopt/voice-kanban/candidate-validation.json`
- Generated, untracked: `artifacts/promptopt/voice-kanban/comparison.json`
- Modify only if rejected: bounded prompt/schema candidate text and its static tests

**Step 1: Run candidate training for diagnosis**

```bash
uv run python scripts/run_voice_kanban_promptopt.py \
  --suite evals/voice_kanban_promptopt/train.json \
  --output artifacts/promptopt/voice-kanban/candidate-training.json \
  --model gpt-realtime-2.1
```

Expected: improvement in observed failure families.

**Step 2: Run held-out validation once per candidate**

```bash
uv run python scripts/run_voice_kanban_promptopt.py \
  --suite evals/voice_kanban_promptopt/validation.json \
  --output artifacts/promptopt/voice-kanban/candidate-validation.json \
  --model gpt-realtime-2.1 \
  --compare artifacts/promptopt/voice-kanban/baseline-validation.json
```

Gate:
- reject any lower hard pass rate
- at equal pass rate, reject material quality regression
- prefer shorter prompt/schema text when behavior is equal

**Step 3: If rejected, make one bounded candidate revision**

Return to the failure family, modify no more than four text clauses, rerun static tests, then rerun training and held-out validation. Do not weaken cases to make a candidate pass.

**Step 4: Record accepted metrics**

Store sanitized comparison metrics in a small committed report only if it contains no private text or credentials. Otherwise report aggregate metrics in the commit message/final response and keep raw artifacts ignored.

---

### Task 8: Run full verification and independent review

**Objective:** Verify application behavior beyond the PromptOpt cases and catch security/lifecycle regressions.

**Files:**
- Potential fixes limited to findings in touched surfaces

**Step 1: Run all quality gates**

```bash
uv run ruff check .
uv run pytest -q
npm run test:web
npm run check:web
uv build
git diff --check
```

Expected: all commands pass.

**Step 2: Smoke-install the wheel in an isolated temporary venv**

Verify the wheel includes evaluator-independent production modules and web assets. The production package must not require eval artifacts at runtime.

**Step 3: Request read-only review**

Review focus:
- wrapper validator false positives
- constraint preservation
- consequential confirmation semantics
- no tool execution in evaluator
- secret redaction
- Realtime event parsing/timeouts
- no regression to task-event completion relay

**Step 4: Fix verified findings with regression tests**

Use TDD for every accepted finding, then rerun the full gates.

---

### Task 9: Merge, deploy, restart, and perform live voice E2E

**Objective:** Put the accepted behavior into the actual running controller and verify a real card contains the direct objective.

**Files:**
- No new source files unless live evidence reveals a reproducible bug

**Step 1: Merge locally into `main`**

Use a fast-forward merge after confirming the base checkout has only the known pre-existing untracked paths.

**Step 2: Re-run merged quality gates**

At minimum:

```bash
uv run ruff check .
uv run pytest -q
npm run test:web
npm run check:web
```

Expected: all pass from `main`.

**Step 3: Build and install the exact merged wheel**

```bash
uv build
uv pip install \
  --python /home/neos/.local/share/okay-hermes-realtime/venv/bin/python \
  --force-reinstall \
  dist/openai_realtime_action_spike-0.1.0-py3-none-any.whl
systemctl --user restart okay-hermes-realtime-controller.service
```

**Step 4: Verify the live runtime**

Check:
- systemd service active with a new PID/timestamp
- `/health` includes all five capabilities
- installed schema exposes `task`, not `request`
- installed prompt contains explicit routing and consent rules
- live `/assets/voice.js` still contains task-event safe-turn behavior

**Step 5: Run real voice scenarios**

1. Explicit harmless route: “Put this on Kanban: research whether my GPU supports AV1 encoding.”
   - Expected: immediate handoff.
   - Inspect the created card: title/task says the research objective, not “Put this on Kanban” or “Have Hermes.”
2. Unsupported request: ask for work the realtime tools cannot do.
   - Expected: voice asks whether to hand it to Hermes; no card exists before approval.
3. Consequential explicit route: ask Hermes to perform a clearly destructive scoped action in a harmless test fixture.
   - Expected: voice names the consequence and asks; no card before confirmation.
4. Let one safe task complete.
   - Expected: completion is spoken on the next safe turn without interrupting the user.

**Step 6: Clean up**

Remove the feature worktree and delete the merged feature branch. Preserve raw eval artifacts only if intentionally retained and scrubbed; do not commit credentials or private transcripts.
