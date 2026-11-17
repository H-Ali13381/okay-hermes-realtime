---
title: Crunchyroll Brave Player Shell and Phone Remote Authentication
subtitle: Approved design for explicit Crunchyroll requests, official Widevine playback, keyboard-free login, and consented unofficial fallback
source_scope: Current design session, deployed media-confirmation architecture, Brave process-ownership contracts, Crunchyroll official authentication and DRM documentation, and open-source client research
---

# Decisions at a glance

| Area | Locked decision |
|---|---|
| Playback engine | Official Crunchyroll web player in a dedicated Brave app-mode shell |
| DRM | Keep Crunchyroll's Chromium/Widevine pipeline intact; never extract or decrypt streams for `mpv` |
| Provider selection | Crunchyroll is used only when explicitly requested until general provider preferences are implemented |
| Unavailable explicit request | Ask before trying an unofficial anime source; never silently disregard “on Crunchyroll” |
| Browser profile | Dedicated owner-only Crunchyroll profile, separate from the Realtime voice profile and the user's normal Brave profile |
| Initial and expired login | Display a short-lived QR code; the phone remotely controls the genuine Crunchyroll SSO page |
| Credentials | Never store the account password; persistent Crunchyroll cookies remain in Brave's profile |
| Pairing transport | Trusted-LAN-only, single-use broker with application-layer encrypted frames and input events |
| Login automation | The phone sends constrained pointer, text, navigation-key, and scroll events to the real Brave login page; there is no application-owned credential form |
| Player presentation | Brave app mode plus a narrowly scoped extension that focuses the official player and reports lifecycle events |
| Completion | Restore voice and offer the next episode; do not autoplay |

> Core invariant: the controller may select and launch an exact official episode, but Crunchyroll owns authentication, DRM, playback, subtitles, language controls, progress, and skip controls.

# Scope

This specification is the first bounded official-provider slice. It adds explicit Crunchyroll requests without implementing the general provider-preference onboarding tracked in `docs/backlog/media-provider-onboarding.md`.

In scope:

- `media_prepare_crunchyroll` for explicit Crunchyroll requests;
- authenticated official-site search and exact episode resolution;
- phone-controlled genuine Crunchyroll login;
- persistent session cookies in a dedicated Brave profile;
- existing inline preview and natural-language confirmation;
- player-focused Brave app-mode playback;
- official Widevine, subtitles, dubs, progress, and skip controls;
- controller-owned process lifecycle and voice restoration;
- explicit consent before unofficial fallback;
- session-expiry and reauthentication recovery.

Out of scope:

- general first-run provider selection or priority ordering;
- providers other than Crunchyroll and the existing unofficial anime adapter;
- password storage, password-manager implementation, or cookie export;
- DRM key, manifest, license, or decrypted-stream extraction;
- `mpv` playback of Crunchyroll media;
- account creation, subscription purchase, payment entry, or parental-control setup;
- remote access outside the trusted LAN;
- automatic next-episode playback.

# User experience

## Explicit request with an active session

1. The user asks for an anime “on Crunchyroll.”
2. The controller resolves the exact official series and episode in the dedicated authenticated profile.
3. The existing inline confirmation card shows Crunchyroll, title, episode, language context, artwork, and provider-managed resume behavior.
4. A natural reply such as “Yep, play it” reduces to strict `media_decide(accept)` for the exact pending generation.
5. The Realtime page enters existing media continuity, mutes capture only after the accepted handoff, and sends `media_ready`.
6. The controller launches the exact Crunchyroll watch URL in the player shell.
7. The official player handles playback and account progress.
8. On episode end, player close, or bounded playback failure, the controller restores voice continuity.
9. Natural episode end produces a new next-episode confirmation rather than autoplay.

## Explicit request without an active session

1. Resolution detects that the dedicated profile is not authenticated or has been redirected to SSO.
2. The controller starts the isolated login broker and shows a QR card in the existing Realtime page.
3. The phone opens the temporary local remote-control page.
4. The TV shows the genuine Crunchyroll SSO page in Brave.
5. The phone displays encrypted low-rate views of that page and relays constrained touch, text, key, and scroll input.
6. The user completes login, CAPTCHA, MFA, consent, or updated terms on Crunchyroll's real page.
7. The broker verifies an authenticated Crunchyroll account page without reading or exporting cookies.
8. The login browser and broker close; the dedicated profile retains Crunchyroll's session.
9. The original media request resumes automatically at resolution, then presents the normal preview card.

