# Crunchyroll phone-login prototype

Status: integrated into the main realtime voice controller on 2026-08-13, with the standalone command retained for direct testing.

Run from the repository root:

```bash
uv run okay-hermes-crunchyroll-login
```

The command opens a dedicated Brave app window with a QR code. Scan it from a phone on the same private LAN. Pairing replaces the QR page with Crunchyroll's genuine SSO page. The phone can send encrypted text plus Tab, Enter, Backspace, and Cancel commands.

This slice deliberately does not provide screen mirroring, pointer input, title resolution, media confirmation, or automatic playback. In the main voice flow, an explicit Crunchyroll request opens this official login setup and returns `authentication_required`; it does not silently use the unofficial anime provider or claim that playback started. The standalone command remains useful for testing the login experience directly.

## Security boundary

- Brave uses `~/.local/share/okay-hermes-realtime/crunchyroll-brave-profile` by default.
- The pairing server binds to the selected private LAN IPv4 address and loopback, never all interfaces.
- CDP binds to loopback only.
- The 256-bit pairing key is in the QR URL fragment and therefore is not sent in HTTP requests; the QR SVG that embeds it is served only to loopback clients.
- Commands use TweetNaCl `secretbox`; the host rejects tampering and replays.
- The broker accepts one ordered stream for five minutes.
- The input field is cleared after each send; command plaintext is not logged or echoed.
- Stopping the command reaps its browser process group and listeners.

Prototype limitation: the phone page is delivered over trusted-LAN HTTP. Encrypted commands protect against passive observers, but an active LAN attacker could replace the page before its script runs. Do not expose this command beyond a trusted private LAN.

## Validation

Automated coverage includes encrypted-frame validation and replay rejection, route secrecy and headers, fixed navigation, CDP input allowlisting, dedicated Brave flags, CLI address validation, packaged assets, and TweetNaCl/PyNaCl interoperability.

The local no-credential acceptance harness launches the installed Brave build, loads the QR host page, submits an authenticated encrypted pair command, verifies navigation to `https://sso.crunchyroll.com`, and then verifies teardown. It never enters account credentials.
