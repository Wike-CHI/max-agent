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
  /** 列表接口若带上该字段就如实透传；缺失时才按 false 处理。 */
  awaiting_dispatch?: boolean;
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
  constructor(
    public status: number,
    public detail?: string,
  ) {
    super(
      status === 0
        ? "操作未能完成，请检查网络或任务 API 是否可用后重试。"
        : status === 401
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

// ---------------------------------------------------------------------------
// 写路径 API 封装：令牌只在内存中，且只出现在 Authorization 头里。
// ---------------------------------------------------------------------------

export type Scope =
  | "read"
  | "create"
  | "dispatch"
  | "approve"
  | "cancel"
  | "execute_inline"
  | "ops";

export type ActionName =
  | "read"
  | "create"
  | "dispatch"
  | "approve"
  | "reject"
  | "cancel";

export interface WhoAmI {
  token_id: string;
  principal: string;
  scopes: Scope[];
}

export interface TaskRead {
  id: string;
  goal: string;
  status: string;
  version: number;
  awaiting_dispatch: boolean;
  content_hash: string;
  dependencies: string[];
  tags: string[];
  created_by: string;
}

export interface TaskCreateBody {
  goal: string;
  acceptance_criteria?: string[];
  worker_kind?: string;
  worker_payload?: Record<string, unknown>;
  tags?: string[];
  dependencies?: string[];
  has_side_effects?: boolean;
  issue_ref?: string;
  priority?: number;
  timeout_seconds?: number;
  max_attempts?: number;
  max_cost_units?: number;
  allow_auto_retry_side_effects?: boolean;
  require_dispatch?: boolean;
}

// ---------------------------------------------------------------------------
// 建图请求体：严格对齐后端 TaskGraphCreate。
//
// 后端契约（POST /v1/tasks/graph）：
//   { "nodes": [ { "key": str, "goal": str, "depends_on": [str], ... } ],
//     "require_dispatch": true }
//
// 注意与单任务 TaskCreate 的区别：
//   - 节点列表字段是 nodes，不是 tasks；
//   - 依赖字段是 depends_on，不是 dependencies（dependencies 属于单任务创建）；
//   - 图根对象上没有 goal，goal 只属于每个节点。
// ---------------------------------------------------------------------------

export interface GraphNodeCreateBody {
  /** 图内节点键；兄弟节点的 depends_on 可以引用它。 */
  key: string;
  goal: string;
  /** 兄弟节点 key 或已有任务 id。 */
  depends_on?: string[];
  /** 节点级覆盖；通常由图级 require_dispatch 统一决定。 */
  require_dispatch?: boolean;
  acceptance_criteria?: string[];
  worker_kind?: string;
  tags?: string[];
  priority?: number;
  timeout_seconds?: number;
  max_attempts?: number;
}

export interface TaskGraphCreateBody {
  nodes: GraphNodeCreateBody[];
  /** 图级开关：对本图所有新节点生效，默认 true（创建 ≠ 执行）。 */
  require_dispatch?: boolean;
}

export interface GraphDraft {
  key: string;
  goal: string;
  dependsOn?: string[];
}

/**
 * 把前端草稿转换成后端 TaskGraphCreate 的请求体。
 *
 * - 节点列表字段是 nodes（不发送 tasks）；
 * - 每个节点提交 { key, goal, depends_on }，缺依赖时为 []；
 * - 图级 require_dispatch 缺省为 true，避免建图后自动进入执行流程。
 */
export function buildGraphCreateBody(input: {
  drafts: GraphDraft[];
  requireDispatch?: boolean;
}): TaskGraphCreateBody {
  const drafts = Array.isArray(input?.drafts) ? input.drafts : [];
  const nodes: GraphNodeCreateBody[] = drafts.map((draft) => ({
    key: typeof draft?.key === "string" ? draft.key : "",
    goal: typeof draft?.goal === "string" ? draft.goal : "",
    depends_on: Array.isArray(draft?.dependsOn) ? [...draft.dependsOn] : [],
  }));
  return {
    nodes,
    // 缺省就是 true：建图不自动派发。
    require_dispatch: input?.requireDispatch ?? true,
  };
}

export interface VersionedWrite {
  expected_version: number;
  content_hash: string;
}

export type FetchLike = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

export interface ApiClientOptions {
  baseUrl?: string;
  fetchImpl?: FetchLike;
}

const KNOWN_SCOPES: readonly string[] = [
  "read",
  "create",
  "dispatch",
  "approve",
  "cancel",
  "execute_inline",
  "ops",
];

function defaultBaseUrl(): string {
  const origin =
    typeof window !== "undefined" ? window.location?.origin : undefined;
  return origin || "http://127.0.0.1:8765";
}

function normalizeScopes(value: unknown): Scope[] {
  if (!Array.isArray(value)) return [];
  const out: Scope[] = [];
  for (const item of value) {
    if (typeof item !== "string") continue;
    const scope = item.trim().toLowerCase();
    if (!KNOWN_SCOPES.includes(scope)) continue;
    out.push(scope as Scope);
  }
  return out;
}

async function responseDetail(response: Response): Promise<string | undefined> {
  try {
    if (typeof response.text !== "function") return undefined;
    const raw = await response.text();
    if (!raw) return undefined;
    const parsed = JSON.parse(raw) as { detail?: unknown };
    const detail = parsed?.detail;
    if (typeof detail !== "string") return undefined;
    return detail.length > 500 ? `${detail.slice(0, 500)}…` : detail;
  } catch {
    return undefined;
  }
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;

  constructor(options: ApiClientOptions = {}) {
    const base = (options.baseUrl ?? defaultBaseUrl()).trim();
    this.baseUrl = base.replace(/\/+$/, "");
    this.fetchImpl =
      options.fetchImpl ??
      ((input: string, init?: RequestInit) => fetch(input, init));
  }

  private url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    token: string,
    body?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
    };
    const init: RequestInit = {
      method,
      headers,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const response = await this.fetchImpl(this.url(path), init);
    if (!response.ok) {
      throw new ApiError(response.status, await responseDetail(response));
    }
    if (response.status === 204) return undefined as unknown as T;
    return response.json() as Promise<T>;
  }

  async whoami(token: string): Promise<WhoAmI> {
    const raw = await this.request<Record<string, unknown>>(
      "GET",
      "/v1/auth/whoami",
      token,
    );
    return {
      token_id: typeof raw?.token_id === "string" ? raw.token_id : "",
      principal: typeof raw?.principal === "string" ? raw.principal : "",
      scopes: normalizeScopes(raw?.scopes),
    };
  }

  async listTasks(token: string): Promise<TaskRead[]> {
    const raw = await this.request<TaskRead[] | { items?: TaskRead[] }>(
      "GET",
      "/v1/tasks",
      token,
    );
    if (Array.isArray(raw)) return raw;
    return Array.isArray(raw?.items) ? raw.items : [];
  }

  async getTask(token: string, taskId: string): Promise<TaskRead> {
    return await this.request<TaskRead>(
      "GET",
      `/v1/tasks/${encodeURIComponent(taskId)}`,
      token,
    );
  }

  async createTask(
    token: string,
    body: TaskCreateBody,
  ): Promise<TaskRead> {
    return await this.request<TaskRead>("POST", "/v1/tasks", token, body);
  }

  async createGraph(
    token: string,
    body: TaskGraphCreateBody,
  ): Promise<TaskRead[]> {
    const raw = await this.request<TaskRead[] | { items?: TaskRead[] }>(
      "POST",
      "/v1/tasks/graph",
      token,
      body,
    );
    if (Array.isArray(raw)) return raw;
    return Array.isArray(raw?.items) ? raw.items : [];
  }

  async dispatchTask(
    token: string,
    taskId: string,
    body: VersionedWrite,
  ): Promise<TaskRead> {
    return await this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/dispatch`,
      token,
      body,
    );
  }

  async approveTask(
    token: string,
    taskId: string,
    body: VersionedWrite & { scope: string[] },
  ): Promise<TaskRead> {
    return await this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/approve`,
      token,
      body,
    );
  }

  async rejectTask(
    token: string,
    taskId: string,
    body: VersionedWrite,
  ): Promise<TaskRead> {
    return await this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/reject`,
      token,
      body,
    );
  }

  async cancelTask(
    token: string,
    taskId: string,
    body: VersionedWrite,
  ): Promise<TaskRead> {
    return await this.request<TaskRead>(
      "POST",
      `/v1/tasks/${encodeURIComponent(taskId)}/cancel`,
      token,
      body,
    );
  }
}

export interface TokenSlot {
  slotId: string;
  tokenId: string | null;
  principal: string | null;
  scopes: Scope[];
  // 明文令牌只放在这个内存字段里：不会落盘，也不会进入 URL、JSON 或日志。
  // whoami 之前 tokenId 保持 null，避免把明文 secret 写进 tokenId。
  secret?: string | null;
}

export function createSlot(slotId: string, token: string): TokenSlot {
  return {
    slotId,
    tokenId: null,
    principal: null,
    scopes: [],
    secret: token ? token : null,
  };
}

export function applyWhoAmI(slot: TokenSlot, who: WhoAmI): TokenSlot {
  return {
    slotId: slot.slotId,
    tokenId: who.token_id,
    principal: who.principal,
    scopes: normalizeScopes(who.scopes),
    secret: slot?.secret ?? null,
  };
}

export function slotSecret(slot: TokenSlot): string | null {
  return slot?.secret ?? null;
}

export function requiredScope(action: ActionName): Scope {
  switch (action) {
    case "read":
      return "read";
    case "create":
      return "create";
    case "dispatch":
      return "dispatch";
    case "approve":
    case "reject":
      return "approve";
    case "cancel":
      return "cancel";
  }
}

export function selectSlotForAction(
  slots: TokenSlot[],
  action: ActionName,
): TokenSlot | null {
  const scope = requiredScope(action);
  for (const slot of slots) {
    if (!slot) continue;
    if (Array.isArray(slot.scopes) && slot.scopes.includes(scope)) return slot;
  }
  return null;
}

export function canPerform(slots: TokenSlot[], action: ActionName): boolean {
  return selectSlotForAction(slots, action) !== null;
}

export async function loadWhoAmI(
  client: ApiClient,
  slot: TokenSlot,
): Promise<TokenSlot> {
  const secret = slotSecret(slot) ?? slot?.tokenId ?? null;
  if (!secret) {
    throw new ApiError(401, "该卡位没有可用的访问令牌，请先重新连接。");
  }
  const who = await client.whoami(secret);
  return applyWhoAmI(slot, who);
}

export async function loadFreshVersion(
  client: ApiClient,
  token: string,
  taskId: string,
): Promise<VersionedWrite> {
  const task = await client.getTask(token, taskId);
  return {
    expected_version: task.version,
    content_hash: task.content_hash,
  };
}

// ---------------------------------------------------------------------------
// 危险操作确认框、动作可用性与取消归一化。
// ---------------------------------------------------------------------------

// 敏感标签必须与后端 src/max_agent/api/scopes.py 保持一致：
// 命中这些标签的任务需要审批范围显式覆盖才允许审批。
export const SENSITIVE_TAGS: readonly string[] = [
  "delete",
  "deploy",
  "payment",
  "publish",
  "permission",
  "production",
  "external_write",
];

// 写操作 scope：持有一项的卡片就不能再被标成「只读」。
export const WRITE_SCOPES: readonly Scope[] = [
  "create",
  "dispatch",
  "approve",
  "cancel",
  "execute_inline",
];

export interface ConfirmSummary {
  goalExcerpt: string;
  hashPrefix: string;
  targetStatus: string;
  lines: string[];
}

export interface ActionAvailability {
  allowed: boolean;
  reason: string | null;
}

const TARGET_STATUS: Record<string, string> = {
  create: "pending",
  dispatch: "ready",
  approve: "approved",
  reject: "rejected",
  cancel: "cancelled",
};

export function goalExcerpt(goal: string, maxLength = 60): string {
  const text = typeof goal === "string" ? goal.trim() : "";
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(1, maxLength - 1))}…`;
}

export function hashPrefix(
  hash: string | null | undefined,
  length = 12,
): string {
  if (typeof hash !== "string") return "(无)";
  const trimmed = hash.trim();
  if (!trimmed) return "(无)";
  return trimmed.slice(0, Math.max(1, length));
}

export function targetStatusFor(action: ActionName, status?: string): string {
  const target = TARGET_STATUS[action] ?? "updated";
  const current = typeof status === "string" ? status.trim() : "";
  if (!current) return target;
  return `${current} → ${target}`;
}

export function buildConfirmSummary(
  input: { goal: string; content_hash?: string | null; status?: string },
  action: ActionName,
): ConfirmSummary {
  const excerpt = goalExcerpt(input?.goal ?? "");
  const prefix = hashPrefix(input?.content_hash);
  const target = targetStatusFor(action, input?.status);
  return {
    goalExcerpt: excerpt,
    hashPrefix: prefix,
    targetStatus: target,
    lines: [`目标：${excerpt}`, `内容哈希：${prefix}`, `目标状态：${target}`],
  };
}

export function missingSensitiveTags(
  task: TaskRead,
  covered: string[],
): string[] {
  const tags = Array.isArray(task?.tags) ? task.tags : [];
  const granted = new Set(
    (Array.isArray(covered) ? covered : []).map((tag) =>
      String(tag).trim().toLowerCase(),
    ),
  );
  const missing: string[] = [];
  for (const tag of tags) {
    const value = String(tag).trim().toLowerCase();
    if (!SENSITIVE_TAGS.includes(value)) continue;
    if (granted.has(value)) continue;
    if (!missing.includes(value)) missing.push(value);
  }
  return missing;
}

export function canApprove(
  task: TaskRead,
  slots: TokenSlot[],
  covered: string[],
): ActionAvailability {
  if (!selectSlotForAction(slots, "approve")) {
    return {
      allowed: false,
      reason: "没有持有 approve scope 的令牌，无法审批。",
    };
  }
  const missing = missingSensitiveTags(task, covered);
  if (missing.length) {
    return {
      allowed: false,
      reason: `敏感标签尚未被审批范围覆盖：${missing.join("、")}。`,
    };
  }
  return { allowed: true, reason: null };
}

export function canDispatch(
  task: TaskRead,
  slots: TokenSlot[],
): ActionAvailability {
  if (!task?.awaiting_dispatch) {
    return {
      allowed: false,
      reason: "该任务当前不处于待派发状态（awaiting_dispatch=false）。",
    };
  }
  if (!selectSlotForAction(slots, "dispatch")) {
    return {
      allowed: false,
      reason: "没有持有 dispatch scope 的令牌，无法派发。",
    };
  }
  return { allowed: true, reason: null };
}

export function canCancel(
  task: TaskRead,
  slots: TokenSlot[],
): ActionAvailability {
  if (task?.status === "cancelled") {
    return {
      allowed: false,
      reason: "任务已处于 cancelled，无需再次取消。",
    };
  }
  if (!selectSlotForAction(slots, "cancel")) {
    return {
      allowed: false,
      reason: "没有持有 cancel scope 的令牌，无法取消。",
    };
  }
  return { allowed: true, reason: null };
}

export type CancelOutcome =
  | { kind: "cancelled"; task: TaskRead }
  | { kind: "already-cancelled"; refresh: true }
  | { kind: "error"; error: ApiError };

const CANCELLED_HINTS = ["cancelled", "canceled", "已取消", "已作废"];

export function cancelledOutcome(task: TaskRead): CancelOutcome {
  return { kind: "cancelled", task };
}

export function resolveCancelOutcome(error: unknown): CancelOutcome {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      const detail = (error.detail ?? "").toLowerCase();
      const alreadyCancelled = CANCELLED_HINTS.some((hint) =>
        detail.includes(hint.toLowerCase()),
      );
      if (alreadyCancelled) return { kind: "already-cancelled", refresh: true };
    }
    return { kind: "error", error };
  }
  const message =
    error instanceof Error ? error.message : String(error ?? "未知错误");
  return { kind: "error", error: new ApiError(0, message) };
}
