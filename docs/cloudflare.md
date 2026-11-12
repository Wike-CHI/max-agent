# Cloudflare Workers Free deployment

## What is deployed

`cloudflare/` packages the React console and a small read-only API proxy as a
separate Worker named **max-agent-console**. It does not deploy the Python API,
Task DB, local stdio MCP server, or subprocess worker daemon. Do not overwrite an
unrelated existing Worker.

The deployment can serve the connection screen without a backend. Until an
authenticated HTTPS MAX API is configured and verified, task requests return
503. A deployed webpage is not evidence of a working task service.

Cloudflare supports Python/FastAPI, but its Python runtime is not a normal Linux
server: the temporary filesystem and subprocess limitations prevent moving this
application's existing durable SQLite file and CLI daemon there unchanged. A
future database/runtime migration is a separate project.

## Build locally

Use Node.js 24 and the checked-in lock files:

```sh
npm --prefix frontend ci
npm --prefix cloudflare ci
npm --prefix frontend test
npm --prefix cloudflare test
npm --prefix cloudflare run build
npm --prefix cloudflare run test:runtime
npm --prefix cloudflare run check
```

The build compiles the existing frontend, stages only its generated assets under
`cloudflare/public/console/`, and adds static response security headers. The
generated directory, Wrangler state, and local environment files are ignored by
Git. `check` is a dry run, not a deployment. `test:runtime` exercises the actual
workerd asset routing locally; it does not contact a real MAX backend.

## GitHub Actions deployment

The prepared deployment route is `.github/workflows/cloudflare-console.yml`,
not Cloudflare Workers Builds. **This revision cannot deploy:** its only trigger
is `workflow_dispatch`, and the deployment job has a literal `false` condition.
Publishing it does not trigger this workflow. Even a manual run cannot deploy,
regardless of repository variables or existing credentials. The normal PR CI
workflow still runs and checks the code without deployment credentials.

The eventual deployment scope remains `feat/53-cloudflare-console` in
`Wike-CHI/max-agent`. Repository, branch and enable-variable guards are retained
behind the hard stop. PR events, forks, tags and `master` must never deploy. Keep
the existing draft PR and repository visibility unchanged; this workflow does
not merge, publish a release or update the Python backend.

Every deployment first calls the complete `CI` workflow for the same commit:
Python 3.12/3.13 tests, frontend tests/typecheck/build, Worker tests/workerd/dry
run, package verification, lock audit, staging smoke and restore drill. Actions
are pinned to full commit SHAs and have only `contents: read`. The deployment
checkout is pinned to the event SHA and does not persist Git credentials.

### One-time setup, before enabling deployment

1. Confirm the selected Cloudflare account remains on **Workers Free**. Do not
   enable paid products or overages. Confirm `max-agent-console` is the intended
   dedicated Worker, not an unrelated existing Worker. A token scoped to one
   Worker may require creating that Worker through the authorized dashboard
   flow first.
2. In GitHub repository Settings → Environments, create
   **cloudflare-max-console**. Restrict deployment branches to the exact branch
   `feat/53-cloudflare-console` (do not allow tags or all branches). Configure a
   required reviewer so the first and later deployments are reviewed before
   credentials are released. Do not claim these protections are active until
   the repository settings have been checked.
3. Through the approved secure flow, create a Cloudflare **account-owned API
   token** granting **Editor for max-agent-console only**. Save it as the
   environment secret **CLOUDFLARE_API_TOKEN**. Do not put it in a repository
   secret, code, issue, command argument, build log, chat or frontend variable.
   Single-Worker support for static-asset upload must be tested on the first
   deployment. On 403, stop and report the denied operation; do not enlarge the
   token's scope automatically.
4. Add **CLOUDFLARE_ACCOUNT_ID** as a variable in that same GitHub environment.
   It is read only by the deployment step and is not written into assets or Git.
5. Verify the current repository Actions variable **CLOUDFLARE_DEPLOY_ENABLED**
   and keep it unset or `false` during setup. Its value is not assumed known in
   this revision: the literal-false job gate prevents deployment even if the
   variable is already `true`. This switch must be repository-scoped because job
   conditions are evaluated before environment variables are available.
6. Only after the protections, token, account and Free plan have been verified
   and activation is explicitly authorized, prepare a separate activation commit:
   remove only the leading `false &&` from the deploy condition, add the `push`
   trigger restricted to `feat/53-cloudflare-console`, and replace the
   pre-activation test with tests for that exact enabled scope. Preserve all
   remaining guards and the full CI dependency. Set the repository switch to
   `true` only as part of that verified activation. Missing credentials still
   fail closed. Publishing the activation commit may initiate deployment if the
   switch is already enabled; do not treat that push as a harmless code update.

Only the Wrangler deploy step receives the token and account ID. Dependency
installation, frontend builds, tests and live verification do not receive them.
The workflow uses the checked-in Wrangler lock version and does not install a
Cloudflare GitHub App or require broader account/zone access. There is no
fallback credential, token-scope expansion, paid-plan upgrade or automatic
redeployment after a failure.

