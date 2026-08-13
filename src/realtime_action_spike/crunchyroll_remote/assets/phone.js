(() => {
  'use strict';

  const sessionId = document.querySelector('main').dataset.session;
  const status = document.querySelector('#status');
  const input = document.querySelector('#input');
  let sequence = 0;
  let queue = Promise.resolve();
  document.querySelectorAll('button').forEach(button => { button.disabled = true; });

  function decodeFragment() {
    const raw = location.hash.slice(1);
    history.replaceState(null, '', location.pathname);
    if (!raw) throw new Error('Pairing key missing');
    const padded = raw.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - raw.length % 4) % 4);
    const bytes = Uint8Array.from(atob(padded), character => character.charCodeAt(0));
    if (bytes.length !== nacl.secretbox.keyLength) throw new Error('Invalid pairing key');
    return bytes;
  }

  function encode(bytes) {
    let binary = '';
    bytes.forEach(value => { binary += String.fromCharCode(value); });
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  const key = decodeFragment();

  async function send(kind, value = null) {
    const message = { version: 1, sequence: ++sequence, kind, value };
    const payload = new TextEncoder().encode(JSON.stringify(message));
    const nonce = nacl.randomBytes(nacl.secretbox.nonceLength);
    const ciphertext = nacl.secretbox(payload, nonce, key);
    const response = await fetch(`/api/sessions/${sessionId}/commands`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nonce: encode(nonce), ciphertext: encode(ciphertext) }),
      cache: 'no-store',
      credentials: 'omit',
      referrerPolicy: 'no-referrer',
    });
    if (!response.ok) throw new Error('Command rejected');
  }

  async function act(kind, value = null) {
    queue = queue.then(async () => {
      try {
        await send(kind, value);
        status.textContent = 'Connected';
        document.querySelectorAll('button').forEach(button => { button.disabled = false; });
      } catch (_) {
        status.textContent = 'Connection failed — rescan the QR code';
        document.querySelectorAll('button').forEach(button => { button.disabled = true; });
      }
    });
    return queue;
  }

  document.querySelector('#send').addEventListener('click', () => {
    const value = input.value;
    input.value = '';
    if (value) act('text', value);
  });
  document.querySelectorAll('[data-key]').forEach(button => {
    button.addEventListener('click', () => act('key', button.dataset.key));
  });
  document.querySelector('#cancel').addEventListener('click', () => act('cancel'));

  act('pair');
})();
