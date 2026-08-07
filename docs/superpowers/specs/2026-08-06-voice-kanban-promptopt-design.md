# Voice-to-Kanban Routing That Preserves the Real Task

The voice agent should treat Kanban and Hermes as execution routes—not as the task itself. This design adapts SkillOpt into an execution-based prompt/tool optimizer for `gpt-realtime-2.1`, with held-out validation before any wording reaches production.

## Decision at a glance

| Situation | Voice-agent behavior | Kanban result |
|---|---|---|
| User explicitly asks for Kanban or Hermes; no consequential side effect | Hand off immediately | One dispatched card whose title and body describe the actual work |
| User explicitly asks for Kanban or Hermes; consequential side effect | State the concrete consequence and ask once | No card until explicit confirmation; then hand off the actual task |
| Realtime agent lacks a suitable tool, but Hermes could plausibly do the work | Offer a Hermes handoff and wait | No card until the user agrees |
| Request is genuinely impossible, incoherent, or unsafe | Explain briefly; do not delegate | No card |
| Realtime agent can answer or perform it directly | Handle it directly | No card |

> **Core invariant:** routing language never becomes the task. “Put this on Kanban: change my wallpaper” becomes **“Change my wallpaper…”**, not **“Create a Kanban task…”** and not **“Have Hermes change…”**.

## What changes

| Surface | Proposed change | Why |
|---|---|---|
| Realtime system instructions | Add an explicit routing decision policy, confirmation rules, and task-rewriting invariant | Tool selection currently covers complex work but not explicit Kanban/Hermes intent or unsupported-task consent |
| Handoff tool schema | Replace generic `request` with `task`, described as the direct, self-contained work specification | The field name and current description invite the model to copy the user's routing wrapper |
| Handoff tool description | Name explicit Kanban/Hermes requests, unsupported-but-plausible requests, and consequential confirmation | Makes the selection boundary visible where the model chooses tools |
| Backend argument validation | Reject obvious routing wrappers and return a corrective error; never silently rewrite natural language | Prevents malformed cards without risking destructive or misleading parser guesses |
| Kanban title | Derive the title from the direct task; remove the `Voice handoff:` title prefix | Keeps the board focused on work rather than transport metadata |
| Card body | Preserve full task constraints; retain voice provenance in metadata/body | Keeps context without contaminating the objective |

## Prompting policy

### Explicit routing requests

Phrases such as “put this on Kanban,” “send this to Hermes,” and “have Hermes Agent handle this” are routing intent. The model extracts the work after or around that phrase and submits only the work.

- Non-consequential work: call the handoff tool immediately.
- Consequential work: ask one short confirmation naming the actual consequence, then call only after explicit confirmation.
- Do not ask a redundant “Do you want me to use Kanban?” when the user already requested that route.

### Unsupported realtime requests

When no lightweight voice tool can perform the request but Hermes plausibly can, the voice agent says—in one sentence—that Hermes can take it and asks whether to hand it off. It creates nothing until the user agrees.

This replaces the current blanket “unavailable” response for plausible Hermes work while preserving user control.

### Truly impossible or unsafe requests

The voice agent does not create a doomed or unsafe Kanban card. It explains the limitation briefly. Safety restrictions are not converted into background work.

## Task-normalization contract

The model-facing argument is:

```json
{
  "task": "Direct, complete, self-contained description of the work itself"
}
```

Hard requirements:

1. Preserve the user's object, constraints, desired result, and verification requirements.
2. Remove only routing language: Kanban, handoff, delegation, or “have/ask/tell Hermes.”
3. Do not summarize away meaningful details.
4. Do not invent acceptance criteria the user did not imply.
5. Do not submit an empty or purely meta task.

### Examples

| User says | Tool argument `task` |
|---|---|
| “Put this on Kanban: change my wallpaper to Vi and Jinx, but save the old path.” | “Change the desktop wallpaper to Vi and Jinx artwork. Preserve the current wallpaper path so the change can be rolled back.” |
| “Have Hermes research whether this GPU supports AV1 encoding.” | “Research whether this GPU supports AV1 encoding and report the relevant codec capabilities.” |
| “Can you play this song?” when the realtime agent lacks media control | First ask whether to hand it to Hermes; after approval: “Play the requested song using the available local media controls.” |
| “Build a Kanban dashboard for my project.” | “Build a Kanban dashboard for the project.” The word *Kanban* remains because it is part of the work, not routing metadata. |
| “Ask Hermes to delete my backups.” | Ask for confirmation naming deletion of backups; after confirmation: “Delete the specified backups…” with all supplied scope constraints preserved. |

