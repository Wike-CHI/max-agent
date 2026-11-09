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

## Git automatic deployment

Follow the repository's Issue, feature-branch, draft-PR, CI and human-review
process. Keep the repository visibility unchanged. Publishing a branch is not
permission to merge it or expand a GitHub App's access.

In the authorized Cloudflare account, create a **new** Worker with a Git
connection to `Wike-CHI/max-agent`. Give the GitHub integration access to only this
repository if a new grant is required, and obtain the applicable approval before
granting persistent access. Do not copy account tokens into Git or this document.

Use these Workers Builds settings:

| Setting | Value |
| --- | --- |
| Worker name | `max-agent-console` |
| Repository | `Wike-CHI/max-agent` |
| Root directory | `cloudflare` |
| Node version | `24` |
| Build command | See below |
| Deploy command | `npm run deploy` |
| Non-production branch builds | Disabled initially |
| Production branch | The exact reviewed/authorized branch containing this code |

```sh
npm ci && npm --prefix ../frontend ci && npm --prefix ../frontend test && npm test && npm run build && npm run test:runtime && npm run check
```

Cloudflare's automatic dependency detection is not a substitute for installing
both package directories. The explicit build command works from `cloudflare/`.
The Worker name must match `wrangler.jsonc`. Do not enable builds for arbitrary
branches or pull requests carrying unreviewed code with deployment access.

Once connected, pushes to the selected production branch rebuild and deploy the
**console and proxy only**. They do not update the separately hosted Python API.
For first deployment, record the exact source commit, build result, deployed
version and verified `workers.dev` URL. Never infer the account subdomain.

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
unlimited. Workers Builds Free includes 3,000 build minutes/month. Upstream wait
time does not count as Worker CPU. These are plan limits, not an availability or
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

Roll back to a known-good Worker version or disable the new Worker's Git trigger
when appropriate. Database rollback is separate; this deployment makes no
schema change. Do not change unrelated Workers or their triggers.

## Official references

- [Workers Builds setup](https://developers.cloudflare.com/workers/ci-cd/builds/)
- [Build configuration](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/)
- [Build limits](https://developers.cloudflare.com/workers/ci-cd/builds/limits-and-pricing/)
- [Worker limits](https://developers.cloudflare.com/workers/platform/limits/)
- [Static asset limits and billing](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/)
- [Static asset routing](https://developers.cloudflare.com/workers/static-assets/binding/)
- [Python FastAPI support](https://developers.cloudflare.com/workers/languages/python/packages/fastapi/)
- [Python standard-library limitations](https://developers.cloudflare.com/workers/languages/python/stdlib/)

