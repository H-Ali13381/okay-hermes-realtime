import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const root = new URL("../../", import.meta.url);
const hosts = JSON.parse(
  readFileSync(new URL("src/realtime_action_spike/media_image_hosts.json", root), "utf8"),
).hosts;
const bundle = readFileSync(
  new URL("src/realtime_action_spike/web/voice.js", root),
  "utf8",
);

test("shipped browser bundle contains every shared image host", () => {
  for (const host of hosts) {
    assert.ok(bundle.includes(host), `bundle is missing image host ${host}`);
  }
});
