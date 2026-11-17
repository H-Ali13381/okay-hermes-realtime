import { readFileSync } from "node:fs";

import { parseMediaConfirmation } from "../../frontend/media-confirmation.js";

const payloadPath = process.argv[2];
if (!payloadPath) {
  console.error("usage: node parse-media-confirmation.mjs <payload.json>");
  process.exit(2);
}

const payload = JSON.parse(readFileSync(payloadPath, "utf8"));
const parsed = parseMediaConfirmation(payload, payload.session_id);
console.log(parsed.thumbnailUrl ?? "");
