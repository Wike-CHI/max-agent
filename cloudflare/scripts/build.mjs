import { cp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { SECURITY_HEADERS } from "../src/worker.mjs";
import { gitProvenance } from "./provenance.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const destination = join(root, "cloudflare/public");
// Read the checked-out commit, not a caller-provided version label.
const provenance = gitProvenance(root);
// Never let deploy/runtime credentials become part of Vite's build environment.
const buildEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  !/^(VITE_|MAX_|CLOUDFLARE_|CF_)/i.test(key),
));
const result = spawnSync(process.platform === "win32" ? "npm.cmd" : "npm", ["run", "build"], {
  cwd: join(root, "frontend"), env: buildEnv, stdio: "inherit",
});
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);
const source = join(root, "src/max_agent/web/dist");
const html = await readFile(join(source, "index.html"), "utf8");
if (!html.includes('/console/assets/')) throw new Error("Expected Vite base /console/.");
await rm(destination, { recursive: true, force: true });
await mkdir(join(destination, "console"), { recursive: true });
await cp(source, join(destination, "console"), { recursive: true });
await writeFile(join(destination, "console/version.json"), JSON.stringify(provenance, null, 2) + "\n");
// Static assets bypass the Worker and therefore need their own response headers.
await writeFile(join(destination, "_headers"), [
  "/*",
  ...Object.entries(SECURITY_HEADERS).map(([key, value]) => `  ${key}: ${value}`),
  "",
  "/console/",
  "  Cache-Control: no-cache",
  "",
  "/console/version.json",
  "  Cache-Control: no-store, max-age=0",
  "",
  "/console/assets/*",
  "  Cache-Control: public, max-age=31536000, immutable",
  "",
].join("\n"));

async function inspect(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) await inspect(path);
    else if (entry.name.startsWith(".") || /\.(map|py|ts|tsx)$/.test(entry.name)) {
      throw new Error("Unexpected source/configuration file in static assets.");
    }
  }
}
await inspect(destination);
console.log("Built cloudflare/public/console/ (no runtime API origin or bearer token is embedded).");
