from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from realtime_action_spike.openai.calls import (
    MAX_OPENAI_REALTIME_ANSWER_BYTES,
    RealtimeCallHandle,
    RealtimeCallHandleParseError,
    parse_realtime_call_handle,
)


def test_realtime_call_handle_is_frozen_slots() -> None:
    handle = RealtimeCallHandle(
        call_id="call_123",
        request_id="req_123",
        sdp_answer="v=0\r\na=answer",
    )

    assert handle.call_id == "call_123"
    assert handle.request_id == "req_123"
    assert handle.sdp_answer == "v=0\r\na=answer"
    assert "__dict__" not in dir(handle)

    with pytest.raises(FrozenInstanceError):
        handle.call_id = "different"


@pytest.mark.parametrize(
    ("location", "expected_call_id", "expected_request_id"),
    [
        ("/v1/realtime/calls/call_abc-123", "call_abc-123", "req_1"),
        (
            "https://api.openai.com/v1/realtime/calls/call_abc-123",
            "call_abc-123",
            "req_2",
        ),
    ],
)
def test_parse_realtime_call_handle_accepts_supported_locations(
    location: str,
    expected_call_id: str,
    expected_request_id: str,
) -> None:
    parsed = parse_realtime_call_handle(
        location=location,
        headers={"x-request-id": f"  {expected_request_id}  "},
        sdp_answer="v=0\r\nmock-answer",
    )

    assert parsed.call_id == expected_call_id
    assert parsed.request_id == expected_request_id
    assert parsed.sdp_answer == "v=0\r\nmock-answer"


@pytest.mark.parametrize(
    "location",
    [
        None,
        "",
        " /v1/realtime/calls/call_123",
        "/v1/realtime/calls/call_123/",
        "/v1/realtime/calls",
        "/v1/realtime/calls/",
        "/v1/realtime/calls/call_1/extra",
        "/v1/realtime/calls/call_1%2Fextra",
        "/v1/realtime/calls/%2Fcall_1",
        "/v1/realtime//calls/call_1",
        "/v1/realtime/calls/./call_1",
        "https://api.openai.com/v1/realtime/calls/call_1?x=1",
        "https://api.openai.com/v1/realtime/calls/call_1#x",
        "http://api.openai.com/v1/realtime/calls/call_1",
        "https://api.openai.com:443/v1/realtime/calls/call_1",
        "https://api.openai.com:9443/v1/realtime/calls/call_1",
        "https://api.openai.com/v1/realtime/other/call_1",
        "https://api.openai.com/v1/realtime/calls/call_1/../bad",
        "https://user:pass@api.openai.com/v1/realtime/calls/call_1",
        "//api.openai.com/v1/realtime/calls/call_proto_relative",
        "https://api.openai.com/v1/realtime/calls/call-\x01bad",
    ],
)
def test_parse_realtime_call_handle_rejects_invalid_location_headers(
    location: str,
) -> None:
    with pytest.raises(RealtimeCallHandleParseError) as exc_info:
        parse_realtime_call_handle(location=location, headers={}, sdp_answer="v=0\r\nmock-answer")

    message = str(exc_info.value)
    assert "call_" not in message
    assert "mock-answer" not in message


def test_parse_realtime_call_handle_rejects_overlong_call_id() -> None:
    overlong_id = "c" * 257
    location = f"/v1/realtime/calls/{overlong_id}"

    with pytest.raises(RealtimeCallHandleParseError):
        parse_realtime_call_handle(location=location, headers={}, sdp_answer="v=0\r\nmock-answer")


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"x-request-id": "  req_whitespace  "}, "req_whitespace"),
        ({"X-Request-ID": "req_case_folded"}, "req_case_folded"),
        ({"Other-Header": "ignored"}, None),
        ({"x-request-id": "bad\x00value"}, None),
    ],
)
def test_parse_realtime_call_handle_extracts_only_official_request_id(
    header: dict[str, str],
    expected: str | None,
) -> None:
    parsed = parse_realtime_call_handle(
        location="/v1/realtime/calls/call_abc",
        headers=header,
        sdp_answer="v=0\r\nmock-answer",
    )

    assert parsed.request_id == expected


def test_parse_realtime_call_handle_preserves_exact_sdp_answer() -> None:
    sdp = "v=0\r\nmock-answer-with-tricky-spaces   \r\nline2"

    parsed = parse_realtime_call_handle(
        location="/v1/realtime/calls/call_preserve",
        headers={"x-request-id": "req_preserve"},
        sdp_answer=sdp,
    )

    assert parsed.sdp_answer == sdp


def test_parse_realtime_call_handle_rejects_empty_sdp() -> None:
    with pytest.raises(RealtimeCallHandleParseError):
        parse_realtime_call_handle(
            location="/v1/realtime/calls/call_empty",
            headers={"x-request-id": "req_empty"},
            sdp_answer="",
        )


def test_parse_realtime_call_handle_rejects_unbounded_sdp() -> None:
    with pytest.raises(RealtimeCallHandleParseError):
        parse_realtime_call_handle(
            location="/v1/realtime/calls/call_too_long",
            headers={"x-request-id": "req_long"},
            sdp_answer="v" * (MAX_OPENAI_REALTIME_ANSWER_BYTES + 1),
        )


def test_parse_realtime_call_handle_rejects_malformed_call_id() -> None:
    with pytest.raises(RealtimeCallHandleParseError) as exc_info:
        parse_realtime_call_handle(
            location="/v1/realtime/calls/call#id",
            headers={"x-request-id": "req_malformed"},
            sdp_answer="v=0\r\nmock-answer",
        )

    assert "call#id" not in str(exc_info.value)
    assert "SECRET-API-KEY" not in str(exc_info.value)

