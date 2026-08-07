"""Typed corpus and deterministic scoring for voice Kanban prompt evaluations."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .routing import find_routing_wrappers


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PromptOptAction(StrEnum):
    handoff = "handoff"
    ask_handoff = "ask_handoff"
    ask_consequence = "ask_consequence"
    direct_tool = "direct_tool"
    answer = "answer"
    refuse = "refuse"


class PromptOptCase(_StrictModel):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    user_turns: list[str] = Field(min_length=1)
    expected_action: PromptOptAction
    expected_tool: str | None
    required_task_substrings: list[str]
    forbidden_task_substrings: list[str]
    routing_wrapper_exclusions: list[str]
    tags: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _action_matches_tool_contract(self) -> PromptOptCase:
        tool_actions = {PromptOptAction.handoff, PromptOptAction.direct_tool}
        if self.expected_action in tool_actions and self.expected_tool is None:
            raise ValueError("expected_tool is required for tool-producing actions")
        if self.expected_action not in tool_actions and self.expected_tool is not None:
            raise ValueError("expected_tool must be null for non-tool actions")
        return self


class PromptOptSplit(_StrictModel):
    cases: list[PromptOptCase] = Field(min_length=1)

    @model_validator(mode="after")
    def _cases_are_unique(self) -> PromptOptSplit:
        seen: set[str] = set()
        seen_turns: set[str] = set()
        for case in self.cases:
            if case.id in seen:
                raise ValueError(f"duplicate case id: {case.id}")
            seen.add(case.id)
            for turn in case.user_turns:
                normalized = " ".join(turn.casefold().split())
                if normalized in seen_turns:
                    raise ValueError(f"duplicate utterance within split: {normalized!r}")
                seen_turns.add(normalized)
        return self


class PromptOptCorpus(_StrictModel):
    train: PromptOptSplit
    validation: PromptOptSplit

    @model_validator(mode="after")
    def _splits_are_disjoint(self) -> PromptOptCorpus:
        train_ids = {case.id for case in self.train.cases}
        validation_ids = {case.id for case in self.validation.cases}
        duplicate_ids = train_ids & validation_ids
        if duplicate_ids:
            raise ValueError(
                f"duplicate case id across splits: {sorted(duplicate_ids)!r}"
            )

        train_turns = _normalized_turns(self.train)
        validation_turns = _normalized_turns(self.validation)
        overlap = train_turns & validation_turns
        if overlap:
            raise ValueError(f"train/validation utterance overlap: {sorted(overlap)!r}")
        return self


class PromptOptPrediction(_StrictModel):
    action: PromptOptAction
    tool_name: str | None = None
    task: str | None = None
    assistant_text: str | None = None


class PromptOptScore(_StrictModel):
    case_id: str
    hard_pass: bool
    quality_score: float = Field(ge=0.0, le=1.0)
    action_match: bool
    tool_match: bool
    missing_required_task_substrings: list[str]
    forbidden_task_hits: list[str]
    routing_wrapper_hits: list[str]


def _normalized_turns(split: PromptOptSplit) -> set[str]:
    return {
        " ".join(turn.casefold().split())
        for case in split.cases
        for turn in case.user_turns
    }


def load_promptopt_split(path: str | Path) -> PromptOptSplit:
    """Load one strict PromptOpt split from JSON."""

    return PromptOptSplit.model_validate_json(Path(path).read_text(encoding="utf-8"))


def load_promptopt_corpus(
    *,
    train_path: str | Path | None = None,
    validation_path: str | Path | None = None,
) -> PromptOptCorpus:
    """Load and cross-validate the repository PromptOpt corpus."""

    root = Path(__file__).resolve().parents[2]
    directory = root / "evals" / "voice_kanban_promptopt"
    train = load_promptopt_split(train_path or directory / "train.json")
    validation = load_promptopt_split(validation_path or directory / "validation.json")
    return PromptOptCorpus(train=train, validation=validation)


def _hits(text: str, candidates: list[str]) -> list[str]:
    folded = text.casefold()
    return [candidate for candidate in candidates if candidate.casefold() in folded]


def score_promptopt_case(
    case: PromptOptCase,
    prediction: PromptOptPrediction,
) -> PromptOptScore:
    """Score hard routing and direct-task invariants for one model prediction."""

    task = prediction.task or ""
    surface_text = task or prediction.assistant_text or ""
    action_match = prediction.action == case.expected_action
    tool_match = prediction.tool_name == case.expected_tool
    missing_required = [
        required
        for required in case.required_task_substrings
        if required.casefold() not in task.casefold()
    ]
    forbidden_hits = _hits(surface_text, case.forbidden_task_substrings)
    routing_hits = _hits(surface_text, case.routing_wrapper_exclusions)
    if case.expected_action is PromptOptAction.answer:
        for hit in find_routing_wrappers(surface_text):
            if hit not in routing_hits:
                routing_hits.append(hit)

    hard_checks = [
        action_match,
        tool_match,
        not missing_required,
        not forbidden_hits,
        not routing_hits,
    ]
    passed_checks = sum(hard_checks)
    return PromptOptScore(
        case_id=case.id,
        hard_pass=all(hard_checks),
        quality_score=passed_checks / len(hard_checks),
        action_match=action_match,
        tool_match=tool_match,
        missing_required_task_substrings=missing_required,
        forbidden_task_hits=forbidden_hits,
        routing_wrapper_hits=routing_hits,
    )


def dump_promptopt_result(path: str | Path, payload: BaseModel | dict[str, object]) -> None:
    """Write a stable JSON evaluation artifact."""

    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