## Explicit request unavailable on Crunchyroll

The assistant says that the requested title, episode, or language was not found on Crunchyroll and asks whether to try an unofficial anime source. Only an affirmative natural-language response starts the existing unofficial resolver. A decline or cancellation leaves voice active and clears the request.

# Architecture

```text
Realtime voice page
  ├── media confirmation card
  ├── QR pairing card
  └── existing media-continuity state
             │
             ▼
VoiceSessionController
  ├── CrunchyrollBrowserAdapter
  │     ├── authenticated-site search
  │     ├── exact watch URL validation
  │     └── typed unavailable/auth-required results
  ├── CrunchyrollLoginBroker subprocess
  │     ├── temporary LAN pairing server
  │     ├── phone remote-control application
  │     ├── loopback-only CDP client
  │     └── genuine Crunchyroll SSO window
  ├── PendingMediaCatalog
  │     └── CrunchyrollPlaybackLocator
  └── CrunchyrollPlayerManager
        ├── dedicated Brave app-mode process
        ├── player-focus extension
        └── playback lifecycle events
```

## Ownership boundaries

| Component | Owns | Must not own |
|---|---|---|
| Realtime model | Natural-language intent and strict tool selection | Credentials, URLs not returned by the controller, browser input, playback processes |
| Realtime page | QR/card rendering and existing media-continuity relay | Crunchyroll login DOM, cookies, CDP, source resolution, player launch |
| Crunchyroll browser adapter | Official-page search, exact episode metadata, authentication-state result | Passwords, confirmation state, playback lifecycle |
| Login broker subprocess | Temporary LAN server, encrypted phone channel, CDP login window, input constraints | Model calls, media decisions, persistent credential storage |
| Pending media catalog | Exact generation, display projection, server-private Crunchyroll locator | Browser session data or cookies |
| Player manager | One exact Brave playback generation, extension handshake, close/end/failure events | Search, credentials, model semantics |
| Brave profile | Crunchyroll cookies, site storage, Widevine state | Realtime voice state or application secrets |
| Player extension | Presentation CSS, exact playback lifecycle observation | Cookies permission, arbitrary navigation, login broker access, source search |

# Provider and tool contracts

Add one preparation tool:

```text
media_prepare_crunchyroll(query, episode?, audio_mode=auto)
```

`query`, `episode`, and `audio_mode` follow the existing bounded anime preparation semantics. The model calls this tool only for an explicit Crunchyroll request in this slice.

Typed outcomes include:

- `prepared`: exact `PendingMedia` created;
- `authentication_required`: QR login flow started or already active;
- `ambiguous`: bounded official-site candidates require clarification;
- `unavailable`: title, episode, or requested audio mode was not found;
- `provider_failure`: official page could not be queried safely.

Authentication is a controller continuation, not a model-visible credential exchange. After successful login, the controller resumes the original bounded preparation request. It does not ask the model to reconstruct the query.

An unavailable explicit request does not automatically invoke another tool. The model asks permission, and only the user's later affirmative response may call the existing `media_prepare_anime` fallback.

## Pending media extension

Extend `SourceKind` with `crunchyroll` and add a server-private locator:

```text
CrunchyrollPlaybackLocator
  canonical_watch_url
  series_id
  episode_id
  locale
  provider_resume = true
```

Validation requires:

- HTTPS;
- hostname exactly in the approved Crunchyroll watch-origin set;
- default port only;
- no username or password;
- a canonical `/watch/{episode_id}/...` path;
- bounded, validated identifiers;
- no fragment or controller secret;
- consistency between path and episode ID.

The model and Realtime page receive only the existing display-safe projection. The exact locator remains controller-private.

Crunchyroll owns resume position. The controller does not seek based on the unofficial source's local progress record. The preview should state “Resume with Crunchyroll progress” when the official page exposes a resume state, without inventing a timestamp that was not observed.

# Dedicated browser profile

Use a new profile root such as:

```text
~/.local/share/okay-hermes-realtime/crunchyroll-profile
```

Requirements:

