import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import worker, { approvedOrigin, allowedRoute, allowedQuery, handleRequest, SECURITY_HEADERS } from "../src/worker.mjs";

const CONSOLE = "https://max-agent-console.workers.dev";
const API = "https://api.example.com";
const ID = "bf833e9c-c36b-4916-bdd5-6d7490b57ea1";
const TOKEN = "unit-test-only-not-a-real-token";
const PATHS = ["/v1/tasks/query", `/v1/tasks/${ID}/status`, `/v1/tasks/${ID}/trace`, `/v1/tasks/${ID}/events/query`];
function request(path = PATHS[0], options = {}) {
  return new Request(CONSOLE + path, { headers: { Authorization: `Bearer ${TOKEN}` }, ...options });
}
function environment(overrides = {}) {
  return { MAX_API_ORIGIN: API, ASSETS: { fetch() { assert.fail("API requests must not reach static assets"); } }, ...overrides };
}
function neverFetch() { assert.fail("request must not be forwarded"); }
function json(body = { items: [], next_cursor: null }, options = {}) {
  return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" }, ...options });
}
async function expectError(response, status) {
  assert.equal(response.status, status);
  assert.match(response.headers.get("Cache-Control"), /no-store/);
  assert.equal(response.headers.get("Set-Cookie"), null);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
  assert.equal(response.headers.get("Content-Type"), "application/json; charset=utf-8");
  const text = await response.text();
  assert.ok(!text.includes(TOKEN));
  assert.ok(!text.includes(API));
  return JSON.parse(text);
}

test("only the four exact GET console routes are forwarded", async () => {
  for (const path of PATHS) {
    let calls = 0;
    const response = await handleRequest(request(path), environment(), async (url, init) => {
      calls++;
      assert.equal(url, API + path);
      assert.equal(init.method, "GET");
      return json({ ok: true });
    });
    assert.equal(response.status, 200);
    assert.equal(calls, 1);
  }
});

test("unlisted endpoints, malformed IDs and encoded route tricks fail closed", async () => {
  for (const path of [
    "/v1", "/v1/", "/v1/health", "/v1/ready", "/v1/tasks", "/v1/metrics", "/v1/queue/run",
    `/v1/tasks/${ID}`, `/v1/tasks/${ID}/approve`, `/v1/tasks/${ID}/cancel`, `/v1/tasks/${ID}/schedule`,
    `/v1/tasks/${ID}/events`, "/v1/tasks/query/", "/v1/tasks/not-a-uuid/status",
    `/v1/tasks/${ID}/status/`, `/v1/tasks/${ID}%2fstatus`, `/v1/tasks/${ID}/%73tatus`,
    `/v1/tasks/${ID}%252fstatus`, "/v1//tasks/query", "/v1/tasks/query.json",
  ]) {
    assert.equal(allowedRoute(new URL(CONSOLE + path).pathname), null, path);
    await expectError(await handleRequest(request(path), environment(), neverFetch), 404);
  }
});

test("POST, PUT, PATCH, DELETE, HEAD and OPTIONS never reach the upstream", async () => {
  for (const method of ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]) {
    for (const path of [...PATHS, `/v1/tasks/${ID}/cancel`]) {
      const response = await handleRequest(request(path, { method }), environment(), neverFetch);
      assert.equal(response.headers.get("Allow"), "GET");
      await expectError(response, 405);
    }
  }
});

