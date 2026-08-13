---
title: Crunchyroll Device-Authorized Provider and Brave Widevine Playback
subtitle: Replacement design for native catalog resolution, passwordless account authorization, and official DRM playback
source_scope: Existing media confirmation contracts, current Crunchyroll pairing prototype, official Crunchyroll activation guidance, and smirgol/plugin.video.crunchyroll behavior reviewed at commit 147c7078be12e4acb6c7be51d2200c0eeef219af
status: approved architecture; implementation gated by feasibility tests
---

# Decisions at a glance

| Area | Decision |
|---|---|
| Authentication | Crunchyroll device authorization; this application never receives a username or password |
| Phone interaction | QR/code opens `https://www.crunchyroll.com/activate` directly |
| Credential persistence | Refresh token in the desktop Secret Service/KDE Wallet; access tokens remain memory-only |
| Resolution | A dedicated provider queries Crunchyroll catalog endpoints and returns typed candidates |
| Confirmation | Reuse the existing controller-private `PendingMediaCatalog` and strict `accept | reject | cancel | revise` semantics |
| Playback | Dedicated Brave app shell using Chromium EME/Widevine and an official Crunchyroll playback session |
| DRM boundary | Never extract keys, decrypt media outside Widevine, or bridge protected playback into `mpv` |
| Provider policy | An explicit Crunchyroll request never silently falls back to an unofficial anime source |
| Migration | Retire the LAN pairing server only after device auth, playback, and cleanup pass a real end-to-end gate |
| Deferred features | Progress sync, watchlist/history, profiles, Crunchylists, and intro/outro skipping |

Core invariant: the model chooses a bounded provider operation; the controller owns exact media identity, confirmation, credentials, playback sessions, and teardown.

# Why this replaces the current design

The current prototype makes the phone a remote keyboard for a Crunchyroll login page. It requires a LAN HTTP server, firewall policy, CDP input injection, a dedicated browser profile, and repeated user interaction while still delivering only authentication—not catalog resolution or playback.

The replacement moves complexity into one provider adapter. First authorization happens on Crunchyroll's HTTPS activation page. Subsequent requests resolve and play without another phone step until authorization is revoked. No application-owned credential form or custom credential encryption is required.

The earlier `2026-08-12-crunchyroll-brave-player-shell-design.md` remains historical context but is superseded wherever it specifies browser-cookie authentication, LAN pairing, remote keyboard input, or rendered-site scraping.

# Scope

## First slice

- device-code request, display, polling, cancellation, and expiry;
- secure refresh-token persistence and explicit disconnect;
- access-token refresh and revoked-token recovery;
- exact title search and bounded ambiguity handling;
- series, season, episode, audio, and subtitle metadata resolution;
- official artwork and display-safe confirmation projection;
- an opaque controller-private Crunchyroll playback locator;
- official DASH/Widevine playback in a controller-owned Brave shell;
- active stream release on stop, replacement, browser exit, failure, controller shutdown, and crash recovery;
- removal of the LAN pairing runtime and its UFW rule after migration acceptance.

## Deferred

- playback-progress synchronization;
- browsing or editing watchlist/history;
- profile selection and profile switching;
- Crunchylists;
- intro/outro skipping;
- a broad preferences UI for dub/subtitle locales;
- automatic next-episode playback;
- providers other than Crunchyroll and existing media adapters.

# User experience

## First authorization

1. An explicit Crunchyroll request finds no refresh credential.
2. The controller requests a short-lived device code from Crunchyroll.
3. The existing UI shows the activation URL, code, expiry countdown, and QR.
4. The QR targets Crunchyroll HTTPS directly. It does not target this workstation.
5. The user signs in and approves on Crunchyroll's page.
6. The controller polls at the server-provided interval, respecting slowdown and expiry responses.
7. On success, the refresh token is stored in Secret Service/KDE Wallet and the access token remains in memory.
8. The original preparation request resumes from controller-owned structured arguments.

The UI must say “Authorize Crunchyroll” rather than implying that a title is resolved or playing. Cancellation clears the device flow and the pending continuation.

## Normal request

1. The user says, for example, “Play season 2 episode 4 of Vinland Saga on Crunchyroll.”
2. `media_prepare_crunchyroll` invokes the provider with the explicit title and episode/audio constraints.
3. The provider returns one exact episode, bounded candidates, unavailable, authentication required, or provider failure.
4. An exact result creates `PendingMedia` with official title, episode metadata, artwork, and language context.
5. The UI renders the same confirmation surface as other media.
6. Only `media_decide(accept)` for the exact pending generation can reserve a playback session and launch Brave.
7. Playback starts immediately, not paused on a first frame.
8. Stop, close, completion, or failure restores voice continuity and releases the upstream stream slot.

## Unavailable or ambiguous

Ambiguity returns at most five display-safe candidates. The model asks a bounded clarification and never guesses between materially different series, seasons, or language variants.

