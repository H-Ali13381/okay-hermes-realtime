#!/usr/bin/env python3
"""Run text-only voice Kanban PromptOpt cases against OpenAI Realtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from realtime_action_spike.capabilities import build_openai_tools
from realtime_action_spike.config import DEFAULT_INSTRUCTIONS
from realtime_action_spike.prompt_eval import (
    PromptOptCase,
    dump_promptopt_result,
    load_promptopt_split,
)
from realtime_action_spike.prompt_eval_realtime import (
    PromptCandidate,
    RealtimePromptEvaluator,
)

_DEFAULT_MODEL = "gpt-realtime-2.1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--candidate-name", default="production-baseline")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--preflight", action="store_true")
    return parser


def _settings(env_file: Path | None, requested_model: str | None) -> tuple[str, str]:
    file_values: dict[str, str | None] = {}
    if env_file is not None:
        if not env_file.is_file():
            raise ValueError(f"environment file not found: {env_file}")
        file_values = dict(dotenv_values(env_file))
    api_key = os.environ.get("OPENAI_API_KEY") or file_values.get("OPENAI_API_KEY") or ""
    model = (
        requested_model
        or os.environ.get("REALTIME_MODEL")
        or file_values.get("REALTIME_MODEL")
        or _DEFAULT_MODEL
    )
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not available")
    return api_key, model


def _candidate(name: str) -> PromptCandidate:
    return PromptCandidate(
        name=name,
        instructions=DEFAULT_INSTRUCTIONS,
        tools=build_openai_tools(),
    )


def _preflight_case() -> PromptOptCase:
    return PromptOptCase.model_validate(
        {
            "id": "preflight-simple-answer",
            "user_turns": ["Reply with the single word okay."],
            "expected_action": "answer",
            "expected_tool": None,
            "required_task_substrings": [],
            "forbidden_task_substrings": [],
            "routing_wrapper_exclusions": [],
            "tags": ["simple_answer"],
        }
    )


def _error_text(error: Exception) -> str:
    return " ".join(str(error).split())[:512]


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    api_key, model = _settings(args.env_file, args.model)
    candidate = _candidate(args.candidate_name)
    evaluator = RealtimePromptEvaluator(
        api_key=api_key,
        model=model,
        timeout_seconds=args.timeout,
    )

    cases = [_preflight_case()] if args.preflight else load_promptopt_split(args.suite).cases
    results: list[dict[str, Any]] = []
    provider_errors = 0
    for case in cases:
        try:
            result = await evaluator.rollout(case, candidate)
        except Exception as error:
            provider_errors += 1
            results.append(
                {
                    "case_id": case.id,
                    "candidate_name": candidate.name,
                    "candidate_hash": candidate.contract_hash,
                    "model": model,
                    "provider_error": _error_text(error),
                    "hard_pass": False,
                }
            )
            continue
        payload = result.model_dump(mode="json")
        payload["hard_pass"] = result.score.hard_pass
        results.append(payload)

    completed = len(results) - provider_errors
    passed = sum(bool(result.get("hard_pass")) for result in results)
    quality_scores = [
        float(result["score"]["quality_score"])
        for result in results
        if isinstance(result.get("score"), dict)
    ]
    artifact = {
        "candidate_name": candidate.name,
        "candidate_hash": candidate.contract_hash,
        "model": model,
        "case_count": len(cases),
        "completed_count": completed,
        "provider_error_count": provider_errors,
        "hard_pass_count": passed,
        "hard_pass_rate": passed / len(cases),
        "mean_quality_score": (
            sum(quality_scores) / len(quality_scores) if quality_scores else 0.0
        ),
        "results": results,
    }
    return artifact, 2 if provider_errors else 0


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if not args.preflight and args.suite is None:
        parser.error("--suite is required unless --preflight is used")
    if not args.preflight and args.output is None:
        parser.error("--output is required unless --preflight is used")

    try:
        artifact, exit_code = asyncio.run(_run(args))
    except ValueError as error:
        parser.error(str(error))

    if args.output is not None:
        dump_promptopt_result(args.output, artifact)
    print(
        json.dumps(
            {
                key: artifact[key]
                for key in (
                    "candidate_name",
                    "candidate_hash",
                    "model",
                    "case_count",
                    "completed_count",
                    "provider_error_count",
                    "hard_pass_count",
                    "hard_pass_rate",
                    "mean_quality_score",
                )
            },
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
