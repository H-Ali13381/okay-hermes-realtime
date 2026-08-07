"""Shared detection of transport/routing wrappers around direct work objectives."""

from __future__ import annotations

import re

_POLITE_PREFIX = r"(?:(?:please|could\s+you|can\s+you|would\s+you)\s+)?"
_ROUTING_DIRECTIVE = r"""(?:
    (?:have|ask|tell|let)\s+hermes(?:\s+agent)?\b
    |(?:send|hand|give)\s+(?:this|that|it|the\s+(?:request|task|job))
        \s+to\s+hermes(?:\s+agent)?\b
    |(?:add|create)\s+(?:a\s+)?(?:new\s+)?kanban\s+task\b
    |(?:add|create)\s+(?:a\s+)?(?:new\s+)?task\s+(?:to|on|in)
        \s+(?:the\s+)?kanban\b
    |put\s+(?:(?:this|that|the)\s+)?(?:request|task|job)?\s*
        (?:on|onto|in|into)\s+(?:the\s+)?kanban\b
)"""
_ROUTING_WRAPPER_PATTERNS = (
    re.compile(rf"(?ix)^\s*{_POLITE_PREFIX}{_ROUTING_DIRECTIVE}"),
    re.compile(rf"(?ix)\b(?:and|then)\s+{_POLITE_PREFIX}{_ROUTING_DIRECTIVE}"),
)


def find_routing_wrappers(text: str) -> list[str]:
    """Return obvious imperative routing wrappers without matching literal subject mentions."""

    hits: list[str] = []
    for pattern in _ROUTING_WRAPPER_PATTERNS:
        hits.extend(match.group(0).strip() for match in pattern.finditer(text))
    return hits