- directory mode `0700`;
- separate from `voice_browser_profile` and every normal Brave profile;
- exactly one controller-owned Crunchyroll browser generation at a time;
- profile singleton locks released only after the exact owned process tree exits;
- login/search/player processes may never overlap on this profile;
- no cookie copying, cookie database reading, or profile synchronization;
- no password-saving prompts; the player shell never enables a password manager extension;
- logout or profile reset is an explicit local maintenance operation.

The profile is persistent because cookies and Widevine/site state must survive playback process exits. It is not deleted during ordinary teardown.

# Official-site resolver

`CrunchyrollBrowserAdapter` uses a bounded controller-owned Brave metadata session against official Crunchyroll pages. It does not call an advertised public API because none exists for this use, and it does not expose undocumented stream endpoints.

Resolution sequence:

1. Launch a metadata browser generation with the dedicated profile and loopback-only CDP.
2. Check a stable account page or authenticated UI marker. A redirect to SSO yields `authentication_required`.
3. Navigate only among allowlisted Crunchyroll top-level origins and search/series/watch paths.
4. Search the official site and extract a bounded candidate set from rendered page metadata.
5. Select only a high-confidence exact title; otherwise return `ambiguous`.
6. Resolve season, episode, locale, artwork, displayed language options, and canonical watch URL.
7. Validate the exact locator and build `PendingMedia`.
8. Close and reap the metadata browser before presenting or accepting playback.

The first implementation may use headless metadata browsing. If Crunchyroll blocks or materially changes the rendered flow, return a typed provider failure; do not fall back to private stream extraction. A visible resolver window is not an acceptable silent fallback.

# Phone remote login

## Why remote control instead of a local credential form

A generic credential form would make this application responsible for Crunchyroll's username/password schema and would break around CAPTCHA, MFA, consent, account migration, or SSO changes. The approved design keeps the real Crunchyroll page visible and interactive. The phone is only a constrained remote display and input surface.

Credentials still exist transiently in the phone page, encrypted transport, broker process, CDP message, and Brave process while input is delivered. Python and browser runtimes may copy strings internally, so the implementation must not claim perfect memory zeroization. The enforceable guarantees are no intentional persistence, no logs/traces, bounded lifetime, isolated process ownership, and immediate teardown.

## Broker isolation

Run login as a dedicated subprocess and process group. The main controller receives only:

- a pairing-ready message containing the ephemeral QR URL;
- bounded status transitions;
- terminal success, cancellation, timeout, or failure.

The status channel is an inherited private pipe or owner-only Unix socket. Secrets are never passed in argv, environment variables, stdout, stderr, or journald. Broker stdout/stderr are closed or routed through a redacting bounded diagnostic channel that cannot contain input events, screenshots, URLs with fragments, CDP payloads, or page text.

The broker owns the login Brave generation and must terminate/reap it on every terminal path.

## LAN pairing

The broker:

- selects the active trusted-LAN interface from the default route;
- accepts only a private IPv4 address or local IPv6 ULA selected by policy;
- binds that specific address, never `0.0.0.0` or `::`;
- chooses a random high port;
- permits one phone session;
- expires after five minutes;
- stops immediately after success, cancellation, browser exit, origin violation, replay, or protocol failure.

The QR URL uses an HTTP origin on the trusted LAN because a generally deployable locally trusted TLS certificate is unavailable. The high-entropy pairing secret remains in the URL fragment, so it is not sent in the initial HTTP request or ordinary server access logs.

The mobile application uses a vendored, reviewed cryptographic library rather than assuming Web Crypto is available on an insecure LAN origin. The protocol derives session keys from an ephemeral key agreement plus the 256-bit QR pairing secret and encrypts all screenshots, status messages, and input events with an authenticated cipher and monotonic sequence numbers. Replays, skipped bounds, and authentication failures terminate pairing.

Threat boundary: this protects against passive LAN observation and accidental cross-connection. It does not protect against an active trusted-LAN attacker who replaces the initial HTTP application code. Supporting hostile LANs requires a future trusted-HTTPS or external-relay design and is outside this slice.

No external CDN, analytics, font, script, or relay request is permitted from the phone page.

## Remote viewport

The broker captures the exact owned Brave page through CDP at a bounded rate and resolution. Frames:

- are compressed in memory;
- are encrypted before LAN transmission;
- are never written to disk, logs, traces, crash attachments, or browser caches;
- stop when the phone backgrounds for a bounded interval or disconnects;
- are cleared from application state on teardown.

The phone may send only:

