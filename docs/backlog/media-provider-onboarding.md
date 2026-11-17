# Media provider onboarding backlog

## Purpose

Track the future provider registry and keyboard-free onboarding system for media services. This is a product backlog, not an approved implementation specification. Each implementation slice still requires a reviewed design and plan.

## Product direction

Users should be able to choose and prioritize media providers without using a keyboard or mouse. Preferences are ordered per media domain rather than globally.

Initial domains:

- anime and video;
- music;
- podcasts;
- audiobooks.

The registry must allow future media domains and providers without embedding anime- or Crunchyroll-specific assumptions in shared preference logic.

## Provider selection rules

1. An explicit source request overrides saved preferences, such as “watch this on Crunchyroll” or “play this on Spotify.”
2. Otherwise, try the user’s enabled providers in their saved order for the relevant media domain.
3. Skip unavailable, unauthenticated, or incapable providers with a bounded reason.
4. Apply domain-specific fallback policy only after preferred providers are exhausted.
5. For anime, unofficial sources may be used as the final fallback.
6. Do not silently substitute a materially different title, episode, language, edition, or recording.

## Shared provider contract

Each provider adapter should expose only the capabilities it supports:

- availability lookup and bounded search;
- authentication state and setup requirements;
- preview metadata and artwork;
- exact item or episode resolution;
- playback launch and lifecycle ownership;
- language, subtitle, dub, or quality capabilities;
- resume and progress synchronization;
- completion and next-item reporting;
- logout, session expiry, and recovery behavior.

Provider-specific credentials, cookies, tokens, manifests, and DRM material must remain behind the provider boundary.

## QR onboarding

Build a first-run, phone-assisted setup flow using an ephemeral LAN broker:

- bind only to the active trusted LAN interface;
- show a QR code containing a short-lived, single-use pairing URL;
- protect submissions with a high-entropy pairing secret and application-layer encryption;
- accept one phone connection and expire after five minutes;
- let the user enable providers and order them per media domain;
- support provider-specific authentication steps;
- retain provider sessions and preferences, but never retain submitted account passwords;
- destroy broker secrets and transient credentials after success, failure, cancellation, or timeout;
- allow the same flow to reopen later for preference changes or reauthentication.

The voice assistant, model context, logs, traces, configuration files, and command lines must never receive account passwords.

## First implementation slice: Crunchyroll

Use Crunchyroll as the first authenticated official anime provider.

Target playback experience:

- dedicated owner-only Brave profile;
- official Crunchyroll page and Widevine playback pipeline;
- app-mode, player-focused window with browser and page chrome minimized;
- exact episode URL launch after the existing preview and natural-language confirmation flow;
- official subtitles, dubs, progress, and intro/outro controls;
- controller-owned browser lifecycle and voice-session restoration;
- persistent authenticated cookies only;
- QR/mobile reauthentication when the Crunchyroll session expires;
- no stream extraction, cookie export, password storage, or DRM handoff to `mpv`.

Until general provider onboarding exists, Crunchyroll selection remains explicit in the user request. Existing unofficial anime resolution remains the fallback path.

## Future provider examples

These are candidates, not commitments:

- video: Crunchyroll and other user-selected streaming platforms;
- music: Spotify, local libraries, and other preferred players or services;
- podcasts: subscribed podcast clients and feeds;
- audiobooks: local libraries and authenticated audiobook services.

Each provider requires its own security, authentication, playback, and lifecycle review before activation.

## Delivery slices

- [ ] Define the media-domain and provider-adapter contracts.
- [ ] Define durable preference storage and explicit-source override semantics.
- [ ] Design the ephemeral LAN pairing and mobile onboarding protocol.
- [ ] Design and implement the Crunchyroll Brave player shell.
- [ ] Implement QR/mobile Crunchyroll authentication without password retention.
- [ ] Integrate provider availability and fallback selection with media confirmation.
- [ ] Add session-expiry, cancellation, teardown, and voice-restoration tests.
- [ ] Add first-run platform selection and preference reordering.
- [ ] Extend the registry to music providers without changing shared contracts.

## Non-goals for the first slice

- extracting or decrypting DRM-protected streams for `mpv`;
- storing provider passwords;
- implementing every media platform at once;
- asking the user to select a provider on every request;
- autoplaying the next episode without confirmation;
- treating unofficial providers as interchangeable with official account-backed services.

## Required follow-up artifacts

Before implementation, write and approve:

1. a provider-registry and QR-onboarding design under `docs/superpowers/specs/`;
2. a bounded Crunchyroll player-shell design or clearly separated section in that specification;
3. an implementation plan under `docs/plans/`;
4. a threat model covering LAN pairing, transient credentials, browser-profile isolation, debugging interfaces, logs, and teardown.
