import { describe, it, expect, vi, afterEach } from "vitest";
import {
  safeUrl,
  costText,
  dateTime,
  cliProof,
  mergeEvents,
  get,
  ApiClient,
  ApiError,
  applyWhoAmI,
  canPerform,
  createSlot,
  loadFreshVersion,
  loadWhoAmI,
  requiredScope,
  selectSlotForAction,
  taskQuery,
  buildConfirmSummary,
  canApprove,
  canCancel,
  canDispatch,
  goalExcerpt,
  hashPrefix,
  missingSensitiveTags,
  resolveCancelOutcome,
  targetStatusFor,
  buildGraphCreateBody,
  type FetchLike,
  type TaskEvent,
  type TaskRead,
  type TokenSlot,
  type WhoAmI,
} from "./model";
afterEach(() => vi.unstubAllGlobals());
const event = (id: number, invoked?: unknown): TaskEvent => ({
  id,
  event_type: "worker_output",
  message: "",
  created_at: "2026-01-01T00:00:00Z",
  payload: { outputs: { real_cli_invoked: invoked } },
});
describe("evidence and display semantics", () => {
  it("interprets naive backend timestamps as UTC", () => {
    expect(dateTime("2026-01-01T00:00:00")).toBe(
      dateTime("2026-01-01T00:00:00Z"),
    );
  });
  it("allows only explicit HTTP(S) links without credentials", () => {
    for (const value of [
      "javascript:alert(1)",
      "data:text/html,test",
      "file:///tmp/proof",
      "/tmp/proof",
      "//example.com",
      "https://user:password@example.com",
      "https:example.com",
    ])
      expect(safeUrl(value)).toBeNull();
    expect(safeUrl("https://example.com/proof")).toBe(
      "https://example.com/proof",
    );
  });
  it("does not turn unknown costs into zero or estimates into measured", () => {
    expect(costText(undefined)).toContain("未知");
    expect(
      costText({
        amount: null,
        currency: "USD",
        marker: "measured",
        source: "api_spend",
      }),
    ).toContain("未知");
    expect(
      costText({
        amount: 0,
        currency: "USD",
        marker: "estimate",
        source: "api_spend",
      }),
    ).toBe("0 USD · 估算");
  });
  it("requires exact boolean worker output proof", () => {
    expect(cliProof([event(1, "true")])).toContain("无真实");
    expect(cliProof([event(1, false)])).toContain("未调用");
    expect(cliProof([event(1, true)])).toContain("已调用");
    expect(cliProof([{ ...event(1, true), event_type: "created" }])).toContain(
      "无真实",
    );
  });
  it("deduplicates older pages in chronological ID order", () =>
    expect(
      mergeEvents([event(1), event(2)], [event(2), event(3)]).map((e) => e.id),
    ).toEqual([1, 2, 3]));
  it("handles invalid dates honestly", () => {
    expect(dateTime("invalid")).toBe("未记录");
    expect(dateTime(null)).toBe("未记录");
  });
});
describe("read-only transport", () => {
  it("distinguishes unconfigured or unavailable backend from bad credentials", () => {
    expect(new ApiError(503).message).toContain("后端尚未配置");
    expect(new ApiError(502).message).toContain("无法连接任务后端");
    expect(new ApiError(401).message).toContain("访问凭证失效");
  });
  it("uses GET only and sends credentials exclusively in bearer header", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ items: [] }) });
    vi.stubGlobal("fetch", fetcher);
    const c = new AbortController();
    await get("/v1/tasks/query", "test-only-token", c.signal);
    expect(fetcher).toHaveBeenCalledWith("/v1/tasks/query", {
      method: "GET",
      headers: { Authorization: "Bearer test-only-token" },
      signal: c.signal,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
  });
  it("does not surface arbitrary backend error bodies", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 401 }),
    );
    await expect(
      get("/v1/tasks/query", "test-only-token", new AbortController().signal),
    ).rejects.toBeInstanceOf(ApiError);
  });
  it("propagates cancellation without retrying or mutating", async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValue(new DOMException("Aborted", "AbortError"));
    vi.stubGlobal("fetch", fetcher);
    const c = new AbortController();
    c.abort();
    await expect(
      get("/v1/tasks/query", "test-only-token", c.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

describe("task query filters", () => {
  it("omits empty enum filter and cursor", () => {
    const query = new URL(taskQuery("", "", null), "https://example.com");
    expect(query.searchParams.has("status")).toBe(false);
    expect(query.searchParams.has("cursor")).toBe(false);
    expect(query.searchParams.get("limit")).toBe("25");
  });
  it("encodes search and preserves cursor and selected state", () => {
    const query = new URL(
      taskQuery("测试 & work", "running", "opaque+/="),
      "https://example.com",
    );
    expect(query.searchParams.get("q")).toBe("测试 & work");
    expect(query.searchParams.get("status")).toBe("running");
    expect(query.searchParams.get("cursor")).toBe("opaque+/=");
  });
});

// ---- Issue 65 Task 8: write-path client, whoami, in-memory tokens ----

interface Recorded {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

function jsonResponse(status: number, payload: unknown): Response {
  const body = JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
    json: async () => JSON.parse(body),
  } as unknown as Response;
}

function makeFetch(responses: Array<{ status: number; json: unknown }>) {
  const calls: Recorded[] = [];
  const queue = [...responses];
  const impl = vi.fn(async (input: string, init?: RequestInit) => {
    calls.push({
      url: input,
      method: (init?.method ?? "GET").toUpperCase(),
      headers: { ...((init?.headers ?? {}) as Record<string, string>) },
      body: typeof init?.body === "string" ? init.body : null,
    });
    const next = queue.shift() ?? { status: 200, json: {} };
    return jsonResponse(next.status, next.json);
  });
  return { impl: impl as unknown as FetchLike, calls };
}

function installStorageSpies() {
  const stores: Record<string, { setItem: ReturnType<typeof vi.fn> }> = {};
  for (const name of ["localStorage", "sessionStorage"]) {
    const store = {
      setItem: vi.fn(),
      getItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(),
      length: 0,
    };
    stores[name] = store;
    Object.defineProperty(globalThis, name, {
      value: store,
      configurable: true,
      writable: true,
    });
  }
  const indexedDb = { open: vi.fn(), deleteDatabase: vi.fn() };
  Object.defineProperty(globalThis, "indexedDB", {
    value: indexedDb,
    configurable: true,
    writable: true,
  });
  const cookieSetter = vi.fn();
  const documentStub: Record<string, unknown> = {};
  Object.defineProperty(documentStub, "cookie", { get: () => "", set: cookieSetter });
  Object.defineProperty(globalThis, "document", {
    value: documentStub,
    configurable: true,
    writable: true,
  });
  return { stores, indexedDb, cookieSetter };
}

describe("令牌只存在于内存", () => {
  it("whoami 之后不写任何持久化存储", async () => {
    const spies = installStorageSpies();
    const { impl } = makeFetch([
      { status: 200, json: { token_id: "read-card", principal: "tester", scopes: ["read"] } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    const slot = await loadWhoAmI(client, createSlot("浏览卡", "read-secret"));

    expect(slot.tokenId).toBe("read-card");
    expect(slot.scopes).toEqual(["read"]);
    expect(spies.stores.localStorage.setItem).not.toHaveBeenCalled();
    expect(spies.stores.sessionStorage.setItem).not.toHaveBeenCalled();
    expect(spies.indexedDb.open).not.toHaveBeenCalled();
    expect(spies.cookieSetter).not.toHaveBeenCalled();
  });

  it("令牌不出现在 URL query / hash 里", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: { token_id: "read-card", principal: "tester", scopes: ["read"] } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await loadWhoAmI(client, createSlot("浏览卡", "read-secret"));

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/auth/whoami");
    expect(calls[0].url).not.toContain("read-secret");
    expect(calls[0].headers.Authorization).toBe("Bearer read-secret");
  });
});

describe("写请求只打同源 /v1/*", () => {
  it("create 走同源 POST /v1/tasks", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: { id: "t1", goal: "g", status: "pending", version: 1 } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await client.createTask("create-secret", { goal: "g", require_dispatch: true });

    expect(calls[0].method).toBe("POST");
    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks");
    expect(new URL(calls[0].url).pathname.startsWith("/v1/")).toBe(true);
    expect(calls[0].url).not.toContain("create-secret");
  });
});

describe("403 冒泡", () => {
  it("缺 scope 时抛出 ApiError(403)", async () => {
    const { impl } = makeFetch([{ status: 403, json: { detail: "missing scope: create" } }]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await expect(
      client.createTask("read-secret", { goal: "g", require_dispatch: true }),
    ).rejects.toBeInstanceOf(ApiError);

    try {
      await client.createTask("read-secret", { goal: "g", require_dispatch: true });
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(403);
      expect((err as ApiError).detail).toContain("missing scope");
    }
  });
});

describe("多令牌按动作选卡", () => {
  const whoami = (token_id: string, scopes: string[]): WhoAmI => ({
    token_id,
    principal: "tester",
    scopes: scopes as WhoAmI["scopes"],
  });

  it("requiredScope 映射动作到 API scope", () => {
    expect(requiredScope("create")).toBe("create");
    expect(requiredScope("dispatch")).toBe("dispatch");
    expect(requiredScope("approve")).toBe("approve");
    expect(requiredScope("reject")).toBe("approve");
    expect(requiredScope("cancel")).toBe("cancel");
    expect(requiredScope("read")).toBe("read");
  });

  it("按操作类型挑出拥有对应 scope 的令牌", () => {
    const slots: TokenSlot[] = [
      applyWhoAmI(createSlot("浏览卡", "s1"), whoami("read-card", ["read"])),
      applyWhoAmI(createSlot("审批卡", "s2"), whoami("approve-card", ["approve"])),
      applyWhoAmI(createSlot("派发卡", "s3"), whoami("dispatch-card", ["dispatch"])),
    ];

    expect(selectSlotForAction(slots, "read")?.slotId).toBe("浏览卡");
    expect(selectSlotForAction(slots, "approve")?.slotId).toBe("审批卡");
    expect(selectSlotForAction(slots, "reject")?.slotId).toBe("审批卡");
    expect(selectSlotForAction(slots, "dispatch")?.slotId).toBe("派发卡");
    expect(selectSlotForAction(slots, "create")).toBeNull();
    expect(canPerform(slots, "create")).toBe(false);
    expect(canPerform(slots, "approve")).toBe(true);
  });

  it("whoami 的 scopes 不含任何秘密字段", () => {
    const { impl } = makeFetch([
      { status: 200, json: { token_id: "read-card", principal: "tester", scopes: ["read"] } },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });
    return client.whoami("read-secret").then((who) => {
      expect(Object.keys(who).sort()).toEqual(["principal", "scopes", "token_id"]);
    });
  });
});

describe("确认前刷新版本与哈希", () => {
  it("loadFreshVersion 重新拉取当前任务详情", async () => {
    const { impl, calls } = makeFetch([
      {
        status: 200,
        json: {
          id: "t1",
          goal: "g",
          status: "waiting_approval",
          version: 7,
          awaiting_dispatch: false,
          content_hash: "f".repeat(64),
          dependencies: [],
          tags: ["payment"],
          created_by: "tester",
        },
      },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    const fresh = await loadFreshVersion(client, "read-secret", "t1");

    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks/t1");
    expect(fresh.expected_version).toBe(7);
    expect(fresh.content_hash).toBe("f".repeat(64));
  });
});

// ---- Issue 65 Task 9: confirm summary, availability, cancel 409 ----

function taskFixture(overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    id: "t1",
    goal: "把生产环境的计费脚本改成幂等实现",
    status: "waiting_approval",
    version: 3,
    awaiting_dispatch: false,
    content_hash: "a".repeat(64),
    dependencies: [],
    tags: ["payment"],
    created_by: "tester",
    ...overrides,
  };
}

describe("危险操作确认框", () => {
  it("包含 goal 摘要、哈希短前缀与目标状态", () => {
    const summary = buildConfirmSummary(taskFixture(), "approve");

    expect(summary.goalExcerpt.length).toBeLessThanOrEqual(60);
    expect(summary.hashPrefix).toBe("aaaaaaaaaaaa");
    expect(summary.targetStatus).toBe("waiting_approval → approved");
    expect(summary.lines).toHaveLength(3);
    expect(summary.lines.join("\n")).toContain("计费脚本");
    expect(summary.lines.join("\n")).toContain("aaaaaaaaaaaa");
    expect(summary.lines.join("\n")).toContain("approved");
  });

  it("goal 过长时截断", () => {
    const long = "x".repeat(200);
    expect(goalExcerpt(long, 60)).toHaveLength(60);
    expect(goalExcerpt(long, 60).endsWith("…")).toBe(true);
  });

  it("创建动作也走确认框", () => {
    const summary = buildConfirmSummary({ goal: "新建任务" }, "create");
    expect(summary.targetStatus).toContain("pending");
    expect(summary.hashPrefix).toBe("(无)");
  });

  it("五种危险动作都有目标状态", () => {
    expect(targetStatusFor("create")).toContain("pending");
    expect(targetStatusFor("dispatch")).toContain("ready");
    expect(targetStatusFor("approve")).toContain("approved");
    expect(targetStatusFor("reject")).toContain("rejected");
    expect(targetStatusFor("cancel")).toContain("cancelled");
  });

  it("hashPrefix 对空哈希给出占位", () => {
    expect(hashPrefix(null)).toBe("(无)");
    expect(hashPrefix("b".repeat(64))).toBe("bbbbbbbbbbbb");
  });
});

describe("动作可用性判定", () => {
  const approveSlot = applyWhoAmI(createSlot("审批卡", "s"), {
    token_id: "approve-card",
    principal: "tester",
    scopes: ["approve"],
  });
  const dispatchSlot = applyWhoAmI(createSlot("派发卡", "s2"), {
    token_id: "dispatch-card",
    principal: "tester",
    scopes: ["dispatch"],
  });
  const cancelSlot = applyWhoAmI(createSlot("取消卡", "s3"), {
    token_id: "cancel-card",
    principal: "tester",
    scopes: ["cancel"],
  });

  it("敏感 tag 未被覆盖时不允许审批并给出原因", () => {
    const result = canApprove(taskFixture(), [approveSlot], []);
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain("payment");
    expect(missingSensitiveTags(taskFixture(), ["payment"])).toEqual([]);
  });

  it("覆盖敏感 tag 后允许审批", () => {
    expect(canApprove(taskFixture(), [approveSlot], ["payment"]).allowed).toBe(true);
  });

  it("没有 approve scope 时不允许审批", () => {
    expect(canApprove(taskFixture(), [dispatchSlot], ["payment"]).allowed).toBe(false);
  });

  it("派发按钮仅在 awaiting_dispatch=true 且持有 dispatch scope 时可用", () => {
    const gated = taskFixture({ awaiting_dispatch: true, status: "pending" });
    expect(canDispatch(gated, [dispatchSlot]).allowed).toBe(true);
    expect(canDispatch(taskFixture(), [dispatchSlot]).allowed).toBe(false);
    expect(canDispatch(gated, [approveSlot]).allowed).toBe(false);
  });

  it("取消需要 cancel scope", () => {
    expect(canCancel(taskFixture(), [cancelSlot]).allowed).toBe(true);
    expect(canCancel(taskFixture(), [dispatchSlot]).allowed).toBe(false);
  });
});

describe("取消 409 的归一化", () => {
  it("任务已是 cancelled 时视为已完成并刷新", () => {
    const outcome = resolveCancelOutcome(
      new ApiError(409, "任务 t1 已处于终态 cancelled"),
    );
    expect(outcome.kind).toBe("already-cancelled");
    if (outcome.kind === "already-cancelled") {
      expect(outcome.refresh).toBe(true);
    }
  });

  it("其它 409 仍然是错误", () => {
    const outcome = resolveCancelOutcome(new ApiError(409, "expected_version 不匹配"));
    expect(outcome.kind).toBe("error");
  });

  it("非 ApiError 也归为错误", () => {
    const outcome = resolveCancelOutcome(new Error("boom"));
    expect(outcome.kind).toBe("error");
  });
});

describe("禁用按钮不是安全边界", () => {
  it("用只有 read 的令牌触发创建，仍然收到服务器 403", async () => {
    const { impl } = makeFetch([{ status: 403, json: { detail: "missing scope: create" } }]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await expect(
      client.createTask("read-secret", { goal: "g", require_dispatch: true }),
    ).rejects.toMatchObject({ status: 403 });
  });

  it("审批请求体带 expected_version、content_hash 与 tag 覆盖范围", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: taskFixture({ status: "ready", version: 4 }) },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await client.approveTask("approve-secret", "t1", {
      expected_version: 3,
      content_hash: "a".repeat(64),
      scope: ["payment"],
    });

    const sent = JSON.parse(calls[0].body ?? "{}");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks/t1/approve");
    expect(sent.expected_version).toBe(3);
    expect(sent.content_hash).toBe("a".repeat(64));
    expect(sent.scope).toEqual(["payment"]);
  });

  it("派发与取消请求体都带 expected_version 与 content_hash", async () => {
    const { impl, calls } = makeFetch([
      { status: 200, json: taskFixture() },
      { status: 200, json: taskFixture({ status: "cancelled" }) },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    await client.dispatchTask("dispatch-secret", "t1", {
      expected_version: 3,
      content_hash: "a".repeat(64),
    });
    await client.cancelTask("cancel-secret", "t1", {
      expected_version: 3,
      content_hash: "a".repeat(64),
    });

    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks/t1/dispatch");
    expect(calls[1].url).toBe("http://127.0.0.1:8765/v1/tasks/t1/cancel");
    expect(JSON.parse(calls[0].body ?? "{}").expected_version).toBe(3);
    expect(JSON.parse(calls[1].body ?? "{}").content_hash).toBe("a".repeat(64));
  });
});

// ---- Issue 65 Task 10 收口：建图请求体必须对齐后端 TaskGraphCreate ----

describe("建图请求体对齐后端 TaskGraphCreate", () => {
  it("图级 require_dispatch 与 nodes/depends_on 发往 POST /v1/tasks/graph", async () => {
    const { impl, calls } = makeFetch([
      {
        status: 200,
        json: [
          {
            id: "t-a",
            goal: "node a",
            status: "pending",
            version: 1,
            awaiting_dispatch: true,
            content_hash: "c".repeat(64),
            dependencies: [],
            tags: [],
            created_by: "tester",
          },
        ],
      },
    ]);
    const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetchImpl: impl });

    const body = buildGraphCreateBody({
      drafts: [
        { key: "a", goal: "node a", dependsOn: [] },
        { key: "b", goal: "node b", dependsOn: ["a"] },
      ],
      requireDispatch: true,
    });
    await client.createGraph("create-secret", body);

    expect(calls[0].method).toBe("POST");
    expect(calls[0].url).toBe("http://127.0.0.1:8765/v1/tasks/graph");
    const sent = JSON.parse(calls[0].body ?? "{}");
    expect(sent.require_dispatch).toBe(true);
    expect(sent.nodes).toEqual([
      { key: "a", goal: "node a", depends_on: [] },
      { key: "b", goal: "node b", depends_on: ["a"] },
    ]);
    expect(sent).not.toHaveProperty("tasks");
  });

  it("缺图级 require_dispatch 时 helper 仍默认 true，避免自动就绪", () => {
    const body = buildGraphCreateBody({
      drafts: [{ key: "only", goal: "solo" }],
    });
    expect(body.require_dispatch).toBe(true);
    expect(body.nodes[0]).toEqual({ key: "only", goal: "solo", depends_on: [] });
  });
});
