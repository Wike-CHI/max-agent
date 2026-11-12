export const statuses: Record<string, string> = {
  pending: "待调度",
  ready: "已就绪",
  leased: "已领取",
  running: "执行中",
  verifying: "验证中",
  succeeded: "已成功",
  failed: "已失败",
  blocked: "已阻塞",
  waiting_approval: "待审批",
  cancelled: "已取消",
  retry_wait: "等待重试",
};
export interface Task {
  dependencies?: string[];
  next_retry_at?: string | null;
  approval_decision?: string | null;
  approval_scope?: string[];
  approval_expires_at?: string | null;
  id: string;
  goal: string;
  status: string;
  worker_kind: string;
  updated_at: string;
  created_at: string;
  attempts: number;
  max_attempts: number;
  acceptance_criteria: string[];
  last_error: string | null;
  result_summary: string | null;
  failure_class: string | null;
  tags: string[];
  issue_ref: string | null;
}
export interface TaskEvent {
  id: number;
  event_type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
}
export interface EventPage {
  items: TaskEvent[];
  next_cursor: string | null;
  has_more: boolean;
}
export interface Cost {
  amount: number | null;
  currency: string | null;
  marker: string;
  source: string;
}
export interface Trace {
  attempts: {
    attempt_no: number;
    status: string;
    worker_kind: string;
    model: string | null;
    provider: string | null;
    runtime_ms: number | null;
    cost: Cost;
    failure_class: string | null;
  }[];
  chain: {
    kind: string;
    ref: string | null;
    marker: string;
    detail: unknown;
  }[];
  budget: unknown;
  exports: unknown;
}
export interface Status {
  task: Task;
  status: string;
  verification: unknown;
}
export function dateTime(value: string | null | undefined): string {
  if (!value) return "未记录";
  const d = new Date(
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(value)
      ? `${value}Z`
      : value,
  );
  return Number.isNaN(d.valueOf())
    ? "未记录"
    : new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(d);
}
export function safeUrl(value: unknown): string | null {
  if (typeof value !== "string" || !/^https?:\/\//i.test(value)) return null;
  try {
    const u = new URL(value);
    return ["http:", "https:"].includes(u.protocol) &&
      !u.username &&
      !u.password
      ? u.href
      : null;
  } catch {
    return null;
  }
}
export function costText(cost: Cost | undefined): string {
  if (!cost || cost.amount == null || !Number.isFinite(cost.amount))
    return "未知 · 未报告";
  const marker =
    cost.marker === "measured"
      ? "实测"
      : cost.marker === "estimate"
        ? "估算"
        : "来源未确认";
  return `${cost.amount.toLocaleString("zh-CN", { maximumFractionDigits: 6 })} ${cost.currency || "单位未报告"} · ${marker}`;
}
export function cliProof(events: TaskEvent[]): string {
  const reports = events
    .filter((e) => e.event_type === "worker_output")
    .map(
      (e) =>
        (e.payload.outputs as Record<string, unknown> | undefined)
          ?.real_cli_invoked,
    );
  if (reports.includes(true)) return "事件报告：已调用真实 CLI";
  if (reports.includes(false)) return "事件报告：未调用真实 CLI";
  return "当前事件页无真实 CLI 调用证据";
}
export function mergeEvents(
  older: TaskEvent[],
  current: TaskEvent[],
): TaskEvent[] {
  return Array.from(
    new Map([...older, ...current].map((e) => [e.id, e])).values(),
  ).sort((a, b) => a.id - b.id);
}
export class ApiError extends Error {
  constructor(public status: number) {
    super(
      status === 401
        ? "访问凭证失效，请重新连接。"
        : status === 403
          ? "当前凭证无权访问此任务或资源。"
          : status === 404
            ? "未找到该任务，可能已被移除。"
            : status === 503
              ? "任务后端尚未配置或暂不可用，请联系部署管理员。"
              : status === 502
                ? "暂时无法连接任务后端，请稍后重试。"
                : `请求失败（HTTP ${status}），请稍后重试。`,
    );
  }
}
export async function get<T>(
  path: string,
  token: string,
  signal: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method: "GET",
    headers: { Authorization: `Bearer ${token}` },
    signal,
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
  });
  if (!response.ok) throw new ApiError(response.status);
  return response.json() as Promise<T>;
}
export function errorText(error: unknown): string {
  return error instanceof ApiError
    ? error.message
    : "无法连接服务，请检查网络和任务 API 是否可用，然后重试。";
}

export function taskQuery(
  search: string,
  status: string,
  cursor: string | null,
): string {
  const params = new URLSearchParams({ q: search, limit: "25" });
  if (status) params.set("status", status);
  if (cursor) params.set("cursor", cursor);
  return `/v1/tasks/query?${params}`;
}