- normalized pointer down/up/tap coordinates inside the page viewport;
- bounded vertical scrolling;
- printable Unicode text insertion;
- Enter, Tab, Backspace, Delete, Escape, and arrow keys;
- a local Cancel command.

Forbidden input includes:

- arbitrary JavaScript;
- DevTools commands;
- address-bar access;
- browser/system shortcuts;
- clipboard reads from the PC;
- file upload or file chooser control;
- navigation to a caller-supplied URL;
- shell commands or native input injection.

A hidden mobile textarea may collect text or password-manager paste on the phone, but it is a generic remote input surface rather than a mirrored Crunchyroll form. It must clear after each encrypted dispatch and must disable autocomplete/history where the mobile browser allows it.

## Navigation policy

The broker continuously validates the top-level page URL. Approved top-level origins are limited to the exact Crunchyroll SSO and website origins required for login and callback. CAPTCHA or identity-provider resources may render as embedded frames only when initiated by the approved top-level page; the phone cannot navigate them as arbitrary top-level destinations.

An unexpected top-level origin, new window, download, permission prompt, file chooser, or popup terminates the login flow and destroys the pairing session. The user receives a concise failure and may request a fresh QR session.

## Success detection

Success is not inferred from a submitted password or a URL substring alone. The broker verifies that an allowlisted Crunchyroll account page loads without an SSO redirect and exposes a bounded authenticated account marker. It does not read, serialize, export, or log cookies.

After success:

1. send terminal success through the private status channel;
2. clear the QR card;
3. close and reap the login browser;
4. release the profile lock;
5. stop the LAN server and destroy pairing state;
6. let the controller resume the original preparation request.

# Player shell

## Launch

After accepted confirmation and `media_ready`, launch Brave with:

- the dedicated Crunchyroll profile;
- `--app=<exact validated watch URL>`;
- fullscreen/kiosk presentation flags verified against the installed Brave build;
- a dedicated unpacked or packaged extension with a fixed identity;
- no remote debugging port;
- no arbitrary model-controlled flags;
- no password-store automation;
- a controller-generated non-secret playback generation identifier.

The player process gets a new owned process group. The same bounded TERM, group KILL fallback, and profile-lock cleanup rules used by the dedicated voice browser apply.

## Player-focus extension

The extension is limited to approved Crunchyroll watch pages and the fixed local lifecycle endpoint. It must not request cookie, history, downloads, clipboard, webRequest interception, debugger, or broad browsing permissions.

Responsibilities:

- apply presentation-only CSS that hides page furniture while retaining the official player and controls;
- request or preserve fullscreen presentation where browser policy permits;
- observe current episode identity, play/pause, ended, player errors, and watch-URL changes;
- report bounded lifecycle events for the exact active playback generation;
- detect navigation to a different episode and stop it before it becomes an uncontrolled autoplay continuation;
- degrade to the official Crunchyroll watch page if presentation selectors break.

The extension must not:

- inspect cookies, manifests, license requests, decrypted media, or account credentials;
- modify subtitle, dub, skip, or progress semantics;
- scrape unrelated account pages;
- connect to the login broker;
- execute controller-supplied JavaScript.

Lifecycle messages are accepted only from the fixed extension origin, while an exact controller-owned Crunchyroll playback generation is active, and when the reported canonical episode matches the pending locator. Stale, duplicate, mismatched, or post-teardown events are ignored or rejected.

## Voice continuity

The existing continuity contract remains:

1. successful `media_decide(accept)` creates a handoff;
2. the Realtime page begins continuity and sends `media_ready` only after local response audio has cleared;
3. the controller launches the player;
4. capture and unrelated task turns remain suspended during playback;
5. player end, close, or failure converges the exact process generation to zero;
6. the controller sends `media_finished`;
7. the page resumes exact or reconstructed voice continuity.

Authentication and metadata resolution happen before acceptance and do not mute the microphone. Only accepted playback enters media continuity.

## No autoplay

The extension reports natural episode end immediately. If Crunchyroll attempts to navigate or begin a different episode, the extension pauses/stops that continuation and reports the original generation complete. After voice restoration, the controller may resolve and present a next-episode card. Playback still requires a new natural-language acceptance.

# State machine