## Adapted SkillOpt loop

This is a compact PromptOpt harness derived from SkillOpt’s methodology. It optimizes application prompt/tool text rather than a `SKILL.md` file.

| Phase | Action | Artifact |
|---|---|---|
| Baseline rollout | Run current instructions and tool schema against training utterances on `gpt-realtime-2.1` | Tool decisions, arguments, spoken responses, latency, token/character estimate |
| Reflect | Group failures: missed handoff, premature handoff, missing confirmation, wrapper leakage, constraint loss | Structured failure report |
| Propose | Generate at most four bounded edits to instructions/schema wording | Candidate patches |
| Validate | Run each candidate on a distinct held-out suite | Per-case hard assertions and quality scores |
| Gate | Reject any pass-rate regression; prefer better quality and smaller prompt footprint at equal pass rate | Accepted/rejected decision |
| Port | Apply only accepted wording; keep deterministic code changes separately test-driven | Production patch and regression suite |

The existing SkillOpt repository supplies the methodology and artifact conventions. The OHV adaptation owns a small repository-local evaluator because the optimization target spans two production strings and a function schema rather than one skill document.

## Evaluation corpus

Start with at least 12 training and 12 held-out validation cases, balanced across these classes:

| Class | Required outcome |
|---|---|
| Explicit Kanban/Hermes, harmless | Immediate handoff; direct task argument |
| Explicit Kanban/Hermes, consequential | Confirmation first; no tool call before approval |
| Unsupported but Hermes-capable | Offer handoff; no tool call before consent |
| Directly supported lightweight action | Use the direct tool, not Kanban |
| Simple conversation/factual answer | Answer directly, no handoff |
| Genuinely impossible or unsafe | Brief refusal/limitation, no handoff |
| Literal Kanban/Hermes subject matter | Preserve those words when they are part of the actual objective |
| Constraint-heavy routing request | Remove wrapper while retaining every meaningful constraint |

Training and validation utterances must differ in wording, task domain, and wrapper placement. Validation cannot be paraphrases of the training set.

## Acceptance gate

A case passes only if all applicable hard assertions hold:

- Correct tool decision: handoff, direct tool, ask, answer, or refuse.
- Exactly one handoff call when delegation is expected.
- No handoff before required confirmation.
- `task` contains the work itself and excludes routing wrappers.
- User constraints survive normalization.
- No invented success claim or fabricated execution.
- Unsafe/impossible requests never become cards.

Pass rate is the hard gate. At equal pass rate, rank candidates by argument quality, brevity/naturalness of spoken output, latency, and prompt-size efficiency. A candidate that merely sounds better but changes a correct routing decision is rejected.

## Deterministic safeguards

The backend validator should catch narrow, obvious meta prefixes such as:

- “Have/ask/tell Hermes…”
- “Create/add/start a Kanban task to…”
- “Put this request on the Kanban board…”

It returns a bounded correction telling the model to resubmit the work itself. It does **not** perform free-form rewriting. This leaves semantic extraction with the model and keeps backend behavior inspectable.

Legitimate objectives such as “Build a Kanban dashboard,” “Audit Hermes Agent configuration,” or “Research Kanban workflow design” remain valid.

## Error handling and observability

- A rejected meta-task argument returns a model-visible validation error without creating a card.
- Repeated invalid calls remain bounded by the existing function-call replay and turn limits.
- Card creation/dispatch errors remain explicit tool failures; the voice agent does not claim success.
- Evaluator artifacts record prompt candidate hash, model, case ID, decision, arguments, and timing—but no secrets or raw private conversation history.
- Production logs should report classification outcomes and task IDs without storing full sensitive task text.

## Implementation boundary

In scope:

- Prompt/tool schema wording
- `request` → `task` contract migration
- Narrow meta-wrapper validator
- Direct objective titles
- PromptOpt train/validation corpus and runner
- Unit, contract, evaluator, and live smoke tests
- Rebuild, install, restart, and a real voice-to-Kanban completion test

Out of scope:

- General-purpose natural-language task rewriting
- Hermes core changes
- A second queue or notification service
- Automatic delegation of every unsupported request
- Bypassing confirmation or downstream permission gates

## Approved decisions

- Explicit routing is immediate only for non-consequential work.
- Unsupported-but-plausible work requires user consent before handoff.
- Truly impossible or unsafe work is not delegated.
- Kanban cards contain direct work objectives, not routing wrappers.
- SkillOpt is adapted as a held-out execution evaluator rather than copied unchanged.
- Backend validation rejects obvious wrappers but never guesses a rewritten objective.