An unavailable explicit request does not invoke the unofficial anime provider. The assistant may ask whether the user wants to try another source; only a later affirmative request permits that provider.

# Architecture

```text
Realtime voice/UI
  ├── activation card
  └── existing media confirmation card
             │
             ▼
VoiceSessionController
  ├── PendingMediaCatalog
  └── CrunchyrollProvider
        ├── DeviceAuthClient
        ├── CredentialStore (Secret Service/KDE Wallet)
        ├── CatalogClient
        ├── Resolver
        └── BraveWidevinePlaybackManager
              ├── loopback-only player server
              ├── Shaka/EME player page
              ├── manifest/license mediation
              └── active-stream lease + process ownership
```

## Component boundaries

### `DeviceAuthClient`

- Requests device codes and validates response shape.
- Polls according to server-provided interval and bounded backoff.
- Distinguishes pending, slowdown, denied, expired, revoked, network failure, and success.
- Refreshes access tokens with a 60-second expiry margin.
- Never logs authorization headers, token responses, device codes, or response bodies from auth endpoints.

### `CredentialStore`

- Stores only refresh token and stable random device identifier.
- Uses `org.freedesktop.secrets`, available through KDE's running Secret Service implementation on this host.
- Uses a fixed service name and account key; no token appears in argv or environment.
- Fails closed if the secret service is unavailable. There is no plaintext or homemade-encrypted-file fallback.
- Supports an explicit disconnect operation that deletes the secret and clears in-memory tokens.

### `CatalogClient`

- Contains endpoint definitions, client identity, headers, timeouts, and strict response models in one module.
- Exposes typed search, seasons, episodes, and playback preparation operations.
- Allows only exact HTTPS Crunchyroll origins.
- Applies bounded pagination and result counts.
- Never exposes raw upstream JSON to the model or UI.

These are private application APIs, not a supported public developer platform. Endpoint/client changes must produce a typed provider failure, never a silent fallback or fabricated result.

### `Resolver`

- Normalizes title, season, episode, dub/subtitle, and “latest” constraints.
- Scores exact title identity separately from episode identity.
- Returns ambiguity when confidence or numbering is unclear.
- Keeps canonical series/season/episode IDs controller-private.
- Produces official thumbnail and display metadata only after URL-host and schema validation.

### `BraveWidevinePlaybackManager`

- Is a separate playback backend; it does not add DRM logic to `MediaPlaybackManager`'s `mpv` path.
- Owns exactly one Crunchyroll playback generation and process group.
- Reserves an upstream playback session only after confirmation.
- Launches the configured Brave binary in app mode against a random loopback player URL.
- Uses Chromium EME with `com.widevine.alpha`; it never handles raw content keys.
- Reports ready, playing, completed, stopped, and failed lifecycle events to the controller.
- Releases the upstream stream token on every terminal path.

# Controller and media contracts

Extend `SourceKind` with `crunchyroll` and add a controller-private locator:

```text
CrunchyrollPlaybackLocator
  series_id
  season_id
  episode_id
  content_id
  audio_locale
  subtitle_locale
```

The locator contains stable content identity only. It must not contain an access token, refresh token, stream token, manifest URL, license URL, or authorization header. Playback credentials and URLs are requested just-in-time after exact acceptance.

`PendingMedia.display_projection()` may expose source, title, episode metadata, thumbnail, and selected language labels. It must never serialize controller-private identifiers or authentication/playback data.

Provider outcomes are:

- `prepared`
- `authentication_required`
- `ambiguous`
- `unavailable`
- `provider_failure`

The device authorization flow is a controller continuation. The original validated preparation arguments remain controller-owned and resume after authorization; the model is not asked to reconstruct them.

# Player and DRM boundary

The loopback player binds only `127.0.0.1` on a random port and uses a high-entropy playback path. It serves a locally vendored, version-pinned EME player; no CDN script is loaded.

Browser JavaScript receives only an opaque playback-session handle and display metadata. The local backend mediates:

- manifest retrieval when bearer headers are required;
- Widevine license challenge forwarding with the required Crunchyroll session headers;
- playback-session release;
- bounded lifecycle events.

Access tokens, stream tokens, authorization headers, and license responses remain backend-only. Media segments should be fetched directly from Crunchyroll/CDN origins where the manifest permits it; the backend must not become a general-purpose proxy.

Origin and request policy:

- exact allowlists for Crunchyroll API, playback, license, static, and returned CDN origins;
- no caller-supplied upstream URL;
- no arbitrary proxy path;
- no credentials in query strings;
- `Cache-Control: no-store` for local session responses;
- restrictive CSP and no external scripts, analytics, fonts, or extensions;
- no remote debugging port in the final player unless a bounded development-only smoke requires it.

This design uses Crunchyroll's encrypted stream and license service through Chromium's Widevine CDM. It does not extract keys, decrypt content outside the CDM, save protected media, or route protected playback through `mpv`.

# Lifecycle and crash recovery