test("origin must be a fixed public DNS HTTPS origin, never credentials or local hosts", async () => {
  for (const value of [
    undefined, null, "", " ", "https://api.example.com ", "http://api.example.com", "//api.example.com",
    "https:api.example.com", "https://user:password@api.example.com", "https://api.example.com/v1",
    "https://api.example.com/?token=secret", "https://api.example.com/#part", "https://api.example.com:8443",
    "https://localhost", "https://x.localhost", "https://api.local", "https://api.internal", "https://api.home.arpa",
    "https://api.lan", "https://api", "https://api.test", "https://127.0.0.1", "https://2130706433",
    "https://0x7f000001", "https://0177.0.0.1", "https://10.0.0.1", "https://169.254.169.254",
    "https://[::1]", "https://[fd00::1]", "https://8.8.8.8", "https://api.example.com.",
    "https://api_example.com", "https://api.-example.com", "https://api.example.com\\evil", CONSOLE,
  ]) {
    assert.equal(approvedOrigin(value, CONSOLE), null, String(value));
    await expectError(await handleRequest(request(), environment({ MAX_API_ORIGIN: value }), neverFetch), 503);
  }
  for (const value of [API, API + "/", API + ":443", "https://API.EXAMPLE.COM"]) {
    assert.equal(approvedOrigin(value, CONSOLE), API);
  }
});

test("missing or malformed bearer credentials return a challenge without forwarding", async () => {
  for (const auth of [undefined, "", "Basic abc", "Bearer", "Bearer ", "Bearer a b", "Bearer token, Bearer other", "Bearer 'secret'", `Bearer ${"a".repeat(8200)}`]) {
    const headers = auth === undefined ? {} : { Authorization: auth };
    const response = await handleRequest(request(PATHS[0], { headers }), environment(), neverFetch);
    assert.equal(response.headers.get("WWW-Authenticate"), "Bearer");
    await expectError(response, 401);
  }
});

test("a fresh request forwards only bearer auth and Accept, never cookies, URLs or host overrides", async () => {
  const query = "?q=%E9%AA%8C%E8%AF%81&status=running&limit=25&cursor=abc_DEF-1";
  const response = await handleRequest(request(PATHS[0] + query, { headers: {
    Authorization: `Bearer ${TOKEN}`, Cookie: "session=must-not-forward", Referer: "https://evil.example.com/",
    "X-Forwarded-Host": "evil.example.com", "X-Max-Api-Origin": "https://evil.example.com", "X-HTTP-Method-Override": "POST",
  } }), environment(), async (url, init) => {
    assert.equal(url, API + PATHS[0] + query);
    assert.deepEqual(init.headers, { Authorization: `Bearer ${TOKEN}`, Accept: "application/json" });
    assert.equal(init.redirect, "manual");
    assert.equal(init.cache, "no-store");
    assert.ok(init.signal instanceof AbortSignal);
    assert.equal(init.signal.aborted, false);
    assert.equal(init.body, undefined);
    return json({ items: [] }, { headers: {
      "Content-Type": "application/json", "Set-Cookie": "never=save", "Cache-Control": "public, max-age=300",
      "Access-Control-Allow-Origin": "*", "Location": "https://evil.example.com", "X-Upstream-Internal": "private",
    } });
  });
  assert.deepEqual(await response.json(), { items: [] });
  for (const key of ["Set-Cookie", "Location", "Access-Control-Allow-Origin", "X-Upstream-Internal", "ETag"]) {
    assert.equal(response.headers.get(key), null);
  }
  assert.match(response.headers.get("Cache-Control"), /no-store/);
  assert.equal(response.headers.get("Vary"), "Authorization");
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) assert.equal(response.headers.get(key), value);
});

test("only backend query parameters and documented bounds are accepted", async () => {
  for (const query of [
    "?url=https://evil.example.com", "?MAX_API_ORIGIN=https://evil.example.com", "?token=secret",
    "?limit=1&limit=2", "?limit=0", "?limit=-1", "?limit=101", "?limit=1.5", "?limit=+1", "?status=unknown",
    `?q=${"x".repeat(257)}`, `?cursor=${"x".repeat(1025)}`, `?q=${"%61".repeat(3000)}`,
  ]) await expectError(await handleRequest(request(PATHS[0] + query), environment(), neverFetch), 400);
  for (const path of [PATHS[1] + "?limit=1", PATHS[2] + "?q=test", PATHS[3] + "?limit=201", PATHS[3] + "?q=test"]) {
    await expectError(await handleRequest(request(path), environment(), neverFetch), 400);
  }
  assert.equal(allowedQuery("tasks", new URL(CONSOLE + PATHS[0] + "?q=&limit=100&status=waiting_approval")), true);
  assert.equal(allowedQuery("events/query", new URL(CONSOLE + PATHS[3] + "?limit=200&cursor=opaque")), true);
});

