from __future__ import annotations

import json
import subprocess
from pathlib import Path

from nacl.secret import SecretBox

from realtime_action_spike.crunchyroll_remote import EncryptedCommandSession, RemoteCommand

ASSET = (
    Path(__file__).parents[1]
    / "src"
    / "realtime_action_spike"
    / "crunchyroll_remote"
    / "assets"
    / "tweetnacl.min.js"
)


def test_vendored_phone_cipher_interoperates_with_host_pynacl() -> None:
    key = bytes(range(SecretBox.KEY_SIZE))
    nonce = bytes(range(SecretBox.NONCE_SIZE))
    script = f"""
const fs = require('fs');
const vm = require('vm');
const context = {{
  self: {{}},
  crypto: require('crypto').webcrypto,
  TextEncoder,
  Uint8Array,
}};
vm.createContext(context);
vm.runInContext(fs.readFileSync({str(ASSET)!r}, 'utf8'), context);
const nacl = context.self.nacl;
const key = Uint8Array.from({list(key)!r});
const nonce = Uint8Array.from({list(nonce)!r});
const message = {{version:1,sequence:1,kind:'pair',value:null}};
const payload = new TextEncoder().encode(JSON.stringify(message));
const ciphertext = nacl.secretbox(payload, nonce, key);
function b64url(value) {{ return Buffer.from(value).toString('base64url'); }}
process.stdout.write(JSON.stringify({{nonce:b64url(nonce),ciphertext:b64url(ciphertext)}}));
"""

    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    session = EncryptedCommandSession(key=key, session_id="interop")

    assert session.decrypt(json.loads(result.stdout)) == RemoteCommand(
        version=1,
        sequence=1,
        kind="pair",
        value=None,
    )
