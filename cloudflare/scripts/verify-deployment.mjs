import assert from "node:assert/strict";
import { appendFile, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { DEPLOY_REF, REPOSITORY } from "./provenance.mjs";

// Consume Wrangler's machine output. Never guess the account's workers.dev name.
export function deploymentRecord(output) {
  const records = output.trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  const deployments = records.filter((entry) => entry.type === "deploy");
  assert.equal(deployments.length, 1, "Expected exactly one Wrangler deployment record.");
  const record = deployments[0];
  assert.equal(record.version, 1);
  assert.equal(record.worker_name, "max-agent-console");
  assert.equal(record.worker_name_overridden, false);
  assert.match(record.version_id, /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i);
  assert.equal(record.targets?.length, 1, "Only the single workers.dev target is permitted.");
  assert.match(record.targets[0], /^https:\/\/max-agent-console\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.workers\.dev\/?$/);
  return { version: record.version_id, origin: new URL(record.targets[0]).origin };
}

export async function verifyLive(record, sha, fetcher = fetch) {
  assert.match(sha, /^[0-9a-f]{40}$/);
  const get = (path) => fetcher(new URL(path, record.origin), {
    redirect: "manual", cache: "no-store", signal: AbortSignal.timeout(15000),
  });
  const response = await get(`/console/version.json?commit=${sha}`);
  assert.equal(response.status, 200, "Live provenance must be available without a redirect.");
  assert.match(response.headers.get("Cache-Control") ?? "", /no-store/);
  const manifest = await response.json();
  assert.deepEqual(manifest, {
    schema_version: 1, repository: REPOSITORY, git_sha: sha, git_ref: DEPLOY_REF, git_dirty: false,
  }, "Live assets must match the deployed, clean Git commit.");

  const root = await get("/");
  assert.equal(root.status, 302);
  assert.equal(root.headers.get("Location"), "/console/");
  const page = await get("/console/");
  assert.equal(page.status, 200);
  assert.match(page.headers.get("Content-Security-Policy") ?? "", /connect-src 'self'/);
  assert.equal(page.headers.get("X-Content-Type-Options"), "nosniff");
  const html = await page.text();
  const js = html.match(/src="(\/console\/assets\/[^"/?]+\.js)"/)?.[1];
  const css = html.match(/href="(\/console\/assets\/[^"/?]+\.css)"/)?.[1];
  assert.ok(js && css, "Expected built same-origin JS and CSS assets.");
  for (const path of [js, css]) {
    const asset = await get(path);
    assert.equal(asset.status, 200);
    assert.equal(asset.headers.get("X-Content-Type-Options"), "nosniff");
    await asset.body?.cancel();
  }
  // No API token is sent, and no task data is requested with authentication.
  const api = await get("/v1/tasks/query");
  assert.ok([401, 503].includes(api.status), "Unauthenticated API must fail closed.");
  assert.match(api.headers.get("Content-Type") ?? "", /application\/json/);
  assert.match(api.headers.get("Cache-Control") ?? "", /no-store/);
  const failure = await api.json();
  assert.equal(failure.detail, api.status === 503 ? "backend is not configured" : "valid bearer token required");
  return { ...record, sha, backend: api.status === 503 ? "not configured" : "configured; authenticated task reads not verified" };
}

async function main() {
  const output = await readFile(process.env.WRANGLER_DEPLOYMENT_OUTPUT, "utf8");
  const record = deploymentRecord(output);
  // Record the already-published version even if subsequent live checks fail.
  const summary = `\nCloudflare console deployment\n\n- Commit: ${process.env.GITHUB_SHA}\n- Worker version: ${record.version}\n- URL: ${record.origin}/console/\n`;
  if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY, summary);
  let result;
  for (let attempt = 1; attempt <= 5; attempt++) {
    try { result = await verifyLive(record, process.env.GITHUB_SHA); break; }
    catch (error) {
      if (attempt === 5) throw error;
      // Only retry public GET verification for propagation, never deployment.
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  }
  console.log(`Verified ${result.sha} at ${result.origin}/console/; Worker version ${result.version}; backend ${result.backend}.`);
  if (process.env.GITHUB_OUTPUT) await appendFile(process.env.GITHUB_OUTPUT, `url=${result.origin}/console/\nversion=${result.version}\n`);
  if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY, `- Live commit, static assets and fail-closed API: verified\n- Backend: ${result.backend}\n`);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
