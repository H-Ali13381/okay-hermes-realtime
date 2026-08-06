from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from realtime_action_spike.capability_services import (
    ActionNotImplementedError,
    HermesTaskActions,
    HermesTaskReference,
    HermesTaskStartRequest,
    MediaActions,
    MediaPlayRequest,
    MediaSkipRequest,
    TimerActions,
    TimerCancelRequest,
    TimerStartRequest,
)


def test_request_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TimerStartRequest.model_validate(
            {"duration_seconds": 60, "label": "tea", "unexpected": True}
        )


def test_timer_duration_uses_existing_one_day_bound() -> None:
    with pytest.raises(ValidationError):
        TimerStartRequest(duration_seconds=0)

    with pytest.raises(ValidationError):
        TimerStartRequest(duration_seconds=86_401)

    assert TimerStartRequest(duration_seconds=86_400).duration_seconds == 86_400


def test_media_and_task_requests_preserve_existing_contract_bounds() -> None:
    with pytest.raises(ValidationError):
        MediaPlayRequest(query="")

    with pytest.raises(ValidationError):
        HermesTaskStartRequest(task="")

    with pytest.raises(ValidationError):
        HermesTaskReference(task_id="")


def unfinished_actions() -> list[tuple[str, Callable[[], object]]]:
    timer = TimerActions()
    media = MediaActions()
    hermes = HermesTaskActions()
    return [
        (
            "timer.start",
            lambda: timer.start(TimerStartRequest(duration_seconds=60, label="tea")),
        ),
        ("timer.cancel", lambda: timer.cancel(TimerCancelRequest())),
        (
            "media.play",
            lambda: media.play(MediaPlayRequest(query="Daft Punk")),
        ),
        ("media.pause", media.pause),
        ("media.resume", media.resume),
        (
            "media.skip",
            lambda: media.skip(MediaSkipRequest(direction="next")),
        ),
        (
            "hermes.task.start",
            lambda: hermes.start(HermesTaskStartRequest(task="Research local VAD")),
        ),
        (
            "hermes.task.cancel",
            lambda: hermes.cancel(HermesTaskReference(task_id="task-123")),
        ),
        (
            "hermes.task.status",
            lambda: hermes.status(HermesTaskReference(task_id="task-123")),
        ),
    ]


@pytest.mark.parametrize(("action", "invoke"), unfinished_actions())
def test_unfinished_action_methods_fail_explicitly(
    action: str,
    invoke: Callable[[], object],
) -> None:
    with pytest.raises(ActionNotImplementedError, match=rf"not implemented: {action}"):
        invoke()
