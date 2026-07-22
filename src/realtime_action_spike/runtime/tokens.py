from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

MonotonicClock = Callable[[], float]
TokenFactory = Callable[[], str]


@dataclass
class _TokenRecord:
    local_session_id: str
    expires_at: float
    generation: int
    consumed: bool = False


class LaunchTokenStore:
    """Store for single-use launch tokens with configurable TTL."""

    def __init__(
        self,
        *,
        token_ttl_seconds: float = 15.0,
        monotonic_clock: MonotonicClock | None = None,
        token_factory: TokenFactory | None = None,
    ) -> None:
        self._clock: Final[MonotonicClock] = monotonic_clock or time.monotonic
        self._token_factory: Final[TokenFactory] = token_factory or (
            lambda: secrets.token_urlsafe(16)
        )
        self._token_ttl_seconds = token_ttl_seconds

        self._records: dict[str, _TokenRecord] = {}
        self._generation: int = 0

    @property
    def token_ttl_seconds(self) -> float:
        return self._token_ttl_seconds

    def issue(self, local_session_id: str) -> str:
        """Issue a new one-time token for a local session."""

        self._generation += 1
        token = self._token_factory()
        record = _TokenRecord(
            local_session_id=local_session_id,
            expires_at=self._clock() + self._token_ttl_seconds,
            generation=self._generation,
        )
        self._records[token] = record
        return token

    def validate(self, token: str) -> str | None:
        """Validate without consuming. Returns session id when valid."""

        now = self._clock()
        record = self._records.get(token)
        if record is None:
            return None
        if record.generation != self._generation:
            return None
        if record.consumed:
            return None
        if now > record.expires_at:
            return None
        return record.local_session_id

    def consume(self, token: str) -> str | None:
        """Consume exactly once when valid."""

        local_session_id = self.validate(token)
        if local_session_id is None:
            return None

        record = self._records[token]
        record.consumed = True
        return local_session_id

    def invalidate(self, token: str) -> None:
        """Invalidate a token without reporting a session id."""

        record = self._records.get(token)
        if record is None:
            return
        record.consumed = True