test("upstream redirects are not followed or exposed even when they point to the same origin", async () => {
  for (const status of [301, 302, 303, 307, 308]) {
    let calls = 0;
    const response = await handleRequest(request(), environment(), async (_url, init) => {
      calls++;
      assert.equal(init.redirect, "manual");
      return new Response("internal upstream details", { status, headers: { Location: API + "/login", "Set-Cookie": "secret=1" } });
    });
    const body = await expectError(response, 502);
    assert.equal(response.headers.get("Location"), null);
    assert.ok(!body.detail.includes("upstream details"));
    assert.equal(calls, 1);
  }
});

test("API status codes and auth challenges survive but upstream error details do not", async () => {
  for (const status of [400, 401, 403, 404, 409, 422, 429, 500, 502, 503]) {
    const challenge = 'Bearer realm="max", error="invalid_token"';
    const response = await handleRequest(request(), environment(), async () => new Response(`internal ${API} ${TOKEN}`, {
      status, headers: { "Content-Type": "text/html", "WWW-Authenticate": challenge, "Set-Cookie": "secret=1", "Retry-After": "30" },
    }));
    await expectError(response, status);
    assert.equal(response.headers.get("WWW-Authenticate"), [401, 403].includes(status) ? challenge : null);
    assert.equal(response.headers.get("Retry-After"), status === 429 ? "30" : null);
  }
});

test("network errors and non-JSON successes expose no upstream information", async () => {
  await expectError(await handleRequest(request(), environment(), async () => { throw new Error(`${API} ${TOKEN}`); }), 502);
  for (const contentType of ["text/html", "application/jsonp", "application/json-secret", ""]) {
    await expectError(await handleRequest(request(), environment(), async () => new Response("private", { headers: { "Content-Type": contentType } })), 502);
  }
});

test("root redirects safely to console, and non-API fallback uses only static assets", async () => {
  for (const method of ["GET", "HEAD"]) {
    const response = await handleRequest(request("/?next=https://evil.example.com", { method }), environment(), neverFetch);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get("Location"), "/console/");
    assert.equal(await response.text(), "");
  }
  await expectError(await handleRequest(request("/", { method: "POST" }), environment(), neverFetch), 405);
  let seen;
  const response = await handleRequest(request("/console/"), environment({ ASSETS: { fetch(r) { seen = r.url; return new Response("static"); } } }), neverFetch);
  assert.equal(await response.text(), "static");
  assert.equal(seen, CONSOLE + "/console/");
});

test("Cloudflare execution context is not confused with the injected unit-test fetcher", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json());
  const response = await worker.fetch(request(), environment(), { waitUntil() {}, passThroughOnException() {} });
  assert.equal(response.status, 200);
});

test("deployment config is asset-first, fail-closed, and contains no API origin/token binding", async () => {
  const config = JSON.parse(await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"));
  assert.equal(config.name, "max-agent-console");
  assert.equal(config.keep_vars, true);
  assert.equal(config.observability.enabled, false);
  assert.equal(config.assets.directory, "./public");
  assert.equal(config.assets.not_found_handling, "none");
  assert.deepEqual(config.assets.run_worker_first, ["/", "/v1", "/v1/*"]);
  assert.equal(config.vars, undefined);
  assert.equal(config.MAX_API_ORIGIN, undefined);
  const source = await readFile(new URL("../src/worker.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(source, /console\.(log|error|warn)|localStorage|sessionStorage|\.put\(/);
});
