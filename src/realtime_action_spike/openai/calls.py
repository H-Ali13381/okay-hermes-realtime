from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_OPENAI_REALTIME_ANSWER_BYTES = 128_000
MAX_OPENAI_REALTIME_CALL_ID_LENGTH = 256

_CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
_CALL_LOCATION_PREFIX = "/v1/realtime/calls/"


@dataclass(frozen=True, slots=True)
class RealtimeCallHandle:
    call_id: str
    request_id: str | None
    sdp_answer: str


class RealtimeCallHandleParseError(ValueError):
    """Raised when a provider response cannot be converted into a local call handle."""


def parse_realtime_call_handle(
    *,
    location: str | None,
    headers: Mapping[str, str],
    sdp_answer: str,
) -> RealtimeCallHandle:
    if not location:
        raise RealtimeCallHandleParseError("OpenAI Realtime call response is missing Location")

    call_id = _parse_location(location=location)
    request_id = _extract_request_id(headers)
    answer = _validate_answer(sdp_answer)

    return RealtimeCallHandle(
        call_id=call_id,
        request_id=request_id,
        sdp_answer=answer,
    )


def _extract_request_id(headers: Mapping[str, str]) -> str | None:
    request_id = None
    for key, value in headers.items():
        if key.lower() == "x-request-id":
            request_id = value
            break

    if request_id is None:
        return None

    request_id = request_id.strip()
    if not request_id:
        return None

    if _contains_control_chars(request_id) or " " in request_id:
        return None

    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        return None

    return request_id


def _parse_location(*, location: str) -> str:
    if location.strip() != location or _contains_control_chars(location):
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if "%" in location:
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    parsed = urlsplit(location)

    if parsed.scheme:
        if parsed.scheme.lower() != "https":
            raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

        if parsed.hostname != "api.openai.com":
            raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

        if parsed.username or parsed.password:
            raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

        if parsed.port is not None:
            raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    elif parsed.netloc:
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if parsed.query or parsed.fragment:
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if not parsed.path.startswith(_CALL_LOCATION_PREFIX):
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if parsed.path.endswith("/"):
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    path_suffix = parsed.path[len(_CALL_LOCATION_PREFIX) :]
    if "/" in path_suffix:
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if not path_suffix:
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if path_suffix in {".", ".."}:
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if path_suffix == "..":
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if len(path_suffix) > MAX_OPENAI_REALTIME_CALL_ID_LENGTH:
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if not _CALL_ID_PATTERN.fullmatch(path_suffix):
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if parsed.path == _CALL_LOCATION_PREFIX + "/":
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    if parsed.path == "/v1/realtime/calls/." or parsed.path == "/v1/realtime/calls/..":
        raise RealtimeCallHandleParseError("OpenAI Realtime call Location is invalid")

    return path_suffix


def _validate_answer(answer: str) -> str:
    if answer == "":
        raise RealtimeCallHandleParseError("OpenAI Realtime SDP answer is invalid")

    if len(answer) > MAX_OPENAI_REALTIME_ANSWER_BYTES:
        raise RealtimeCallHandleParseError("OpenAI Realtime SDP answer is invalid")

    return answer


def _contains_control_chars(value: str) -> bool:
    return any(ord(character) <= 31 or ord(character) == 127 for character in value)


__all__ = [
    "MAX_OPENAI_REALTIME_ANSWER_BYTES",
    "MAX_OPENAI_REALTIME_CALL_ID_LENGTH",
    "RealtimeCallHandle",
    "RealtimeCallHandleParseError",
    "parse_realtime_call_handle",
]
