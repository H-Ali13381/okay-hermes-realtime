from __future__ import annotations

from dataclasses import dataclass


class RemotePrototypeError(RuntimeError):
    """A bounded failure in the experimental Crunchyroll remote."""


class CommandError(RemotePrototypeError):
    """An invalid, unauthenticated, or replayed encrypted command."""


@dataclass(frozen=True, slots=True)
class RemoteCommand:
    version: int
    sequence: int
    kind: str
    value: str | None