```text
idle
  → resolving_official
      → authentication_required
          → pairing
          → remote_login
          → authenticated
          → resolving_official
      → ambiguous
      → unavailable_waiting_fallback_consent
      → awaiting_confirmation
          → accepted_handoff
          → playing
          → restoring_voice
          → next_episode_confirmation | idle
```

Terminal transitions clear their exact owned state:

- cancellation during pairing clears the QR and original request;
- pairing timeout closes login Brave and leaves voice active;
- controller/tray Exit suppresses request resumption and next-episode prompts;
- stale login success cannot resume a superseded media generation;
- accepted playback cannot launch while metadata/login owns the profile;
- duplicate acceptance cannot start a second player.

# Failure behavior

| Failure | User-visible result | Cleanup |
|---|---|---|
| No trusted LAN address | Explain that phone setup cannot start on the current network | No broker/browser remains |
| Phone never connects | QR expires after five minutes | Server, login browser, pairing state, and request are cleared |
| Phone disconnects briefly | Permit one bounded reconnect only if the encrypted session and sequence continue | Otherwise terminate and require a fresh QR |
| Pairing authentication/replay failure | Report setup failure without protocol detail | Destroy keys and terminate broker/browser |
| Unexpected login origin or popup | Report that login left the allowed Crunchyroll flow | Terminate immediately |
| CAPTCHA/MFA cannot be completed | Keep genuine page available until timeout or cancel | No special bypass or challenge extraction |
| Login rejected | Keep the genuine error visible for another bounded attempt in the same paired session | Never log fields or page text |
| Session expires later | Reopen QR login and resume the bounded original request after success | Player does not launch unauthenticated |
| Official title/episode unavailable | Ask before unofficial fallback | No unofficial resolver starts yet |
| Official resolver layout changes | Concise Crunchyroll-unavailable error | Metadata browser is reaped; no silent private-API fallback |
| Player extension selector breaks | Show official watch page in app mode | Playback remains official; record bounded non-sensitive diagnostic |
| Widevine/player error | Restore voice and report playback failure | Exact Brave generation is stopped/reaped |
| Extension lifecycle channel fails | Treat owned player process exit as fallback completion; otherwise allow manual close and bounded watchdog | Never orphan process or permanently mute voice |
| Controller/tray Exit | Terminal shutdown | Login broker, LAN socket, metadata browser, player browser, and voice browser converge to zero |

# Threat model

## Protected assets

- Crunchyroll password and MFA text;
- authenticated profile cookies and site storage;
- pairing secret and ephemeral session keys;
- login screenshots and typed input;
- exact media generation and lifecycle authority;
- local microphone and Realtime continuity state.

## Defended threats

- passive LAN observation of login screenshots and input;
- random or stale phone connection attempts;
- replayed pairing/input messages;
- model attempts to inject URLs, flags, browser input, or credentials;
- stale media or authentication generations;
- accidental logging or command-line exposure;
- arbitrary top-level navigation during remote login;
- player extension privilege creep;
- process/profile overlap and orphaned browser trees.

## Explicitly trusted

- the local user account and controller installation;
- the phone being physically controlled by the user;
- the selected home LAN having no active code-injection attacker;
- Brave, Crunchyroll, Widevine, and the mobile browser;
- the reviewed vendored cryptographic library.

## Not defended in this slice

- an active attacker replacing the broker's HTTP mobile application on the LAN;
- a compromised phone, Brave build, Crunchyroll page, operating system, or local user account;
- screenshots captured by the phone OS or mobile browser;
- Crunchyroll account restrictions, regional availability, or service-side policy changes;
- perfect erasure of runtime string copies from managed-language memory.

# Verification strategy

Implementation follows RED → GREEN in bounded slices.

## Contract tests

- `media_prepare_crunchyroll` has an exact strict schema and is absent from unrelated tool paths.
- Explicit Crunchyroll unavailable results require a later consent turn before unofficial preparation.
- `SourceKind=crunchyroll` and `CrunchyrollPlaybackLocator` reject malformed hosts, ports, paths, fragments, credentials, mismatched IDs, and control characters.
- Locator details never appear in model-visible or page-visible output.
- Provider-managed resume does not reuse unofficial local seek state.

## Profile and browser tests

- Voice, normal Brave, and Crunchyroll profile paths cannot alias.
- Profile permissions are owner-only.
- Metadata/login/player generations are mutually exclusive.
- Login and metadata launches bind CDP to loopback only.
- Playback launches without CDP.
- No credential, pairing secret, or sensitive URL fragment appears in argv, environment, diagnostics, or service logs.
- Every close/error/timeout path reaps the exact process group and releases stale singleton locks safely.

