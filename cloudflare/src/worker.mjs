/**
 * The Worker only hosts the console and forwards its four read-only API routes.
 * MAX_API_ORIGIN is a server-side binding, never a browser-provided URL or token.
 */
export const SECURITY_HEADERS = Object.freeze({
  "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Strict-Transport-Security": "max-age=31536000",
});

const TASK_ID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
const TASK_ROUTE = new RegExp(`^/v1/tasks/${TASK_ID}/(status|trace|events/query)$`);
const STATUSES = new Set([
  "pending", "ready", "leased", "running", "verifying", "succeeded", "failed",
  "blocked", "waiting_approval", "cancelled", "retry_wait",
]);
const MAX_QUERY_LENGTH = 8192;

function reply(status, detail, extraHeaders = {}) {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: {
      ...SECURITY_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store, max-age=0",
      "Pragma": "no-cache",
      "Vary": "Authorization",
      ...extraHeaders,
    },
  });
}

/** Public DNS HTTPS origins only. No paths, credentials, ports, IPs or local names. */
export function approvedOrigin(value, consoleOrigin) {
  if (typeof value !== "string" || value !== value.trim() || !value) return null;
  // Parse only a literal origin, rather than allowing URL's permissive repairs.
  if (!/^https:\/\/[a-zA-Z0-9.-]+(?::443)?\/?$/.test(value)) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  const host = url.hostname;
  const labels = host.split(".");
  if (host.length > 253 || labels.length < 2 || labels.some(
    (label) => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label),
  )) return null;
  // Reject every IP literal, including URL-normalized numeric/octal/hex IPv4.
  if (/^[0-9.]+$/.test(host) || !/[a-z]/.test(labels.at(-1))) return null;
  if (/(^|\.)(localhost|local|internal|intranet|lan|home|home\.arpa|test|invalid|example)$/.test(host)) return null;
  if (url.origin === consoleOrigin) return null;
  return url.origin;
}

export function allowedRoute(pathname) {
  if (pathname === "/v1/tasks/query") return "tasks";
  const match = TASK_ROUTE.exec(pathname);
  return match?.[1] ?? null;
}

/** Keep proxy inputs identical to the query surface used by the Python API. */
export function allowedQuery(route, url) {
  if (url.search.length > MAX_QUERY_LENGTH) return false;
  const allowed = route === "tasks"
    ? new Set(["q", "status", "limit", "cursor"])
    : route === "events/query" ? new Set(["limit", "cursor"]) : new Set();
  const seen = new Set();
  for (const [key, value] of url.searchParams) {
    if (!allowed.has(key) || seen.has(key)) return false;
    seen.add(key);
    if (key === "q" && [...value].length > 256) return false;
    if (key === "cursor" && [...value].length > 1024) return false;
    if (key === "status" && !STATUSES.has(value)) return false;
    if (key === "limit" && (!/^[1-9][0-9]{0,2}$/.test(value) || Number(value) > (route === "tasks" ? 100 : 200))) return false;
  }
  return true;
}

async function proxy(request, env, url, fetcher) {
  if (request.method !== "GET") {
    return reply(405, "only GET is supported", { Allow: "GET" });
  }
  const route = allowedRoute(url.pathname);
  if (!route) return reply(404, "API route not available");
  if (!allowedQuery(route, url)) return reply(400, "invalid query parameters");

  const origin = approvedOrigin(env.MAX_API_ORIGIN, url.origin);
  if (!origin) return reply(503, "backend is not configured");

  const authorization = request.headers.get("Authorization") ?? "";
  if (authorization.length > 8192 || !/^Bearer [A-Za-z0-9._~+\/-]+=*$/i.test(authorization)) {
    return reply(401, "valid bearer token required", { "WWW-Authenticate": "Bearer" });
  }

  // Construct a fresh request: cookies, forwarded hosts, referers and client
  // headers must never influence the destination or accompany the bearer token.
  const target = new URL(url.pathname + url.search, origin);
  try {
    const upstream = await fetcher(target.href, {
      method: "GET",
      headers: { Authorization: authorization, Accept: "application/json" },
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    });
    if (upstream.status >= 300 && upstream.status < 400) {
      await upstream.body?.cancel();
      return reply(502, "backend returned an unsupported response");
    }
    if (!upstream.ok) {
      // Keep API auth/validation/not-found/rate-limit statuses, but never expose
      // upstream HTML, exception details, cookies, URLs or error bodies.
      const status = upstream.status >= 400 && upstream.status <= 599 ? upstream.status : 502;
      const headers = {};
      if (status === 401 || status === 403) {
        headers["WWW-Authenticate"] = upstream.headers.get("WWW-Authenticate") ?? "Bearer";
      }
      const retryAfter = upstream.headers.get("Retry-After");
      if (status === 429 && retryAfter && /^\d{1,6}$/.test(retryAfter)) {
        headers["Retry-After"] = retryAfter;
      }
      await upstream.body?.cancel();
      return reply(status, status === 401 ? "valid bearer token required"
        : status === 403 ? "task access denied"
          : status === 404 ? "task not found"
            : status === 422 ? "invalid query parameters" : "backend request failed", headers);
    }
    if ((upstream.headers.get("Content-Type") ?? "").split(";", 1)[0].trim().toLowerCase() !== "application/json") {
      await upstream.body?.cancel();
      return reply(502, "backend returned an unsupported response");
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        ...SECURITY_HEADERS,
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "Vary": "Authorization",
      },
    });
  } catch {
    // Deliberately do not log the exception, request, URL, headers or token.
    return reply(502, "backend is unavailable");
  }
}

export async function handleRequest(request, env, fetcher = fetch) {
  const url = new URL(request.url);
  if (url.pathname === "/v1" || url.pathname.startsWith("/v1/")) {
    return proxy(request, env, url, fetcher);
  }
  if (url.pathname === "/") {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return reply(405, "only GET and HEAD are supported", { Allow: "GET, HEAD" });
    }
    return new Response(null, {
      status: 302,
      headers: { ...SECURITY_HEADERS, Location: "/console/", "Cache-Control": "no-store" },
    });
  }
  // With asset-first routing, this branch handles only missing/static fallbacks.
  // No SPA fallback exists, and /v1 never reaches the asset binding.
  return env.ASSETS.fetch(request);
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};
