from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from nacl.secret import SecretBox

from realtime_action_spike.crunchyroll_remote import (
    CommandError,
    EncryptedCommandSession,
    RemoteCommand,
    create_remote_app,
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _encrypted_frame(
    key: bytes,
    payload: dict[str, object],
    *,
    nonce_byte: int = 1,
) -> dict[str, str]:
    nonce = bytes([nonce_byte]) * SecretBox.NONCE_SIZE
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ciphertext = SecretBox(key).encrypt(plaintext, nonce).ciphertext
    return {"nonce": _b64url(nonce), "ciphertext": _b64url(ciphertext)}


def test_encrypted_session_accepts_ordered_allowlisted_commands_and_rejects_replay() -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    session = EncryptedCommandSession(key=key, session_id="public-session")

    pair = session.decrypt(_encrypted_frame(key, {"version": 1, "sequence": 1, "kind": "pair"}))
    text = session.decrypt(
        _encrypted_frame(
            key,
            {"version": 1, "sequence": 2, "kind": "text", "value": "not logged"},
            nonce_byte=2,
        )
    )

    assert pair == RemoteCommand(version=1, sequence=1, kind="pair", value=None)
    assert text == RemoteCommand(version=1, sequence=2, kind="text", value="not logged")
    with pytest.raises(CommandError, match="sequence"):
        session.decrypt(
            _encrypted_frame(
                key,
                {"version": 1, "sequence": 2, "kind": "text", "value": "replayed"},
                nonce_byte=3,
            )
        )


@pytest.mark.parametrize(
    ("payload", "message", "pair_first"),
    [
        ({"version": 1, "sequence": 1, "kind": "text", "value": "before pair"}, "pair", False),
        ({"version": 1, "sequence": 2, "kind": "key", "value": "F12"}, "key", True),
        (
            {"version": 1, "sequence": 2, "kind": "navigate", "value": "https://evil.test"},
            "kind",
            True,
        ),
        ({"version": 1, "sequence": 2, "kind": "text", "value": "x" * 2049}, "text", True),
    ],
)
def test_encrypted_session_rejects_unsafe_commands(
    payload: dict[str, object], message: str, pair_first: bool
) -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    session = EncryptedCommandSession(key=key, session_id="public-session")
    if pair_first:
        session.decrypt(_encrypted_frame(key, {"version": 1, "sequence": 1, "kind": "pair"}))

    with pytest.raises(CommandError, match=message):
        session.decrypt(_encrypted_frame(key, payload, nonce_byte=2 if pair_first else 1))


def test_encrypted_session_rejects_wrong_key_without_leaking_ciphertext() -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    wrong_key = bytes(reversed(range(SecretBox.KEY_SIZE)))
    frame = _encrypted_frame(wrong_key, {"version": 1, "sequence": 1, "kind": "pair"})
    session = EncryptedCommandSession(key=key, session_id="public-session")

    with pytest.raises(CommandError) as exc_info:
        session.decrypt(frame)

    assert frame["ciphertext"] not in str(exc_info.value)


@dataclass
class RecordingController:
    commands: list[RemoteCommand] = field(default_factory=list)

    async def apply(self, command: RemoteCommand) -> None:
        self.commands.append(command)


def test_remote_app_keeps_pairing_key_in_fragment_and_serves_no_external_assets() -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    controller = RecordingController()
    app = create_remote_app(
        session_id="public-session",
        pairing_key=key,
        public_base_url="http://192.168.50.9:49152",
        controller=controller,
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    host = client.get("/host/public-session")
    phone = client.get("/pair/public-session")
    qr = client.get("/host/public-session/qr.svg")

    encoded_key = _b64url(key)
    assert host.status_code == phone.status_code == qr.status_code == 200
    assert encoded_key not in host.text
    assert encoded_key not in phone.text
    assert encoded_key not in qr.text
    assert "data-pairing-url" not in host.text
    assert "tweetnacl.min.js" in phone.text
    assert "http://" not in phone.text and "https://" not in phone.text
    assert qr.headers["content-type"].startswith("image/svg+xml")
    assert host.headers["content-security-policy"].startswith("default-src 'self'")
    assert phone.headers["cache-control"] == "no-store"


def test_qr_asset_is_available_only_to_loopback_host_browser() -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    app = create_remote_app(
        session_id="public-session",
        pairing_key=key,
        public_base_url="http://192.168.50.9:49152",
        controller=RecordingController(),
    )

    host_client = TestClient(app, client=("127.0.0.1", 50000))
    lan_client = TestClient(app, client=("192.168.50.20", 50000))

    assert host_client.get("/host/public-session/qr.svg").status_code == 200
    assert lan_client.get("/host/public-session/qr.svg").status_code == 404


def test_remote_app_decrypts_commands_and_never_echoes_sensitive_text() -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    controller = RecordingController()
    session = EncryptedCommandSession(key=key, session_id="public-session")
    app = create_remote_app(
        session_id="public-session",
        pairing_key=key,
        public_base_url="http://192.168.50.9:49152",
        controller=controller,
        command_session=session,
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    pair = client.post(
        "/api/sessions/public-session/commands",
        json=_encrypted_frame(key, {"version": 1, "sequence": 1, "kind": "pair"}),
    )
    secret_text = "credential-shaped-secret"
    text = client.post(
        "/api/sessions/public-session/commands",
        json=_encrypted_frame(
            key,
            {"version": 1, "sequence": 2, "kind": "text", "value": secret_text},
            nonce_byte=2,
        ),
    )

    assert pair.json() == {"ok": True, "status": "paired"}
    assert text.json() == {"ok": True, "status": "paired"}
    assert secret_text not in pair.text + text.text
    assert [command.kind for command in controller.commands] == ["pair", "text"]


def test_remote_app_rejects_unknown_session_and_invalid_ciphertext_generically() -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    app = create_remote_app(
        session_id="public-session",
        pairing_key=key,
        public_base_url="http://192.168.50.9:49152",
        controller=RecordingController(),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    unknown = client.post(
        "/api/sessions/other/commands",
        json=_encrypted_frame(key, {"version": 1, "sequence": 1, "kind": "pair"}),
    )
    invalid = client.post(
        "/api/sessions/public-session/commands",
        json={"nonce": _b64url(bytes(SecretBox.NONCE_SIZE)), "ciphertext": "invalid"},
    )

    assert unknown.status_code == 404
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid encrypted command"}
