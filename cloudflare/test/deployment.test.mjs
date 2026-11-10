import test from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { assertDeployable, DEPLOY_REF, gitProvenance, REPOSITORY } from "../scripts/provenance.mjs";
import { deploymentRecord, verifyLive } from "../scripts/verify-deployment.mjs";

const sha = "a".repeat(40); // Test-only commit, never a deployment label.
const manifest = { schema_version: 1, repository: REPOSITORY, git_sha: sha, git_ref: DEPLOY_REF, git_dirty: false };
const env = { GITHUB_ACTIONS: "true", GITHUB_EVENT_NAME: "push", GITHUB_SHA: sha, GITHUB_REF: DEPLOY_REF, GITHUB_REPOSITORY: REPOSITORY };
const record = { type: "deploy", version: 1, worker_name: "max-agent-console", worker_name_overridden: false,
  version_id: "11111111-2222-4333-8444-555555555555", targets: ["https://max-agent-console.test-account.workers.dev"] };

test("build provenance reads real Git HEAD and detects modified or untracked source", async () => {
  const root = await mkdtemp(join(tmpdir(), "max-provenance-"));
  const git = (...args) => execFileSync("git", args, { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  try {
    git("init", "--initial-branch", "feat/53-cloudflare-console");
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "test fixture");
    const actual = gitProvenance(root, {});
    assert.equal(actual.git_sha, git("rev-parse", "HEAD"));
    assert.equal(actual.git_ref, DEPLOY_REF);
    assert.equal(actual.git_dirty, false);
    assert.equal(Object.keys(actual).length, 5);
    assert.throws(() => gitProvenance(root, { GITHUB_SHA: sha }), /does not match/);
    assert.throws(() => gitProvenance(root, { GITHUB_REPOSITORY: "other/repo" }), /Unexpected/);
    await writeFile(join(root, "new-source.txt"), "uncommitted");
    assert.equal(gitProvenance(root, {}).git_dirty, true);
    git("add", "new-source.txt");
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "track fixture");
    assert.equal(gitProvenance(root, {}).git_dirty, false);
    await writeFile(join(root, "new-source.txt"), "modified");
    assert.equal(gitProvenance(root, {}).git_dirty, true);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("deploy provenance rejects wrong branch, fork, PR, dirty build and mismatched commit", () => {
  assert.doesNotThrow(() => assertDeployable(manifest, manifest, env));
  assert.doesNotThrow(() => assertDeployable(manifest, manifest, { ...env, GITHUB_EVENT_NAME: "workflow_dispatch" }));
  for (const change of [
    { GITHUB_REF: "refs/heads/master" }, { GITHUB_REPOSITORY: "other/repo" },
    { GITHUB_EVENT_NAME: "pull_request" }, { GITHUB_ACTIONS: "false" }, { GITHUB_SHA: "b".repeat(40) },
  ]) assert.throws(() => assertDeployable(manifest, manifest, { ...env, ...change }));
  for (const change of [{ git_dirty: true }, { git_sha: "b".repeat(40) }, { git_ref: "refs/heads/master" }]) {
    assert.throws(() => assertDeployable({ ...manifest, ...change }, manifest, env));
    assert.throws(() => assertDeployable(manifest, { ...manifest, ...change }, env));
  }
});

test("verification requires Wrangler's actual Worker version and exact workers.dev target", () => {
  assert.deepEqual(deploymentRecord(JSON.stringify(record)), { version: record.version_id, origin: record.targets[0] });
  for (const change of [
    { version_id: null }, { worker_name: "other-worker" }, { worker_name_overridden: true },
    { targets: [] }, { targets: [...record.targets, "https://extra.example.com"] },
    { targets: ["https://max-agent-console.test-account.workers.dev.evil.example.com"] },
    { targets: ["https://max-agent-console.test-account.workers.dev/?token=secret"] },
  ]) assert.throws(() => deploymentRecord(JSON.stringify({ ...record, ...change })));
  assert.throws(() => deploymentRecord(`${JSON.stringify(record)}\n${JSON.stringify(record)}`));
});

function fakeFetch(version = manifest, apiStatus = 503) {
  return async (url, init) => {
    assert.equal(url.origin, record.targets[0]);
    assert.equal(init.redirect, "manual");
    assert.equal(init.headers, undefined);
    const headers = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "connect-src 'self'" };
    if (url.pathname === "/") return new Response(null, { status: 302, headers: { Location: "/console/" } });
    if (url.pathname === "/console/version.json") return Response.json(version, { headers });
    if (url.pathname === "/console/") return new Response('<script src="/console/assets/main-hash.js"></script><link href="/console/assets/main-hash.css">', { headers });
    if (url.pathname.startsWith("/console/assets/")) return new Response("asset", { headers });
    assert.equal(url.pathname, "/v1/tasks/query");
    return Response.json({ detail: apiStatus === 503 ? "backend is not configured" : "valid bearer token required" }, { status: apiStatus, headers });
  };
}

test("live verification accepts only matching clean provenance and fail-closed API", async () => {
  const deployment = deploymentRecord(JSON.stringify(record));
  assert.equal((await verifyLive(deployment, sha, fakeFetch())).backend, "not configured");
  assert.match((await verifyLive(deployment, sha, fakeFetch(manifest, 401))).backend, /not verified/);
  await assert.rejects(verifyLive(deployment, sha, fakeFetch({ ...manifest, git_sha: "b".repeat(40) })));
  await assert.rejects(verifyLive(deployment, sha, fakeFetch({ ...manifest, git_dirty: true })));
  await assert.rejects(verifyLive(deployment, sha, fakeFetch(manifest, 200)));
  await assert.rejects(verifyLive(deployment, sha, async () => new Response(null, { status: 302 })));
});
