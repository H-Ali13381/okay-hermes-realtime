import { readFile, writeFile } from "node:fs/promises";

const bundlePath = new URL("../src/realtime_action_spike/web/voice.js", import.meta.url);
const source = await readFile(bundlePath, "utf8");
const normalized = source.replace(/[\t ]+$/gmu, "");
await writeFile(bundlePath, normalized, "utf8");
