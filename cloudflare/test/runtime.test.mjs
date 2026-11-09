/** Real workerd + static assets smoke tests. Run after npm run build. */
import test from "node:test";
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const root = fileURLToPath(new URL("..", import.meta.url));
const config = JSON.parse(await readFile(resolve(root, "wrangler.jsonc"), "utf8"));
const origin = "https://console.example.com";
function runtime(bindings = {}) {
  return new Miniflare(convertV4MiniflareOptions({
    host: "127.0.0.1", port: 0, telemetry: { enabled: false }, cf: false,
    name: config.name, modules: true, scriptPath: resolve(root, config.main),
    compatibilityDate: config.compatibility_date, bindings,
    assets: {
      directory: resolve(root, config.assets.directory), binding: config.assets.binding,
      run_worker_first: config.assets.run_worker_first,
      routerConfig: { has_user_worker: true },
      assetConfig: { html_handling: config.assets.html_handling, not_found_handling: config.assets.not_found_handling },
    },
  }));
}

test("workerd serves /console/ with security headers and hashed static assets", async () => {
  const mf = runtime();
  try {
    const rootResponse = await mf.dispatchFetch(origin + "/", { redirect: "manual" });
    assert.equal(rootResponse.status, 302);
    assert.equal(rootResponse.headers.get("Location"), "/console/");
    const response = await mf.dispatchFetch(origin + "/console/");
    assert.equal(response.status, 200);
    assert.match(response.headers.get("Content-Security-Policy"), /connect-src 'self'/);
    assert.match(response.headers.get("Content-Security-Policy"), /frame-ancestors 'none'/);
    assert.equal(response.headers.get("Cache-Control"), "no-cache");
    assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
    assert.equal(response.headers.get("Set-Cookie"), null);
    const html = await response.text();
    const js = html.match(/src="([^\"]+\.js)"/)?.[1];
    const css = html.match(/href="([^\"]+\.css)"/)?.[1];
    assert.ok(js?.startsWith("/console/assets/"));
    assert.ok(css?.startsWith("/console/assets/"));
    for (const path of [js, css]) {
      const asset = await mf.dispatchFetch(origin + path);
      assert.equal(asset.status, 200);
      assert.equal(asset.headers.get("Cache-Control"), "public, max-age=31536000, immutable");
      assert.equal(asset.headers.get("X-Content-Type-Options"), "nosniff");
      const content = await asset.text();
      assert.doesNotMatch(content, /MAX_API_ORIGIN|MAX_AGENT_API_TOKEN|CLOUDFLARE_API_TOKEN|unit-test-only-not-a-real-token/);
    }
    for (const path of ["/console/not-a-page", "/console/.env", "/.dev.vars", "/_headers"]) {
      assert.equal((await mf.dispatchFetch(origin + path)).status, 404, path);
    }
    for (const filename of await readdir(resolve(root, "public/console/assets"))) {
      assert.match(filename, /\.(js|css)$/);
    }
  } finally { await mf.dispose(); }
});

test("workerd intercepts every API route before assets and fails closed without a backend", async () => {
  const mf = runtime();
  try {
    for (const [path, method, status] of [
      ["/v1/tasks/query", "GET", 503], ["/v1/tasks/query", "POST", 405],
      ["/v1/tasks/query", "OPTIONS", 405], ["/v1/tasks", "GET", 404],
      ["/v1", "GET", 404], ["/v1/tasks/query/", "GET", 404], ["/v1/unknown", "GET", 404],
    ]) {
      const response = await mf.dispatchFetch(origin + path, { method, redirect: "manual" });
      assert.equal(response.status, status, `${method} ${path}`);
      assert.match(response.headers.get("Content-Type"), /application\/json/);
      assert.match(response.headers.get("Cache-Control"), /no-store/);
      assert.equal(response.headers.get("Set-Cookie"), null);
      assert.doesNotMatch(await response.text(), /<html|<script/i);
    }
  } finally { await mf.dispose(); }
});

test("workerd checks browser bearer credentials when a valid backend is configured", async () => {
  const mf = runtime({ MAX_API_ORIGIN: "https://api.example.com" });
  try {
    const response = await mf.dispatchFetch(origin + "/v1/tasks/query");
    assert.equal(response.status, 401);
    assert.equal(response.headers.get("WWW-Authenticate"), "Bearer");
    assert.match(response.headers.get("Cache-Control"), /no-store/);
  } finally { await mf.dispose(); }
});
