"""OpenAI Realtime event parser for function-call arguments streams."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class FunctionCallEventError(ValueError):
    """Raised when an OpenAI function-call event is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class FunctionCallRequest:
    """An immutable function call request extracted from stream events."""

    call_id: str
    name: str
    arguments: MappingProxyType


@dataclass(frozen=True, slots=True)
class _PartialFunctionCall:
    name: str
    fragments: tuple[str, ...]
    total_chars: int


# Provider event arguments can be large, but we intentionally cap payloads to avoid
# unbounded memory use from malformed or pathological event streams.
DEFAULT_MAX_ARGUMENT_BYTES = 16_384
DEFAULT_MAX_CALL_ID_BYTES = 256
DEFAULT_MAX_NAME_BYTES = 160


class FunctionCallEventParser:
    """Parse OpenAI Realtime events into complete function-call requests.

    The parser accepts events from two paths:
      * `response.function_call_arguments.delta` + `.done`
      * `response.done` fallback `response.output` entries

    It accumulates argument fragments per `call_id` and emits a request only once
    the call is fully specified.
    """

    def __init__(
        self,
        *,
        max_argument_bytes: int = DEFAULT_MAX_ARGUMENT_BYTES,
        max_call_id_bytes: int = DEFAULT_MAX_CALL_ID_BYTES,
        max_name_bytes: int = DEFAULT_MAX_NAME_BYTES,
    ) -> None:
        self._max_argument_bytes = max_argument_bytes
        self._max_call_id_bytes = max_call_id_bytes
        self._max_name_bytes = max_name_bytes
        self._partials: dict[str, _PartialFunctionCall] = {}
        self._completed: dict[str, FunctionCallRequest] = {}

    def consume(self, event: Mapping[str, object]) -> tuple[FunctionCallRequest, ...]:
        """Consume one event and return any completed function-call requests.

        Unknown or unrelated events return an empty tuple.
        """

        if not isinstance(event, Mapping):
            raise FunctionCallEventError("event must be an object")

        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise FunctionCallEventError("event must include a type")

        if event_type == "response.function_call_arguments.delta":
            return self._consume_delta(event)
        if event_type == "response.function_call_arguments.done":
            return self._consume_done(event)
        if event_type == "response.done":
            return self._consume_response_done(event)
        return ()

    def _consume_delta(self, event: Mapping[str, object]) -> tuple[FunctionCallRequest, ...]:
        call_id, name, delta = self._extract_delta_event_fields(event)
        current = self._partials.get(call_id)

        if call_id in self._completed:
            completed = self._completed[call_id]
            if completed.name != name:
                raise FunctionCallEventError("call_id replay payload conflicts with completed call")
            return ()

        if current is not None and current.name != name:
            raise FunctionCallEventError("call_id has conflicting function-call name")

        current_total = current.total_chars if current is not None else 0
        self._ensure_fragment_limit(current_total + len(delta))

        fragments = current.fragments if current is not None else ()
        updated = _PartialFunctionCall(
            name=name,
            fragments=(*fragments, delta),
            total_chars=current_total + len(delta),
        )
        self._partials[call_id] = updated
        return ()

    def _consume_done(self, event: Mapping[str, object]) -> tuple[FunctionCallRequest, ...]:
        call_id, name, arguments = self._extract_done_event_fields(event)
        return self._finalize(call_id=call_id, name=name, arguments=arguments)

    def _consume_response_done(
        self,
        event: Mapping[str, object],
    ) -> tuple[FunctionCallRequest, ...]:
        response = event.get("response")
        if not isinstance(response, Mapping):
            return ()

        output_items = response.get("output")
        if not isinstance(output_items, list):
            return ()

        results: list[FunctionCallRequest] = []
        for raw_item in output_items:
            if not isinstance(raw_item, Mapping):
                continue
            if raw_item.get("type") != "function_call":
                continue
            try:
                call_id, name, arguments = self._extract_call_fields_from_item(raw_item)
            except FunctionCallEventError:
                continue

            request = self._finalize_once(call_id=call_id, name=name, arguments=arguments)
            if request is not None:
                results.append(request)

        return tuple(results)

    def _finalize(
        self,
        *,
        call_id: str,
        name: str,
        arguments: MappingProxyType,
    ) -> tuple[FunctionCallRequest, ...]:
        request = self._build_request(call_id=call_id, name=name, arguments=arguments)
        if call_id in self._completed:
            previous = self._completed[call_id]
            if previous == request:
                return ()
            raise FunctionCallEventError("call_id already completed with a different request")

        self._partials.pop(call_id, None)
        self._completed[call_id] = request
        return (request,)

    def _finalize_once(
        self, *,
        call_id: str,
        name: str,
        arguments: MappingProxyType,
    ) -> FunctionCallRequest | None:
        request = self._build_request(call_id=call_id, name=name, arguments=arguments)
        if call_id in self._completed:
            previous = self._completed[call_id]
            if previous == request:
                return None
            raise FunctionCallEventError("call_id already completed with a different request")

        # Response-done output should override any in-progress fragments for the same call.
        partial = self._partials.get(call_id)
        if partial is not None and partial.name != name:
            raise FunctionCallEventError("call_id has conflicting function-call name")

        self._partials.pop(call_id, None)
        self._completed[call_id] = request
        return request

    def _build_request(
        self,
        *,
        call_id: str,
        name: str,
        arguments: MappingProxyType,
    ) -> FunctionCallRequest:
        return FunctionCallRequest(call_id=call_id, name=name, arguments=arguments)

    @staticmethod
    def _ensure_str_field(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value:
            raise FunctionCallEventError(f"{field_name} must be a non-empty string")
        return value

    def _validate_call_id(self, value: object) -> str:
        call_id = self._ensure_str_field(value, "call_id")
        if len(call_id) > self._max_call_id_bytes:
            raise FunctionCallEventError("call_id is too large")
        return call_id

    def _validate_name(self, value: object) -> str:
        name = self._ensure_str_field(value, "name")
        if len(name) > self._max_name_bytes:
            raise FunctionCallEventError("name is too large")
        return name

    def _extract_delta_event_fields(
        self,
        event: Mapping[str, object],
    ) -> tuple[str, str, str]:
        call_id = self._validate_call_id(event.get("call_id"))
        name = self._validate_name(event.get("name"))
        delta = event.get("delta")
        if not isinstance(delta, str):
            raise FunctionCallEventError("delta must be a string")
        return call_id, name, delta

    def _extract_done_event_fields(
        self,
        event: Mapping[str, object],
    ) -> tuple[str, str, MappingProxyType]:
        call_id = self._validate_call_id(event.get("call_id"))
        name = self._validate_name(event.get("name"))
        arguments = event.get("arguments")
        if isinstance(arguments, MappingProxyType):
            # Already parsed/canonicalized payload coming from internal callers.
            parsed = dict(arguments)
        else:
            parsed = self._parse_arguments(arguments)
        return call_id, name, MappingProxyType(parsed)

    def _extract_call_fields_from_item(
        self,
        item: Mapping[str, object],
    ) -> tuple[str, str, MappingProxyType]:
        call_id = self._validate_call_id(item.get("call_id"))
        name = self._validate_name(item.get("name"))
        arguments = item.get("arguments")
        if isinstance(arguments, MappingProxyType):
            parsed = dict(arguments)
        else:
            parsed = self._parse_arguments(arguments)
        return call_id, name, MappingProxyType(parsed)

    def _parse_arguments(self, value: object) -> dict[str, object]:
        if not isinstance(value, str):
            raise FunctionCallEventError("arguments must be a JSON string")
        self._ensure_fragment_limit(len(value))
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError as exc:
            raise FunctionCallEventError("arguments must be valid JSON") from exc

        if not isinstance(parsed, dict):
            raise FunctionCallEventError("arguments must be a JSON object")
        return parsed

    def _ensure_fragment_limit(self, total_chars: int) -> None:
        if total_chars > self._max_argument_bytes:
            raise FunctionCallEventError("function-call arguments payload is too large")
