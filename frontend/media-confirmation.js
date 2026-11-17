import imageHosts from "../src/realtime_action_spike/media_image_hosts.json" with { type: "json" };

const ROOT_KEYS = new Set([
  "type",
  "session_id",
  "pending_id",
  "source",
  "title",
  "subtitle",
  "thumbnail_url",
  "prompt",
  "episode",
  "audio_mode",
  "resume",
]);
const EPISODE_KEYS = new Set([
  "series_title",
  "season_number",
  "episode_number",
  "episode_title",
]);
const RESUME_KEYS = new Set(["start_seconds", "duration_seconds", "reason"]);
const SAFE_ID = /^[A-Za-z0-9_-]{8,128}$/u;
const IMAGE_HOSTS = new Set(imageHosts.hosts);

function assertExactKeys(value, allowed, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`Invalid media confirmation ${label}`);
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new Error(`Unknown media confirmation field: ${key}`);
  }
}

function boundedText(value, label, maximum = 300) {
  if (typeof value !== "string") throw new Error(`Invalid media confirmation ${label}`);
  const normalized = value.replace(/\s+/gu, " ").trim();
  if (!normalized || normalized.length > maximum || /[\u0000-\u001f\u007f]/u.test(normalized)) {
    throw new Error(`Invalid media confirmation ${label}`);
  }
  return normalized;
}

function parseThumbnail(value) {
  if (value === null) return null;
  if (typeof value !== "string" || value.length > 2048) {
    throw new Error("Invalid media confirmation thumbnail");
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch (_error) {
    throw new Error("Invalid media confirmation thumbnail");
  }
  if (
    parsed.protocol !== "https:"
    || parsed.username
    || parsed.password
    || parsed.port
    || parsed.hash
    || !IMAGE_HOSTS.has(parsed.hostname)
  ) {
    throw new Error("Invalid media confirmation thumbnail");
  }
  return parsed.href;
}

function parseEpisode(value) {
  if (value === null) return null;
  assertExactKeys(value, EPISODE_KEYS, "episode");
  if (!Number.isInteger(value.episode_number) || value.episode_number < 1) {
    throw new Error("Invalid media confirmation episode");
  }
  if (
    value.season_number !== null
    && (!Number.isInteger(value.season_number) || value.season_number < 1)
  ) {
    throw new Error("Invalid media confirmation season");
  }
  return {
    seriesTitle: boundedText(value.series_title, "series title"),
    seasonNumber: value.season_number,
    episodeNumber: value.episode_number,
    episodeTitle: value.episode_title === null
      ? null
      : boundedText(value.episode_title, "episode title"),
  };
}

function parseResume(value) {
  if (value === null) return null;
  assertExactKeys(value, RESUME_KEYS, "resume");
  if (!Number.isFinite(value.start_seconds) || value.start_seconds < 0) {
    throw new Error("Invalid media confirmation resume position");
  }
  if (
    value.duration_seconds !== null
    && (!Number.isFinite(value.duration_seconds) || value.duration_seconds <= 0)
  ) {
    throw new Error("Invalid media confirmation duration");
  }
  return {
    startSeconds: value.start_seconds,
    durationSeconds: value.duration_seconds,
    reason: boundedText(value.reason, "resume reason"),
  };
}

export function parseMediaConfirmation(event, expectedSessionId) {
  assertExactKeys(event, ROOT_KEYS, "message");
  if (event.type !== "media_confirmation") {
    throw new Error("Invalid media confirmation type");
  }
  if (event.session_id !== expectedSessionId || !SAFE_ID.test(event.session_id || "")) {
    throw new Error("Invalid media confirmation session");
  }
  if (!SAFE_ID.test(event.pending_id || "")) {
    throw new Error("Invalid media confirmation pending id");
  }
  if (!new Set(["youtube", "anime"]).has(event.source)) {
    throw new Error("Invalid media confirmation source");
  }
  if (event.audio_mode !== null && !new Set(["sub", "dub"]).has(event.audio_mode)) {
    throw new Error("Invalid media confirmation audio mode");
  }
  const title = boundedText(event.title, "title");
  const subtitle = boundedText(event.subtitle, "subtitle");
  const sourceLabel = event.source === "youtube" ? "YouTube" : "Anime";
  const modeLabel = event.audio_mode === "sub"
    ? "Subbed"
    : event.audio_mode === "dub" ? "Dubbed" : null;
  return Object.freeze({
    pendingId: event.pending_id,
    source: event.source,
    title,
    subtitle,
    thumbnailUrl: parseThumbnail(event.thumbnail_url),
    prompt: boundedText(event.prompt, "prompt"),
    episode: parseEpisode(event.episode),
    audioMode: event.audio_mode,
    resume: parseResume(event.resume),
    badge: modeLabel ? `${sourceLabel} · ${modeLabel}` : sourceLabel,
  });
}

export class MediaConfirmationState {
  constructor(sessionId) {
    if (!SAFE_ID.test(sessionId || "")) throw new Error("Invalid media confirmation session");
    this.sessionId = sessionId;
    this.current = null;
  }

  replace(event) {
    this.current = parseMediaConfirmation(event, this.sessionId);
    return this.current;
  }

  clearExact(pendingId) {
    if (!this.current || this.current.pendingId !== pendingId) return false;
    this.current = null;
    return true;
  }

  clear() {
    this.current = null;
  }
}

const CHOICES_ROOT_KEYS = new Set(["type", "session_id", "query", "pending_ids", "choices"]);
const CHOICE_KEYS = new Set(["position", "series_id", "title"]);
const SERIES_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*-\d+$/u;

export function parseMediaChoices(event, expectedSessionId) {
  assertExactKeys(event, CHOICES_ROOT_KEYS, "choices message");
  if (event.type !== "media_choices") {
    throw new Error("Invalid media choices type");
  }
  if (event.session_id !== expectedSessionId || !SAFE_ID.test(event.session_id || "")) {
    throw new Error("Invalid media choices session");
  }
  const query = boundedText(event.query, "choices query");
  if (!Array.isArray(event.pending_ids) || !Array.isArray(event.choices)) {
    throw new Error("Invalid media choices payload");
  }
  if (
    event.pending_ids.length < 2
    || event.pending_ids.length > 5
    || event.pending_ids.length !== event.choices.length
  ) {
    throw new Error("Invalid media choices count");
  }
  for (const pendingId of event.pending_ids) {
    if (!SAFE_ID.test(pendingId || "")) throw new Error("Invalid media choices pending id");
  }
  const seenPositions = new Set();
  const choices = event.choices.map((choice, index) => {
    assertExactKeys(choice, CHOICE_KEYS, "choice");
    if (!Number.isInteger(choice.position)) throw new Error("Invalid media choice position");
    if (choice.position !== index + 1) {
      throw new Error("Invalid media choice ordering");
    }
    if (!SERIES_ID.test(choice.series_id || "")) {
      throw new Error("Invalid media choice series id");
    }
    seenPositions.add(choice.position);
    return Object.freeze({
      position: choice.position,
      seriesId: choice.series_id,
      title: boundedText(choice.title, "choice title"),
    });
  });
  return Object.freeze({ query, pendingIds: [...event.pending_ids], choices });
}
