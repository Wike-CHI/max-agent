import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = fileURLToPath(new URL("../../", import.meta.url));
export const REPOSITORY = "Wike-CHI/max-agent";
export const DEPLOY_REF = "refs/heads/feat/53-cloudflare-console";

export function gitProvenance(root = ROOT, env = process.env) {
  const git = (...args) => execFileSync("git", args, { cwd: root, encoding: "utf8" }).trim();
  const sha = git("rev-parse", "HEAD");
  if (!/^[0-9a-f]{40}$/.test(sha)) throw new Error("Expected a full Git commit SHA.");
  if (env.GITHUB_SHA && env.GITHUB_SHA !== sha) throw new Error("Checkout does not match GITHUB_SHA.");
  if (env.GITHUB_REPOSITORY && env.GITHUB_REPOSITORY !== REPOSITORY) throw new Error("Unexpected source repository.");
  return {
    schema_version: 1,
    repository: REPOSITORY,
    git_sha: sha,
    git_ref: env.GITHUB_REF || git("rev-parse", "--symbolic-full-name", "HEAD"),
    git_dirty: git("status", "--porcelain", "--untracked-files=normal") !== "",
  };
}

export function assertDeployable(manifest, current, env = process.env) {
  if (env.GITHUB_ACTIONS !== "true" || !["push", "workflow_dispatch"].includes(env.GITHUB_EVENT_NAME)) {
    throw new Error("Deploy requires an authorized GitHub push or manual workflow run.");
  }
  if (env.GITHUB_REPOSITORY !== REPOSITORY || env.GITHUB_REF !== DEPLOY_REF || !env.GITHUB_SHA) {
    throw new Error("Deploy is restricted to the authorized repository and branch.");
  }
  if (current.git_dirty || manifest.git_dirty || manifest.git_ref !== DEPLOY_REF
      || manifest.git_sha !== env.GITHUB_SHA || JSON.stringify(manifest) !== JSON.stringify(current)) {
    throw new Error("Build provenance must match the clean, current workflow commit.");
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv[2] !== "--check-deploy") throw new Error("Use --check-deploy.");
  const manifest = JSON.parse(await readFile(resolve(ROOT, "cloudflare/public/console/version.json"), "utf8"));
  assertDeployable(manifest, gitProvenance());
  console.log(`Verified clean deployment source ${manifest.git_sha}.`);
}
