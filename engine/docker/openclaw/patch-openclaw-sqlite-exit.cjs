"use strict";

const { createHash } = require("node:crypto");
const { readFileSync, readdirSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");

const root = process.argv[2];
if (!root || process.argv.length !== 3) {
  throw new Error("Usage: patch-openclaw-sqlite-exit.cjs OPENCLAW_ROOT");
}
const manifest = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));
if (manifest.name !== "openclaw" || manifest.version !== "2026.10.6") {
  throw new Error("SQLite exit patch requires OpenClaw 2026.10.6; review the upstream helper before upgrading");
}
const dist = join(root, "dist");
const candidates = readdirSync(dist).filter((name) =>
  /^sqlite-wal-[\w-]+\.mjs$/.test(name) &&
  readFileSync(join(dist, name), "utf8").includes("function registerSqliteCacheExitClose(closeAll) {"),
);
if (candidates.length !== 1) {
  throw new Error(`Expected one SQLite WAL module, found ${candidates.length}`);
}
const target = join(dist, candidates[0]);
const source = readFileSync(target, "utf8");
const start = source.indexOf("let lastSqliteExitGroup;\n");
const registration = source.indexOf("function registerSqliteCacheExitClose(closeAll) {", start);
const end = source.indexOf("\n}\n", registration) + 3;
const helper = source.slice(start, end);
if (createHash("sha256").update(helper).digest("hex") !==
    "d8fce98b1732e61a1c2aad6a872d10ef09077ed8a6afef58b06a9ee2d7793ed9") {
  throw new Error("Unknown SQLite exit helper; refusing to modify installed OpenClaw");
}
// Unrelated exit owners must not fragment this cache registry into extra listeners.
const patched = helper.replace(
  'if (!group || process.listeners("exit").at(-1) !== group.dispatch) {',
  "if (!group) {",
);
writeFileSync(target, source.slice(0, start) + patched + source.slice(end));
