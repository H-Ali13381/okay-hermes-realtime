from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from .contracts import CommandError, RemoteCommand

_ALLOWED_KEYS = frozenset({"Tab", "Enter", "Backspace"})
_ALLOWED_KINDS = frozenset({"pair", "text", "key", "cancel"})
_MAX_TEXT_CHARACTERS = 2_048
_MAX_FRAME_CHARACTERS = 8_192


def encode_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: object, *, expected_bytes: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or len(value) > _MAX_FRAME_CHARACTERS:
        raise CommandError("invalid encrypted command")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise CommandError("invalid encrypted command") from exc
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise CommandError("invalid encrypted command")
    return decoded


class EncryptedCommandSession:
    """Decrypt one ordered, single-client command stream without logging payloads."""

    def __init__(
        self,
        *,
        key: bytes,
        session_id: str,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(key) != SecretBox.KEY_SIZE:
            raise ValueError("pairing key must be 256 bits")
        if not session_id:
            raise ValueError("session id is required")
        if ttl_seconds <= 0:
            raise ValueError("session lifetime must be positive")
        self._box = SecretBox(key)
        self.session_id = session_id
        self._clock = clock
        self._expires_at = clock() + ttl_seconds
        self._last_sequence = 0
        self._paired = False

    @property
    def paired(self) -> bool:
        return self._paired

    def decrypt(self, frame: Mapping[str, Any]) -> RemoteCommand:
        if self._clock() >= self._expires_at:
            raise CommandError("pairing session expired")
        nonce = _decode(frame.get("nonce"), expected_bytes=SecretBox.NONCE_SIZE)
        ciphertext = _decode(frame.get("ciphertext"))
        try:
            plaintext = self._box.decrypt(ciphertext, nonce)
            payload = json.loads(plaintext)
        except (CryptoError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise CommandError("invalid encrypted command") from exc
        if not isinstance(payload, dict):
            raise CommandError("invalid encrypted command")
        command = self._validate(payload)
        self._last_sequence = command.sequence
        if command.kind == "pair":
            self._paired = True
        return command

    def _validate(self, payload: dict[str, Any]) -> RemoteCommand:
        if set(payload) - {"version", "sequence", "kind", "value"}:
            raise CommandError("invalid encrypted command")
        version = payload.get("version")
        sequence = payload.get("sequence")
        kind = payload.get("kind")
        value = payload.get("value")
        if version != 1:
            raise CommandError("unsupported protocol version")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise CommandError("invalid command sequence")
        if sequence != self._last_sequence + 1:
            raise CommandError("invalid command sequence")
        if not isinstance(kind, str) or kind not in _ALLOWED_KINDS:
            raise CommandError("invalid command kind")
        if not self._paired and kind != "pair":
            raise CommandError("pair command required")
        if self._paired and kind == "pair":
            raise CommandError("pair command already accepted")
        if kind in {"pair", "cancel"}:
            if value is not None:
                raise CommandError("command value is not allowed")
            value = None
        elif kind == "key":
            if not isinstance(value, str) or value not in _ALLOWED_KEYS:
                raise CommandError("invalid key command")
        elif not isinstance(value, str) or not value or len(value) > _MAX_TEXT_CHARACTERS:
            raise CommandError("invalid text command")
        return RemoteCommand(version=version, sequence=sequence, kind=kind, value=value)
