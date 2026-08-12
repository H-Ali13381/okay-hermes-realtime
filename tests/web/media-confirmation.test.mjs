import assert from "node:assert/strict";
import test from "node:test";

import {
  MediaConfirmationState,
  parseMediaConfirmation,
} from "../../frontend/media-confirmation.js";

function confirmation(overrides = {}) {
  return {
    type: "media_confirmation",
    session_id: "local-session-1234",
    pending_id: "pending-anime-01",
    source: "anime",
    title: "Cowboy Bebop",
    subtitle: "Episode 2 · Subbed",
    thumbnail_url: "https://cdn.xlsbox.com/poster/small/1065.jpg",
    prompt: "Play Cowboy Bebop, Episode 2 · Subbed?",
    episode: {
      series_title: "Cowboy Bebop",
      season_number: null,
      episode_number: 2,
      episode_title: null,
    },
    audio_mode: "sub",
    resume: null,
    ...overrides,
  };
}

test("parses a safe exact-session media confirmation", () => {
  const parsed = parseMediaConfirmation(confirmation(), "local-session-1234");

  assert.equal(parsed.pendingId, "pending-anime-01");
  assert.equal(parsed.title, "Cowboy Bebop");
  assert.equal(parsed.subtitle, "Episode 2 · Subbed");
  assert.equal(parsed.prompt, "Play Cowboy Bebop, Episode 2 · Subbed?");
  assert.equal(parsed.thumbnailUrl, "https://cdn.xlsbox.com/poster/small/1065.jpg");
  assert.equal(parsed.badge, "Anime · Subbed");
});

test("rejects stale sessions, unsafe images, unknown fields, and private locators", () => {
  assert.throws(
    () => parseMediaConfirmation(confirmation(), "other-session-1234"),
    /session/i,
  );
  assert.throws(
    () => parseMediaConfirmation(confirmation({ thumbnail_url: "http://evil.test/a.jpg" }), "local-session-1234"),
    /thumbnail/i,
  );
  assert.throws(
    () => parseMediaConfirmation(confirmation({ stream_url: "https://private.test/a.m3u8" }), "local-session-1234"),
    /field/i,
  );
  assert.throws(
    () => parseMediaConfirmation(confirmation({ playback_locator: {} }), "local-session-1234"),
    /field/i,
  );
});

test("replacement is exact and stale clear cannot hide a newer card", () => {
  const state = new MediaConfirmationState("local-session-1234");
  const first = state.replace(confirmation());
  const second = state.replace(confirmation({
    pending_id: "pending-anime-02",
    subtitle: "Episode 3 · Dubbed",
    audio_mode: "dub",
  }));

  assert.equal(first.pendingId, "pending-anime-01");
  assert.equal(second.pendingId, "pending-anime-02");
  assert.equal(state.clearExact("pending-anime-01"), false);
  assert.equal(state.current.pendingId, "pending-anime-02");
  assert.equal(state.clearExact("pending-anime-02"), true);
  assert.equal(state.current, null);
});

test("null artwork degrades to a text-only card", () => {
  const parsed = parseMediaConfirmation(
    confirmation({ thumbnail_url: null }),
    "local-session-1234",
  );

  assert.equal(parsed.thumbnailUrl, null);
});