After the separate activation commit and setup are complete, an authorized push
to the selected branch runs all CI checks,
waits for environment review, rebuilds and deploys the **console and proxy
only**. Because this workflow initially exists only on the feature branch,
GitHub may not expose `workflow_dispatch` until it also exists on the default
branch. Do not merge just to obtain that button. Before activation, there is no
push deployment run to retry and manual runs cannot override the hard stop.
After activation, use an authorized branch push or review/re-run its existing
push run when appropriate. A run that failed after
upload may already have changed the Worker: inspect its recorded version before
retrying. Keep Cloudflare Workers Builds disconnected for this Worker to avoid
competing deployers.

### Commit and deployment evidence

The build reads the actual checked-out Git commit and publishes only five
provenance fields at `/console/version.json`: schema version, public repository,
full Git SHA, ref and dirty status. It checks `GITHUB_SHA` against `git HEAD` and
refuses deployment when the working tree or build is dirty/mismatched. No
account identifier, environment dump, bearer token or deployment token is in
that file. Local builds are labeled dirty when applicable and are not evidence
of a deployment.

Wrangler tags the Worker version with the short SHA and records the full SHA
and Actions run ID in its version message. The post-deploy check reads Wrangler's
machine output for the **actual version UUID and workers.dev URL**, then checks
live provenance, the root redirect, static HTML/JS/CSS and the unauthenticated
API boundary. It never guesses the account subdomain or sends an API bearer
token. Read-only propagation checks retry briefly; the deploy command does not
retry automatically. The Actions job summary records commit, Worker version,
verified URL and backend status. A successful static deployment still does not
prove authenticated task queries work.

## Backend connection and authentication

Configure `MAX_API_ORIGIN` as a runtime text binding on this new Worker only. It
must be the exact approved public DNS HTTPS origin of a reachable MAX API, for
example `https://api.example.com`, with no path, query, credentials or custom
port. This example is not a configured backend. `localhost`, IP literals, local
hostnames and the console's own origin are rejected. The binding must be changed
only by a trusted deployment administrator; it determines where credentials go.

No backend URL or bearer token is built into the frontend. Users enter their
existing API bearer token in the console; it stays in page memory and is sent
only in the Authorization header. The Worker forwards it to the fixed API origin
over HTTPS and never saves it. The API remains responsible for validating the
token and enforcing task ownership. The proxy permits only these GET routes:

- `/v1/tasks/query`
- `/v1/tasks/{uuid}/status`
- `/v1/tasks/{uuid}/trace`
- `/v1/tasks/{uuid}/events/query`

It rejects writes, unlisted routes, unsupported query fields and redirects.
It forwards neither cookies nor arbitrary browser headers, and never caches API
responses. Missing configuration fails closed. CORS is not enabled. Static
assets are public, but task data still requires valid backend authentication.

The existing bearer token can still write directly to the original Python API;
this proxy does not turn it into a read-only credential. Do not expose the API
without its existing auth, or put a token in a Vite variable, URL, source file,
build output, build command, or log. Only use an already approved backend or an
authorized tunnel; a cloud Worker cannot reach the user's localhost directly.

`keep_vars` preserves runtime text bindings across CLI deployments. No account
ID, API origin or credential is committed. Observability/request logging is off
in the checked-in Worker configuration. Review any account-level logging
separately before sending private task data.

## Free tier and verification

At the documentation check on 2026-11-09, Workers Free includes 100,000 dynamic
requests/day with 10 ms CPU per invocation; static asset requests are free and
unlimited. Standard GitHub-hosted runners are free for public repositories; this workflow
uses `ubuntu-latest`, not paid larger runners. Upstream wait time does not count
as Worker CPU. These are plan limits, not an availability or
cost guarantee; stay on Free and do not enable paid overages or paid products.
Asset-first routing avoids running the proxy for ordinary static files.

After deployment verify:

1. `/` redirects to `/console/`; HTML, JS and CSS load with security headers.
2. No private task data or credential appears in public assets.
3. With no backend configured, GET `/v1/tasks/query` returns JSON 503, never SPA
   HTML or fabricated sample tasks.
4. With an approved backend configured, unauthenticated reads return 401;
   authenticated reads show the expected owner and real Task DB.
5. POST/DELETE and unlisted API routes are blocked. Auth redirects do not leak a
   token to another service.
6. A subsequent authorized Git commit triggers a successful build, and the
   deployed SHA matches that commit. Do not claim automatic updates are active
   from a configuration screen alone.

Set repository variable `CLOUDFLARE_DEPLOY_ENABLED` to `false` to stop future
automatic deployments. With the required authorization, cancel any already
queued/running deployment and roll back to a known-good Worker version when
appropriate. Changing the switch does not undo an in-flight upload or remove
the live Worker. Database rollback is separate; this deployment makes no
schema change. Do not change unrelated Workers or their triggers.

## Official references

- [GitHub Actions for Workers](https://developers.cloudflare.com/workers/ci-cd/external-cicd/github-actions/)
- [GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Worker limits](https://developers.cloudflare.com/workers/platform/limits/)
- [Static asset limits and billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)
- [Static asset routing](https://developers.cloudflare.com/workers/static-assets/binding/)
- [Python FastAPI support](https://developers.cloudflare.com/workers/languages/python/packages/fastapi/)
- [Python standard-library limitations](https://developers.cloudflare.com/workers/languages/python/stdlib/)