## Pairing protocol tests

- Pairing secret has at least 256 bits of entropy and appears only in the QR fragment/in-memory state.
- Broker binds one selected private interface and rejects wildcard/public binding.
- One phone session, five-minute expiry, replay rejection, monotonic counters, authenticated encryption, and terminal key destruction.
- Corrupt, duplicate, out-of-order, oversized, or unauthenticated frames terminate safely.
- No plaintext frame, input, page text, or screenshot reaches test logs or filesystem fixtures.
- Passive capture fixtures cannot recover typed text or screenshot bytes.
- Active-LAN replacement remains documented as an unsupported threat rather than falsely claimed solved.

## Remote-control tests

- Allowed pointer, scroll, printable text, and navigation keys map to bounded CDP calls.
- Coordinates, text length, rate, frame dimensions, and frame rate are capped.
- Address-bar/system shortcuts, arbitrary CDP methods, JavaScript, file chooser, downloads, popups, and non-allowlisted navigation terminate or reject.
- Success requires an authenticated account marker, not merely a callback URL.
- Cancellation, disconnect, timeout, browser crash, stale success, and service shutdown clear the QR and original request correctly.

## Resolver tests

- Official-page fixtures cover exact title, ambiguity, seasons, episode mapping, artwork, locale, sub/dub display, resume marker, unavailable episode, and login redirect.
- Layout/parser failures are typed and bounded.
- Canonical watch URL is obtained from rendered official-site data and validated before catalog insertion.
- Metadata browser closes before the confirmation card becomes actionable.

## Extension and player tests

- Manifest permissions are exact and contain no cookie, history, downloads, clipboard, debugger, or broad-host access.
- Presentation CSS degrades to the official page when selectors fail.
- Lifecycle messages require fixed extension origin, active playback ID, and matching episode.
- Stale/duplicate/mismatched lifecycle events cannot stop or finish another generation.
- URL change to another episode stops autoplay and returns to voice.
- Process exit without an extension event still restores voice through bounded fallback.
- Widevine/player launch failure and controller teardown leave zero owned player processes.

## End-to-end tests

Automated local E2E uses fake Crunchyroll pages and a fake CDP browser to exercise pairing, remote input, authentication continuation, preview, acceptance, player events, teardown, and fallback consent without real credentials.

Manual acceptance on the live service verifies:

1. New explicit Crunchyroll request shows QR with no PC keyboard/mouse interaction.
2. Phone connects on the trusted LAN and controls the genuine SSO page.
3. Password, CAPTCHA/MFA when present, and login errors never appear in logs/traces.
4. Successful login resumes the original request and presents the exact episode preview.
5. Natural confirmation launches the official player-focused Brave shell with subtitles/dubs and skip controls available.
6. Player close and natural end restore voice.
7. Next episode requires confirmation.
8. Expired session repeats QR login.
9. Explicit unavailable episode asks before the unofficial fallback.
10. Cancel, timeout, tray Exit, and controller stop leave zero broker/browser/player processes and no open LAN port.

# Delivery slices

1. Extend typed media contracts and strict Crunchyroll tool routing.
2. Add dedicated profile and mutually exclusive metadata/login/player process ownership.
3. Implement official-site metadata resolution with fixtures and typed authentication/unavailable outcomes.
4. Implement broker subprocess protocol and QR card lifecycle.
5. Implement encrypted phone remote application and constrained CDP input.
6. Implement authenticated success continuation and session-expiry recovery.
7. Implement fixed-identity player-focus extension and Brave player manager.
8. Integrate media continuity, completion, no-autoplay, and consented unofficial fallback.
9. Run full automated gates, package/install verification, security log scan, and live phone/Crunchyroll acceptance.

# Approval state

The user approved:

- official Brave player-shell playback;
- login cookies persisted in a dedicated profile;
- no password retention;
- QR setup over the same trusted LAN;
- phone remote control of the genuine Brave/Crunchyroll login page;
- explicit Crunchyroll selection during this slice;
- asking before unofficial fallback when an explicit Crunchyroll request is unavailable;
- later generalization to user-selected video, music, podcast, audiobook, and future media providers.
