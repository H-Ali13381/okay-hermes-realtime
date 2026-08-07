from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from realtime_action_spike.prompt_eval import (
    PromptOptAction,
    PromptOptCase,
    PromptOptPrediction,
    load_promptopt_corpus,
    load_promptopt_split,
    score_promptopt_case,
)


def _case(**overrides: object) -> dict[str, object]:
    case: dict[str, object] = {
        "id": "case-001",
        "user_turns": ["Please start a five minute tea timer."],
        "expected_action": PromptOptAction.direct_tool,
        "expected_tool": "timer.start",
        "required_task_substrings": ["five minute", "tea"],
        "forbidden_task_substrings": ["heavy agent", "Kanban"],
        "routing_wrapper_exclusions": ["routing assistant", "hand off this request"],
        "tags": ["direct_lightweight_tool"],
    }
    case.update(overrides)
    return case


def test_promptopt_case_schema_is_strict() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PromptOptCase.model_validate({**_case(), "unexpected": True})

    for required_field in (
        "expected_tool",
        "required_task_substrings",
        "forbidden_task_substrings",
        "routing_wrapper_exclusions",
    ):
        payload = _case()
        payload.pop(required_field)
        with pytest.raises(ValidationError, match=required_field):
            PromptOptCase.model_validate(payload)


@pytest.mark.parametrize(
    ("expected_action", "expected_tool"),
    [
        ("handoff", None),
        ("direct_tool", None),
        ("answer", "assistant_get_current_time"),
        ("ask_handoff", "handoff_to_heavy_agent"),
        ("ask_consequence", "handoff_to_heavy_agent"),
        ("refuse", "handoff_to_heavy_agent"),
    ],
)
def test_promptopt_case_rejects_inconsistent_action_tool_contract(
    expected_action: str,
    expected_tool: str | None,
) -> None:
    with pytest.raises(ValidationError, match="expected_tool"):
        PromptOptCase.model_validate(
            _case(expected_action=expected_action, expected_tool=expected_tool)
        )


def test_promptopt_split_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    payload = {
        "cases": [
            _case(
                id="dup-1",
                user_turns=["A"],
                tags=["simple_answer"],
                expected_action="answer",
                expected_tool=None,
            ),
            _case(
                id="dup-1",
                user_turns=["B"],
                tags=["simple_answer"],
                expected_action="answer",
                expected_tool=None,
            ),
        ]
    }
    path = tmp_path / "train.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="duplicate case id"):
        load_promptopt_split(path)


def test_promptopt_split_rejects_duplicate_normalized_utterances(tmp_path: Path) -> None:
    path = tmp_path / "train.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    _case(id="first", user_turns=["  Same spoken request  "]),
                    _case(id="second", user_turns=["same SPOKEN request"]),
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="duplicate utterance within split"):
        load_promptopt_split(path)


def test_promptopt_corpus_rejects_cross_split_duplicate_ids(tmp_path: Path) -> None:
    train_path = tmp_path / "train.json"
    validation_path = tmp_path / "validation.json"
    train_path.write_text(
        json.dumps({"cases": [_case(id="shared-id", user_turns=["training turn"])]}),
        encoding="utf-8",
    )
    validation_path.write_text(
        json.dumps({"cases": [_case(id="shared-id", user_turns=["validation turn"])]}),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="duplicate case id across splits"):
        load_promptopt_corpus(train_path=train_path, validation_path=validation_path)


