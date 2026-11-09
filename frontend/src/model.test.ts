import { describe, it, expect, vi, afterEach } from "vitest";
import {
  safeUrl,
  costText,
  dateTime,
  cliProof,
  mergeEvents,
  get,
  ApiError,
  taskQuery,
  type TaskEvent,
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