The controller owns one active Crunchyroll object containing session ID, playback ID, episode/content IDs, opaque upstream stream token, local server task, Brave process group, and watcher task.

Startup is transactional:

1. validate exact accepted pending generation;
2. obtain a valid access token;
3. request playback data;
4. create the loopback player session;
5. launch Brave;
6. wait for a bounded ready/playing handshake;
7. publish `media_ready`.

Any failure unwinds in reverse order. If an upstream stream token was issued, release is attempted even when Brave never becomes ready.

On normal stop, replacement, browser close, controller session teardown, SIGTERM, or service shutdown: stop the local player, terminate/reap the exact process group, release the stream token, clear memory, then restore voice.

Because a process can be killed before cleanup, provider startup queries active playback sessions and releases only stale sessions whose device ID exactly matches this installation's stable device identifier. This recovery is bounded and idempotent. No stream token or playback URL is written to disk for crash recovery.

# Security and privacy

Persisted:

- refresh token in Secret Service/KDE Wallet;
- stable random device identifier;
- non-sensitive locale preferences when later added.

Memory-only:

- access token;
- device code and polling handle;
- manifest URL and headers;
- stream token;
- license challenge/response;
- provider response bodies.

Never handled:

- Crunchyroll username;
- Crunchyroll password;
- MFA secret;
- payment information.

Redaction tests must cover structured logs, exceptions, HTTP client errors, traces, subprocess argv/environment, and health/status projections.

# Migration

The existing `crunchyroll_remote` pairing implementation may remain as the currently deployed behavior while the replacement is developed and tested in isolation. The new provider path must never invoke it as a fallback. Production switches atomically only after the acceptance gate passes; that deployment removes the old path rather than keeping two authentication systems.

After the acceptance gate passes:

- remove the CLI entry point and controller login-manager ownership;
- delete phone/host assets and encrypted keyboard protocol;
- remove PyNaCl and QR dependencies if no other code uses them;
- remove the exact UFW rule for `10.0.0.151 -> 10.0.0.152:42877/tcp` on `wlan0`;
- remove the dedicated pairing browser profile if it contains no required migration state;
- retain history through git rather than dead runtime code.

If the new provider fails its feasibility gate, the deployed runtime remains honest: explicit Crunchyroll requests return unavailable/provider failure. It does not silently reactivate the pairing prototype or use an unofficial source.

# Implementation and verification gates

## Gate 1: device authorization feasibility

Against the real service, without credentials in the test harness:

- receive a valid user code, verification URL, expiry, and polling interval;
- complete authorization on Crunchyroll HTTPS;
- receive and store a refresh token through Secret Service;
- restart the provider and refresh successfully without another login;
- disconnect and prove the secret was deleted.

## Gate 2: catalog contract

- exact search resolves a known title;
- ambiguous title returns bounded candidates;
- season/episode identity is stable and validated;
- unavailable title and locale are typed failures;
- fixtures contain no real tokens or account identifiers.

## Gate 3: playback feasibility

With an authorized premium account and one chosen episode:

- obtain official playback metadata after confirmation only;
- play encrypted video and audio in Brave through Widevine;
- verify immediate playback, seek, pause, volume, fullscreen, subtitle selection, and clean browser close;
- prove access/stream/license secrets are absent from browser-visible state, logs, argv, environment, and persisted files;
- release the upstream stream session and prove no Brave, server, or watcher process remains.

## Gate 4: main-flow integration

A real spoken explicit request must exercise:

`ask -> resolve -> preview -> accept -> play -> close -> voice restored`

Also verify reject, cancel, revise, auth expiry, token revocation, provider outage, Brave startup failure, Widevine failure, controller restart, and service shutdown.

The old pairing runtime and firewall rule are removed only after all four gates pass.

# Testing strategy

- Unit tests for auth state transitions, polling cadence, token expiry, response validation, URL allowlists, resolver ambiguity, locators, redaction, and cleanup idempotency.
- Contract fixtures synthesized from documented shapes or sanitized captures; never commit real credentials, account IDs, stream tokens, or license payloads.
- Integration tests use fake HTTP transports and fake Brave processes by default.
- Live auth/catalog/playback tests are opt-in, serialized, and always release stream sessions in fixture teardown.
- Main-flow tests preserve exact-session and exact-pending-generation semantics already used by media confirmation.
- A final live deployment test installs the built wheel, restarts the controller, executes the spoken path, and checks process/listener cleanup.

# Legal and maintenance boundary

`smirgol/plugin.video.crunchyroll` is AGPL-3.0. This project may use it as behavioral research, but implementation must be original unless the project deliberately adopts compatible licensing and attribution. No source is copied from the plugin in this design.

The provider uses private Crunchyroll application interfaces and can break when Crunchyroll changes client identity, endpoints, headers, schemas, or playback policy. Endpoint definitions, transport, response models, and fixtures must remain isolated so failures are visible and repairable.