def test_promptopt_corpus_rejects_train_validation_utterance_overlap(tmp_path: Path) -> None:
    train_path = tmp_path / "train.json"
    validation_path = tmp_path / "validation.json"
    shared_turn = "What does the Kanban board mean in my card game?"
    train_path.write_text(
        json.dumps(
            {
                "cases": [
                    _case(
                        id="train-1",
                        user_turns=[shared_turn],
                        expected_action="answer",
                        expected_tool=None,
                        tags=["literal_kanban_hermes_subject_matter"],
                        required_task_substrings=[],
                        forbidden_task_substrings=["heavy agent"],
                        routing_wrapper_exclusions=["handoff"],
                    )
                ]
            }
        ),
        encoding="utf-8",
    )
    validation_path.write_text(
        json.dumps(
            {
                "cases": [
                    _case(
                        id="validation-1",
                        user_turns=[shared_turn],
                        expected_action="answer",
                        expected_tool=None,
                        tags=["literal_kanban_hermes_subject_matter"],
                        required_task_substrings=[],
                        forbidden_task_substrings=["heavy agent"],
                        routing_wrapper_exclusions=["handoff"],
                    )
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="utterance overlap"):
        load_promptopt_corpus(train_path=train_path, validation_path=validation_path)


def test_direct_tool_scoring_requires_direct_task_and_preserves_constraints() -> None:
    case = PromptOptCase.model_validate(
        {
            "id": "tool-1",
            "user_turns": ["Please set a five minute tea timer."],
            "expected_action": "direct_tool",
            "expected_tool": "timer.start",
            "required_task_substrings": ["five minute", "tea"],
            "forbidden_task_substrings": ["heavy agent", "Kanban"],
            "routing_wrapper_exclusions": ["routing assistant", "hand off this request"],
            "tags": ["constraint_heavy_wrapper_removal"],
        }
    )

    passing = score_promptopt_case(
        case,
        PromptOptPrediction.model_validate(
            {
                "action": "direct_tool",
                "tool_name": "timer.start",
                "task": "Set a five minute tea timer.",
            }
        ),
    )
    assert passing.hard_pass is True
    assert passing.quality_score == pytest.approx(1.0)

    failing = score_promptopt_case(
        case,
        PromptOptPrediction.model_validate(
            {
                "action": "direct_tool",
                "tool_name": "timer.start",
                "task": "You are a routing assistant. Hand off this request to a heavy agent.",
            }
        ),
    )
    assert failing.hard_pass is False
    assert failing.forbidden_task_hits == ["heavy agent"]
    assert "routing assistant" in failing.routing_wrapper_hits


def test_confirmation_scoring_rejects_premature_handoff() -> None:
    case = PromptOptCase.model_validate(
        {
            "id": "confirm-1",
            "user_turns": [
                "Before you delete the shared folder, tell me exactly what will disappear."
            ],
            "expected_action": "ask_consequence",
            "expected_tool": None,
            "required_task_substrings": [],
            "forbidden_task_substrings": ["hand off"],
            "routing_wrapper_exclusions": ["handoff_to_heavy_agent"],
            "tags": ["explicit_consequential_confirmation"],
        }
    )

    score = score_promptopt_case(
        case,
        PromptOptPrediction.model_validate(
            {
                "action": "handoff",
                "tool_name": "handoff_to_heavy_agent",
            }
        ),
    )

    assert score.hard_pass is False
    assert score.action_match is False


def test_non_tool_case_rejects_unexpected_tool_call() -> None:
    case = PromptOptCase.model_validate(
        {
            "id": "answer-with-tool",
            "user_turns": ["What does idempotent mean?"],
            "expected_action": "answer",
            "expected_tool": None,
            "required_task_substrings": [],
            "forbidden_task_substrings": [],
            "routing_wrapper_exclusions": [],
            "tags": ["simple_answer"],
        }
    )
    score = score_promptopt_case(
        case,
        PromptOptPrediction.model_validate(
            {
                "action": "answer",
                "tool_name": "handoff_to_heavy_agent",
                "assistant_text": "It means repeated application has the same effect.",
            }
        ),
    )

    assert score.hard_pass is False
    assert score.tool_match is False


def test_literal_kanban_subject_matter_is_not_mistaken_for_routing() -> None:
    case = PromptOptCase.model_validate(
        {
            "id": "literal-1",
            "user_turns": [
                "In this tabletop game's lore, what does the Kanban hero symbolize?"
            ],
            "expected_action": "answer",
            "expected_tool": None,
            "required_task_substrings": [],
            "forbidden_task_substrings": ["heavy agent", "handoff_to_heavy_agent"],
            "routing_wrapper_exclusions": ["route this", "heavy agent"],
            "tags": ["literal_kanban_hermes_subject_matter"],
        }
    )

    answering = score_promptopt_case(
        case,
        PromptOptPrediction.model_validate(
            {
                "action": "answer",
                "assistant_text": "Kanban is just the name of a hero in the lore here.",
            }
        ),
    )
    assert answering.hard_pass is True

    misrouted = score_promptopt_case(
        case,
        PromptOptPrediction.model_validate(
            {
                "action": "handoff",
                "tool_name": "handoff_to_heavy_agent",
            }
        ),
    )
    assert misrouted.hard_pass is False
    assert misrouted.action_match is False


def test_promptopt_corpus_files_are_12_case_balanced() -> None:
    corpus = load_promptopt_corpus()
    assert len(corpus.train.cases) == 12
    assert len(corpus.validation.cases) == 12

    tag_counts = Counter(
        tag
        for split in (corpus.train, corpus.validation)
        for case in split.cases
        for tag in case.tags
    )
    assert tag_counts == Counter(
        {
            "explicit_harmless_handoff": 3,
            "explicit_consequential_confirmation": 3,
            "unsupported_but_hermes_capable_consent": 3,
            "direct_lightweight_tool": 3,
            "simple_answer": 3,
            "impossible_or_unsafe_refusal": 3,
            "literal_kanban_hermes_subject_matter": 3,
            "constraint_heavy_wrapper_removal": 3,
        }
    )

    expected_tags = set(tag_counts)
    for split in (corpus.train, corpus.validation):
        split_counts = Counter(tag for case in split.cases for tag in case.tags)
        assert set(split_counts) == expected_tags
        assert max(split_counts.values()) - min(split_counts.values()) <= 1
