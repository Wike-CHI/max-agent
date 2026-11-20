import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDownLeft,
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronRight,
  Clock3,
  FileCheck2,
  GitBranch,
  Layers3,
  Link2,
  LockKeyhole,
  LogOut,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import {
  ApiClient,
  ApiError,
  SENSITIVE_TAGS,
  buildConfirmSummary,
  buildGraphCreateBody,
  canApprove,
  canCancel,
  canDispatch,
  cliProof,
  costText,
  createSlot,
  dateTime,
  errorText,
  get,
  goalExcerpt,
  loadFreshVersion,
  loadWhoAmI,
  mergeEvents,
  resolveCancelOutcome,
  safeUrl,
  selectSlotForAction,
  slotSecret,
  statuses,
  targetStatusFor,
  taskQuery,
  WRITE_SCOPES,
  type ActionAvailability,
  type ActionName,
  type ConfirmSummary,
  type EventPage,
  type GraphDraft,
  type Status,
  type Task,
  type TaskCreateBody,
  type TaskRead,
  type TokenSlot,
  type Trace,
} from "./model";
import {
  assertAcyclic,
  buildDag,
  dagCycles,
  type DagGraph,
  type DagNode,
} from "./dag";
import "./style.css";

const ACTION_LABELS: Record<ActionName, string> = {
  read: "读取",
  create: "创建",
  dispatch: "派发",
  approve: "审批",
  reject: "拒绝",
  cancel: "取消",
};

// 写路径错误文案：说清楚发生了什么，但不回显后端原始响应体。
function writeErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "访问凭证失效，请重新连接后重试。";
    if (error.status === 403)
      return "操作被拒绝：当前令牌缺少所需 scope（如 create / dispatch / approve / cancel）。";
    if (error.status === 409)
      return "操作被拒绝：任务状态或版本已变化，请刷新后重试。";
    if (error.status === 404) return "未找到该任务，可能已被移除。";
    return error.message;
  }
  return errorText(error);
}

function isWriteCard(slot: TokenSlot): boolean {
  return slot.scopes.some((scope) => WRITE_SCOPES.includes(scope));
}

function Badge({ status }: { status: string }) {
  return (
    <span className={`badge ${status}`}>
      <i />
      {statuses[status] || status}
    </span>
  );
}
function Json({ value }: { value: unknown }) {
  return <pre>{JSON.stringify(value ?? null, null, 2)}</pre>;
}
function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty">
      <Layers3 size={34} strokeWidth={1.3} />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

/**
 * 列表接口返回的任务缺少 version / content_hash，构图时用安全缺省补齐。
 * awaiting_dispatch 若已随列表项返回就如实透传；只有字段缺失时才按 false 处理——
 * 不知道就当作「不处于待派发状态」，不猜测。
 */
function toTaskRead(task: Task): TaskRead {
  return {
    id: task.id,
    goal: typeof task.goal === "string" ? task.goal : "",
    status:
      typeof task.status === "string" && task.status.trim()
        ? task.status
        : "unknown",
    version: 1,
    awaiting_dispatch:
      typeof task.awaiting_dispatch === "boolean"
        ? task.awaiting_dispatch
        : false,
    content_hash: "",
    dependencies: Array.isArray(task.dependencies) ? task.dependencies : [],
    tags: Array.isArray(task.tags) ? task.tags : [],
    created_by: "",
  };
}

/** 解析用户填写的依赖 ID：逗号 / 空白分隔，去重去空。 */
function parseDependencyIds(text: string): string[] {
  const out: string[] = [];
  for (const part of String(text ?? "").split(/[,\s，、]+/)) {
    const id = part.trim();
    if (!id || out.includes(id)) continue;
    out.push(id);
  }
  return out;
}

/**
 * 只读 DAG：按拓扑层级铺开节点，并列出入图的依赖边。
 * 图上不提供派发 / 审批等写操作——那些仍然只发生在任务详情页。
 */
function DagView({
  graph,
  onSelect,
}: {
  graph: DagGraph;
  onSelect: (id: string) => void;
}) {
  const buckets = new Map<number, DagNode[]>();
  for (const node of graph.nodes) {
    const level = graph.levels[node.id] ?? 0;
    const bucket = buckets.get(level);
    if (bucket) bucket.push(node);
    else buckets.set(level, [node]);
  }
  const levels = Array.from(buckets.keys()).sort((a, b) => a - b);
  return (
    <div className="dag">
      {graph.cycles.length > 0 && (
        <div className="error dag-cycle" role="alert">
          检测到循环依赖：
          {graph.cycles.map((cycle) => cycle.join(" → ")).join("；")}。
          这不是一张合法的 DAG，执行顺序无法确定；这里只如实展示，不做任何调度。
        </div>
      )}
      <div className="dag-levels">
        {levels.map((level) => (
          <div className="dag-level" key={level}>
            <span className="dag-level-tag">L{level}</span>
            <div className="dag-nodes">
              {(buckets.get(level) ?? []).map((node) => (
                <button
                  key={node.id}
                  className="dag-node"
                  onClick={() => onSelect(node.id)}
                  title={`查看 ${node.id} 的详情`}
                >
                  <Badge status={node.status} />
                  <strong>{node.goal}</strong>
                  <small>{node.id}</small>
                  {node.awaiting_dispatch && (
                    <em className="dag-flag">待派发</em>
                  )}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="dag-edges">
        <h4>依赖边（前置 → 依赖者）</h4>
        {graph.edges.length ? (
          <ul>
            {graph.edges.map((edge) => (
              <li key={`${edge.from}->${edge.to}`}>
                <code>{edge.from}</code> → <code>{edge.to}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">当前页任务之间没有可展示的依赖边。</p>
        )}
      </div>
      <p className="dag-note">
        依赖关系只来自任务已有的 dependencies 字段；指向未加载上游的依赖不生成边。
        图是只读的，派发与审批请到任务详情页处理。
      </p>
    </div>
  );
}

function ConfirmDialog({
  action,
  summary,
  busy,
  note,
  onCancel,
  onConfirm,
}: {
  action: ActionName;
  summary: ConfirmSummary;
  busy: boolean;
  note?: string;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    panel.current?.focus();
  }, []);
  return (
    <div className="confirm-layer">
      <button className="scrim" onClick={onCancel} aria-label="取消确认" />
      <section
        className="confirm"
        role="dialog"
        aria-modal="true"
        aria-label={`确认${ACTION_LABELS[action]}`}
        ref={panel}
        tabIndex={-1}
      >
        <header>
          <span className="eyebrow">CONFIRM {ACTION_LABELS[action].toUpperCase()}</span>
          <button onClick={onCancel} aria-label="关闭确认框">
            <X size={20} />
          </button>
        </header>
        <h2>确认{ACTION_LABELS[action]}操作</h2>
        <ul className="confirm-lines">
          {summary.lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <p className="confirm-note">
          {note ??
            "该操作会改变任务状态。确认后仍会重新读取任务版本与内容哈希，再向任务 API 提交写请求。"}
        </p>
        <footer>
          <button onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            className="primary confirm-go"
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "提交中…" : `确认${ACTION_LABELS[action]}`}
          </button>
        </footer>
      </section>
    </div>
  );
}

function App() {
  const client = useMemo(() => new ApiClient(), []);
  const [slots, setSlots] = useState<TokenSlot[]>([]);
  const [notice, setNotice] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const serial = useRef(0);

  // 添加令牌：先 whoami，拿到 scope 之后这张卡才会被授予能力。
  async function connect(secret: string): Promise<TokenSlot> {
    setConnecting(true);
    try {
      serial.current += 1;
      const loaded = await loadWhoAmI(
        client,
        createSlot(`访问卡 ${serial.current}`, secret),
      );
      setSlots((current) => [...current, loaded]);
      return loaded;
    } finally {
      setConnecting(false);
    }
  }

  function disconnect(message = "") {
    setSlots([]);
    setNotice(message);
    setEpoch((n) => n + 1);
  }

  return slots.length ? (
    <Console
      key={epoch}
      client={client}
      slots={slots}
      onConnect={connect}
      onRemove={(slotId) =>
        setSlots((current) => current.filter((slot) => slot.slotId !== slotId))
      }
      disconnect={disconnect}
    />
  ) : (
    <ConnectView
      connecting={connecting}
      notice={notice}
      onConnect={async (secret) => {
        setNotice("");
        try {
          await connect(secret);
        } catch (error) {
          setNotice(writeErrorText(error));
        }
      }}
    />
  );
}

function ConnectView({
  connecting,
  notice,
  onConnect,
}: {
  connecting: boolean;
  notice: string;
  onConnect: (secret: string) => Promise<void>;
}) {
  const [error, setError] = useState("");
  return (
    <div className="connect-shell">
      <div className="connect-brand">
        <span className="logo">M</span>
        <b>MAX</b>
        <span>任务优先，结果可验证。</span>
      </div>
      <main className="connect-card">
        <span className="eyebrow">MAX WORKSPACE / 按 scope 访问</span>
        <div className="connect-icon">
          <LockKeyhole size={27} />
        </div>
        <h1>连接你的任务空间</h1>
        <p>
          把执行过程放在眼前。查看任务、追踪事件，
          <br />
          核对每一次尝试的结果与证据。
        </p>
        <p className="connect-hint">
          可以添加多枚令牌卡：每一枚只携带自己被授予的 scope。
          只读卡用于浏览任务，创建 / 派发 / 审批 / 取消卡用于写操作，
          写操作都会要求二次确认。
        </p>
        {notice && (
          <div className="error" role="alert">
            {notice}
          </div>
        )}
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            const input = form.elements.namedItem("token") as HTMLInputElement;
            const value = input.value.trim();
            if (!value) return;
            setError("");
            onConnect(value)
              .then(() => {
                input.value = "";
              })
              .catch((err) => setError(writeErrorText(err)));
          }}
        >
          <label htmlFor="token">API 访问令牌</label>
          <input
            id="token"
            name="token"
            type="password"
            autoComplete="off"
            required
            placeholder="输入 Bearer token"
            spellCheck={false}
          />
          <button className="primary" type="submit" disabled={connecting}>
            {connecting ? "正在校验…" : "连接控制台"} <ArrowRight size={16} />
          </button>
        </form>
        <p className="security-note">
          <ShieldCheck size={15} />
          令牌仅保存在当前页面内存中，不会写入浏览器存储。
        </p>
      </main>
      <footer>
        MAX CONSOLE <span>按 scope 授权 · 写操作需二次确认</span>
      </footer>
    </div>
  );
}

function TokenPanel({
  slots,
  onConnect,
  onRemove,
}: {
  slots: TokenSlot[];
  onConnect: (secret: string) => Promise<TokenSlot>;
  onRemove: (slotId: string) => void;
}) {
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <section className="tasks-card token-panel">
      <div className="section-heading">
        <div>
          <h2>
            访问卡 <span>{slots.length}</span>
          </h2>
          <p>每张卡按 whoami 返回的 scope 生效 · 令牌只留在内存</p>
        </div>
      </div>
      <div className="token-cards">
        {slots.map((slot) => (
          <div className="token-card" key={slot.slotId}>
            <div className="token-card-top">
              <b>{slot.tokenId || slot.slotId}</b>
              <button
                aria-label={`移除 ${slot.tokenId || slot.slotId}`}
                onClick={() => onRemove(slot.slotId)}
              >
                <Trash2 size={14} />
              </button>
            </div>
            <span className="token-principal">
              {slot.principal || "主体未报告"}
            </span>
            <div className="token-scopes">
              {slot.scopes.length ? (
                slot.scopes.map((scope) => (
                  <span className={`scope-tag ${scope}`} key={scope}>
                    {scope}
                  </span>
                ))
              ) : (
                <span className="scope-tag none">无 scope</span>
              )}
            </div>
            <small>
              {isWriteCard(slot)
                ? "可写操作 · 需二次确认"
                : slot.scopes.includes("read")
                  ? "只读"
                  : "无读写能力"}
            </small>
          </div>
        ))}
      </div>
      <form
        className="token-add"
        onSubmit={(e) => {
          e.preventDefault();
          const value = secret.trim();
          if (!value) return;
          setBusy(true);
          setError("");
          onConnect(value)
            .then(() => setSecret(""))
            .catch((err) => setError(writeErrorText(err)))
            .finally(() => setBusy(false));
        }}
      >
        <input
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
          placeholder="再添加一枚令牌（不同 scope）…"
          aria-label="添加令牌"
        />
        <button type="submit" disabled={busy || !secret.trim()}>
          <Plus size={15} />
          {busy ? "校验中…" : "添加令牌卡"}
        </button>
      </form>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}

function Console({
  client,
  slots,
  onConnect,
  onRemove,
  disconnect,
}: {
  client: ApiClient;
  slots: TokenSlot[];
  onConnect: (secret: string) => Promise<TokenSlot>;
  onRemove: (slotId: string) => void;
  disconnect: (message?: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("");
  const [search, setSearch] = useState("");
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);
  const [data, setData] = useState<{
    items: Task[];
    total: number;
    next_cursor: string | null;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [updated, setUpdated] = useState("");
  const [selected, setSelected] = useState(() => decodeHash());
  const [createOpen, setCreateOpen] = useState(false);
  const [createGoal, setCreateGoal] = useState("");
  const [createCriteria, setCreateCriteria] = useState("");
  const [createTags, setCreateTags] = useState("");
  const [createError, setCreateError] = useState("");
  const [notice, setNotice] = useState("");
  const [graphOpen, setGraphOpen] = useState(false);
  const [graphName, setGraphName] = useState("");
  // 每个草稿带一个图内 key：后端 TaskGraphCreate 的 depends_on 引用的是
  // 兄弟节点 key 或已有任务 id，前端必须给新节点稳定且唯一的 key。
  const [graphDrafts, setGraphDrafts] = useState<
    { key: string; goal: string; dependencies: string }[]
  >([{ key: "n1", goal: "", dependencies: "" }]);
  const [graphError, setGraphError] = useState("");
  const [confirm, setConfirm] = useState<{
    action: ActionName;
    summary: ConfirmSummary;
    kind: "task" | "graph";
  } | null>(null);
  const [busy, setBusy] = useState(false);

  const readSlot = selectSlotForAction(slots, "read");
  // 明文令牌只从内存里的 slot.secret 取，不会写进 URL 或浏览器存储。
  const readToken = readSlot ? (readSlot.secret ?? null) : null;
  const createSlotForAction = selectSlotForAction(slots, "create");
  const canCreate = !!createSlotForAction;
  // 只读 DAG：直接用当前列表里的任务构图，不额外发请求。
  const graphTasks = useMemo(
    () => (data?.items ?? []).map(toTaskRead),
    [data],
  );
  const dag = useMemo(() => buildDag(graphTasks), [graphTasks]);

  useEffect(() => {
    const handler = () => setSelected(decodeHash());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  useEffect(() => {
    const timer = setTimeout(() => {
      setSearch(query.trim());
      setCursors([null]);
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    if (!readToken) {
      setLoading(false);
      setData(null);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError("");
    setData(null);
    get<{ items: Task[]; total: number; next_cursor: string | null }>(
      taskQuery(search, filter, cursors[page]),
      readToken,
      controller.signal,
    )
      .then((result) => {
        if (active) {
          setData(result);
          setUpdated(new Date().toISOString());
        }
      })
      .catch((e) => {
        if (active) {
          if (e instanceof ApiError && e.status === 401) disconnect(e.message);
          else setError(errorText(e));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [search, filter, page, cursors, refresh, readToken]);
  function choose(id: string) {
    window.location.hash = `task=${encodeURIComponent(id)}`;
    setSelected(id);
  }
  function reload() {
    setPage(0);
    setCursors([null]);
    setRefresh((n) => n + 1);
  }

  async function runCreate() {
    const slot = selectSlotForAction(slots, "create");
    const token = slot ? (slot.secret ?? null) : null;
    if (!token) {
      setCreateError("没有持有 create scope 的令牌，无法创建任务。");
      setConfirm(null);
      return;
    }
    setBusy(true);
    setCreateError("");
    try {
      const body: TaskCreateBody = {
        goal: createGoal.trim(),
        acceptance_criteria: createCriteria
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean),
        tags: createTags
          .split(/[,\s，]+/)
          .map((tag) => tag.trim())
          .filter(Boolean),
        // 默认不自动派发：创建后需要显式确认派发。
        require_dispatch: true,
      };
      const created = await client.createTask(token, body);
      setNotice(
        `任务 ${created.id} 已创建（${targetStatusFor("create")}），等待人工派发。`,
      );
      setCreateOpen(false);
      setCreateGoal("");
      setCreateCriteria("");
      setCreateTags("");
      setConfirm(null);
      reload();
    } catch (e) {
      setCreateError(writeErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  /**
   * 把表单草稿整理成后端 TaskGraphCreate 需要的形状：
   * - key：用户留空时按位置分配稳定 key（n1、n2…），并保证图内唯一；
   * - depends_on：依赖可以是兄弟节点 key，也可以是已有任务 id。
   * 图根对象不夹带 goal，节点列表字段是 nodes（不是 tasks）。
   */
  function collectGraphDrafts(): {
    drafts: GraphDraft[];
    /** key → 依赖引用（兄弟 key 或已有任务 id），用于环检测 */
    dependencies: Record<string, string[]>;
    /** 图内节点 key 集合 */
    siblingKeys: Set<string>;
    /** 当前页看不到的依赖引用（既不是兄弟 key，也不是本页任务 id） */
    unknownRefs: string[];
  } {
    const filled = graphDrafts.filter((draft) => draft.goal.trim());
    const knownTaskIds = new Set(graphTasks.map((task) => task.id));
    const keys: string[] = [];
    const used = new Set<string>();
    filled.forEach((draft, index) => {
      let key = draft.key.trim();
      if (!key) key = `n${index + 1}`;
      while (used.has(key)) key = `${key}-${index + 1}`;
      used.add(key);
      keys.push(key);
    });
    const siblingKeys = new Set(keys);
    const dependencies: Record<string, string[]> = {};
    const unknownRefs: string[] = [];
    const drafts: GraphDraft[] = filled.map((draft, index) => {
      const key = keys[index];
      const refs: string[] = [];
      for (const ref of parseDependencyIds(draft.dependencies)) {
        if (siblingKeys.has(ref)) {
          // 兄弟节点 key：图内依赖。
          refs.push(ref);
        } else if (knownTaskIds.has(ref)) {
          // 已有任务 id：跨图依赖。
          refs.push(ref);
        } else {
          // 既不是兄弟 key 也不是本页任务 id：如实上报，不静默丢弃。
          if (!unknownRefs.includes(ref)) unknownRefs.push(ref);
          refs.push(ref);
        }
      }
      dependencies[key] = refs;
      return { key, goal: draft.goal.trim(), dependsOn: refs };
    });
    return { drafts, dependencies, siblingKeys, unknownRefs };
  }

  /**
   * 图创建前的一次复核：把这次要提交的依赖并进当前依赖图，
   * 用与后端一致的规则再查一次环（任务集合口径与依赖映射口径必须一致）。
   */
  function requestGraphCreate() {
    setGraphError("");
    const { drafts, dependencies, siblingKeys, unknownRefs } =
      collectGraphDrafts();
    if (!drafts.length) {
      setGraphError("至少填写一个任务目标。");
      return;
    }
    // 草稿在复核图里用合成 id，依赖引用（兄弟 key 或已有任务 id）映射到同一口径。
    const synthetic = (key: string) => `draft:${key}`;
    const merged: TaskRead[] = [
      ...graphTasks,
      ...drafts.map((draft) => ({
        id: synthetic(draft.key),
        goal: draft.goal,
        status: "pending",
        version: 1,
        awaiting_dispatch: false,
        content_hash: "",
        dependencies: (dependencies[draft.key] ?? []).map((ref) =>
          siblingKeys.has(ref) ? synthetic(ref) : ref,
        ),
        tags: [],
        created_by: "console",
      })),
    ];
    const mergedDeps: Record<string, string[]> = {};
    for (const task of merged) mergedDeps[task.id] = task.dependencies;
    const mergedCycles = dagCycles(merged);
    const firstCycle = assertAcyclic(mergedDeps);

    const name = graphName.trim() || `任务图（${drafts.length} 个任务）`;
    const lines: string[] = [
      `图名称：${name}`,
      `任务数量：${drafts.length}`,
      "图级 require_dispatch：true（创建后不会自动派发）",
    ];
    drafts.forEach((draft, index) => {
      const deps = dependencies[draft.key] ?? [];
      lines.push(
        `${index + 1}. [${draft.key}] ${goalExcerpt(draft.goal)}` +
          (deps.length ? ` · 依赖 ${deps.join(", ")}` : " · 无依赖"),
      );
    });
    if (unknownRefs.length) {
      lines.push(
        `注意：${unknownRefs.join(", ")} 既不是本页任务 id，也不是图内节点 key。`,
      );
    }
    if (mergedCycles.length || firstCycle) {
      const cyclePaths = mergedCycles.length
        ? mergedCycles.map((cycle) => cycle.join(" → "))
        : firstCycle
          ? [firstCycle.join(" → ")]
          : [];
      lines.push(
        `警告：当前依赖图已存在循环依赖（${cyclePaths.join(
          "；",
        )}），新建任务不会修复它。`,
      );
    }
    setConfirm({
      action: "create",
      kind: "graph",
      summary: {
        goalExcerpt: `${name}（${drafts.length} 个任务）`,
        hashPrefix: "(无)",
        targetStatus: "pending",
        lines,
      },
    });
  }

  async function runCreateGraph() {
    const slot = selectSlotForAction(slots, "create");
    const token = slot ? (slot.secret ?? null) : null;
    if (!token) {
      setGraphError("没有持有 create scope 的令牌，无法创建任务图。");
      setConfirm(null);
      return;
    }
    const { drafts } = collectGraphDrafts();
    setBusy(true);
    setGraphError("");
    try {
      // 请求体严格对齐后端 TaskGraphCreate：nodes + 图级 require_dispatch。
      const body = buildGraphCreateBody({ drafts, requireDispatch: true });
      const created = await client.createGraph(token, body);
      setNotice(
        `任务图已创建：${created.length} 个任务（${created
          .map((task) => task.id)
          .join(", ")}），均为 pending，等待人工派发。`,
      );
      setGraphOpen(false);
      setGraphName("");
      setGraphDrafts([{ key: "n1", goal: "", dependencies: "" }]);
      setConfirm(null);
      reload();
    } catch (e) {
      setGraphError(writeErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app-shell">
      <aside className="sidebar" inert={!!selected}>
        <a className="brand" href="#" onClick={() => setSelected("")}>
          <span className="logo">M</span>
          <b>
            MAX<span>AGENT WORKSPACE</span>
          </b>
        </a>
        <div className="workspace">
          <span className="workspace-icon">
            <Terminal size={16} />
          </span>
          <div>
            任务工作空间<small>Task workspace</small>
          </div>
        </div>
        <div className="nav-label">工作台</div>
        <button
          className="nav-item"
          onClick={() => {
            window.location.hash = "";
            setSelected("");
          }}
        >
          <Layers3 size={18} />
          任务控制台
          <ChevronRight size={15} />
        </button>
        <div className="sidebar-bottom">
          <div className="mode">
            <ShieldCheck size={17} />
            <span>
              按 scope 授权<small>令牌只在内存 · 写操作需确认</small>
            </span>
          </div>
          <button className="disconnect" onClick={() => disconnect()}>
            <LogOut size={16} />
            断开连接
          </button>
        </div>
      </aside>
      <div className="main" inert={!!selected}>
        <header className="topbar">
          <span>
            工作台 <ChevronRight size={13} /> <b>任务控制台</b>
          </span>
          <div className="topbar-actions">
            <span className="readonly">
              <LockKeyhole size={12} /> SCOPED ACCESS
            </span>
            <button
              className="mobile-disconnect"
              aria-label="断开连接"
              onClick={() => disconnect()}
            >
              <LogOut size={15} />
            </button>
          </div>
        </header>
        <main className="content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">TASK OBSERVABILITY</div>
              <h1>
                让每个任务，都有迹可循<span>.</span>
              </h1>
              <p>从目标到验证，查看真实执行状态与完整事件记录。</p>
            </div>
            <div className="heading-actions">
              <button
                className="refresh"
                onClick={reload}
                disabled={loading}
              >
                <RefreshCw size={16} className={loading ? "spin" : ""} />
                刷新
              </button>
              <button
                className="primary create-toggle"
                onClick={() => setCreateOpen((v) => !v)}
                disabled={!canCreate}
                title={
                  canCreate
                    ? "创建任务（创建后需显式派发）"
                    : "需要一枚持有 create scope 的令牌"
                }
              >
                <Plus size={16} />
                新建任务
              </button>
              <button
                className="graph-toggle"
                onClick={() => {
                  setGraphError("");
                  setGraphOpen((v) => !v);
                }}
                disabled={!canCreate}
                title={
                  canCreate
                    ? "一次创建多个任务（依赖既有任务，创建后需显式派发）"
                    : "需要一枚持有 create scope 的令牌"
                }
              >
                <GitBranch size={16} />
                新建任务图
              </button>
            </div>
          </div>

          <TokenPanel
            slots={slots}
            onConnect={onConnect}
            onRemove={onRemove}
          />

          {notice && (
            <div className="notice" role="status">
              {notice}
            </div>
          )}

          {createOpen && (
            <section className="tasks-card create-card">
              <div className="section-heading">
                <div>
                  <h2>新建任务</h2>
                  <p>创建后状态为 pending，需显式派发才会进入执行流程</p>
                </div>
                <button
                  aria-label="关闭创建表单"
                  onClick={() => setCreateOpen(false)}
                >
                  <X size={16} />
                </button>
              </div>
              <form
                className="create-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!createGoal.trim()) {
                    setCreateError("请先填写任务目标。");
                    return;
                  }
                  setCreateError("");
                  setConfirm({
                    action: "create",
                    kind: "task",
                    summary: buildConfirmSummary(
                      { goal: createGoal.trim() },
                      "create",
                    ),
                  });
                }}
              >
                <label htmlFor="create-goal">任务目标</label>
                <textarea
                  id="create-goal"
                  value={createGoal}
                  onChange={(e) => setCreateGoal(e.target.value)}
                  placeholder="例如：把生产环境的计费脚本改成幂等实现"
                />
                <label htmlFor="create-criteria">验收标准（每行一条，可选）</label>
                <textarea
                  id="create-criteria"
                  value={createCriteria}
                  onChange={(e) => setCreateCriteria(e.target.value)}
                  placeholder={"脚本重复执行不产生重复扣款\n提供回滚说明"}
                />
                <label htmlFor="create-tags">标签（逗号分隔，可选）</label>
                <input
                  id="create-tags"
                  value={createTags}
                  onChange={(e) => setCreateTags(e.target.value)}
                  placeholder="payment, production"
                />
                <p className="create-hint">
                  创建请求固定使用 require_dispatch=true：派发 ≠ 立即执行，
                  需要本机运行 max-agent worker。
                </p>
                {createError && (
                  <div className="error" role="alert">
                    {createError}
                  </div>
                )}
                <div className="create-actions">
                  <button type="button" onClick={() => setCreateOpen(false)}>
                    取消
                  </button>
                  <button className="primary" type="submit">
                    复核并创建…
                  </button>
                </div>
              </form>
            </section>
          )}

          <section className="tasks-card">
            <div className="section-heading">
              <div>
                <h2>
                  任务列表 <span>{data?.total ?? "—"}</span>
                </h2>
                <p>当前筛选结果 · 按最近创建排序</p>
              </div>
              <div className="updated">
                <Clock3 size={13} />
                {updated ? `更新于 ${dateTime(updated)}` : "尚未同步"}
              </div>
            </div>
            <div className="filters">
              <label className="search">
                <Search size={17} />
                <input
                  aria-label="搜索任务"
                  maxLength={256}
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="搜索任务目标或 ID…"
                />
                {query && (
                  <button aria-label="清除搜索" onClick={() => setQuery("")}>
                    <X size={15} />
                  </button>
                )}
              </label>
              <select
                aria-label="筛选状态"
                value={filter}
                onChange={(e) => {
                  setFilter(e.target.value);
                  setPage(0);
                  setCursors([null]);
                }}
              >
                <option value="">所有状态</option>
                {Object.entries(statuses).map(([k, v]) => (
                  <option key={k} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            </div>
            {!readToken ? (
              <div className="error-area" role="alert">
                <h2>没有可读取的令牌</h2>
                <p>
                  任务列表需要一枚持有 read scope
                  的令牌。请在「访问卡」里添加一枚只读卡。
                </p>
              </div>
            ) : loading ? (
              <div className="loading" role="status">
                <RefreshCw className="spin" size={20} />
                正在读取任务…
              </div>
            ) : error ? (
              <div className="error-area" role="alert">
                <p>{error}</p>
                <button onClick={reload}>重试</button>
              </div>
            ) : data?.items.length ? (
              <div className="task-table">
                <div className="table-head">
                  <span>任务 / 目标</span>
                  <span>状态</span>
                  <span>执行器</span>
                  <span>最近更新</span>
                  <span />
                </div>
                {data.items.map((task) => (
                  <button
                    key={task.id}
                    className={`task-row ${task.id === selected ? "selected" : ""}`}
                    onClick={() => choose(task.id)}
                  >
                    <div className="task-goal">
                      <span className={`task-icon ${task.status}`}>
                        <FileCheck2 size={18} />
                      </span>
                      <span>
                        <strong>{task.goal}</strong>
                        <small>{task.id}</small>
                      </span>
                    </div>
                    <div>
                      <Badge status={task.status} />
                    </div>
                    <span className="worker">{task.worker_kind}</span>
                    <time dateTime={task.updated_at}>
                      {dateTime(task.updated_at)}
                    </time>
                    <ChevronRight size={16} />
                  </button>
                ))}
              </div>
            ) : (
              <Empty title={search || filter ? "没有匹配的任务" : "还没有任务"}>
                {search || filter
                  ? "试试其他关键词或状态筛选。"
                  : "任务创建后会在这里显示；创建后还需要显式派发。"}
              </Empty>
            )}
            <div className="pagination">
              <span>每页最多 25 条 · 第 {page + 1} 页</span>
              <div>
                <button
                  aria-label="上一页"
                  disabled={page === 0 || loading}
                  onClick={() => setPage((p) => p - 1)}
                >
                  <ArrowLeft size={15} />
                </button>
                <button
                  aria-label="下一页"
                  disabled={!data?.next_cursor || loading}
                  onClick={() => {
                    if (data?.next_cursor) {
                      setCursors((c) => [
                        ...c.slice(0, page + 1),
                        data.next_cursor,
                      ]);
                      setPage((p) => p + 1);
                    }
                  }}
                >
                  <ArrowRight size={15} />
                </button>
              </div>
            </div>
          </section>

          <section className="tasks-card dag-card">
            <div className="section-heading">
              <div>
                <h2>
                  任务依赖图 <span>{dag.nodes.length}</span>
                </h2>
                <p>
                  只读视图 · 仅基于当前页任务的 dependencies 字段 · 不代表真实调度顺序
                </p>
              </div>
              <div className="updated">
                <LockKeyhole size={13} />
                只读
              </div>
            </div>
            {dag.nodes.length ? (
              <DagView graph={dag} onSelect={choose} />
            ) : (
              <Empty title="还没有可构图的任务">
                当前页没有任务；任务创建后会在这里显示依赖关系。
              </Empty>
            )}
          </section>

          {graphOpen && (
            <section className="tasks-card create-card graph-card">
              <div className="section-heading">
                <div>
                  <h2>新建任务图</h2>
                  <p>
                    一次提交多个任务 · 图级 require_dispatch=true · 创建后仍需逐个显式派发
                  </p>
                </div>
                <button
                  aria-label="关闭任务图表单"
                  onClick={() => setGraphOpen(false)}
                >
                  <X size={16} />
                </button>
              </div>
              <form
                className="create-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  requestGraphCreate();
                }}
              >
                <label htmlFor="graph-name">图名称（可选，仅用于本地复核提示）</label>
                <input
                  id="graph-name"
                  value={graphName}
                  onChange={(e) => setGraphName(e.target.value)}
                  placeholder={`任务图（${graphDrafts.filter((d) => d.goal.trim()).length} 个任务）`}
                />
                <datalist id="known-task-ids">
                  {graphTasks.map((task) => (
                    <option value={task.id} key={task.id}>
                      {task.goal}
                    </option>
                  ))}
                </datalist>
                {graphDrafts.map((draft, index) => (
                  <fieldset className="graph-draft" key={index}>
                    <legend>任务 {index + 1}</legend>
                    <label htmlFor={`graph-key-${index}`}>节点 key</label>
                    <input
                      id={`graph-key-${index}`}
                      value={draft.key}
                      onChange={(e) =>
                        setGraphDrafts((current) =>
                          current.map((item, i) =>
                            i === index
                              ? { ...item, key: e.target.value }
                              : item,
                          ),
                        )
                      }
                      placeholder={`n${index + 1}`}
                    />
                    <label htmlFor={`graph-goal-${index}`}>任务目标</label>
                    <input
                      id={`graph-goal-${index}`}
                      value={draft.goal}
                      onChange={(e) =>
                        setGraphDrafts((current) =>
                          current.map((item, i) =>
                            i === index
                              ? { ...item, goal: e.target.value }
                              : item,
                          ),
                        )
                      }
                      placeholder="例如：把计费脚本改成幂等实现"
                    />
                    <label htmlFor={`graph-deps-${index}`}>
                      依赖（兄弟节点 key 或已有任务 ID，逗号分隔，可选）
                    </label>
                    <input
                      id={`graph-deps-${index}`}
                      list="known-task-ids"
                      value={draft.dependencies}
                      onChange={(e) =>
                        setGraphDrafts((current) =>
                          current.map((item, i) =>
                            i === index
                              ? { ...item, dependencies: e.target.value }
                              : item,
                          ),
                        )
                      }
                      placeholder="n1, t-123"
                    />
                  </fieldset>
                ))}
                <p className="create-hint">
                  依赖用节点 key（同一张图内）或已有任务 ID 表达；新任务的 ID
                  由后端分配。请求固定为 nodes + 图级 require_dispatch=true，
                  创建后仍需逐个显式派发。提交前会再按后端同一规则检查一次依赖图是否成环。
                </p>
                {graphError && (
                  <div className="error" role="alert">
                    {graphError}
                  </div>
                )}
                <div className="create-actions">
                  <button
                    type="button"
                    onClick={() =>
                      setGraphDrafts((current) => [
                        ...current,
                        {
                          // 新节点的 key 稳定递增，避免与已有草稿重名。
                          key: `n${current.length + 1}`,
                          goal: "",
                          dependencies: "",
                        },
                      ])
                    }
                  >
                    <Plus size={14} />
                    再加一个任务
                  </button>
                  <button type="button" onClick={() => setGraphOpen(false)}>
                    取消
                  </button>
                  <button className="primary" type="submit">
                    复核并创建…
                  </button>
                </div>
              </form>
            </section>
          )}

          <div className="bottom-note">
            <ShieldCheck size={14} /> 所有状态来自 API。执行器类型不代表真实 CLI
            已被调用。
          </div>
        </main>
      </div>
      {selected && (
        <Detail
          key={selected}
          id={selected}
          client={client}
          slots={slots}
          token={readToken}
          refresh={refresh}
          disconnect={disconnect}
          onChanged={reload}
          close={() => {
            window.location.hash = "";
            setSelected("");
          }}
        />
      )}
      {confirm && confirm.action === "create" && (
        <ConfirmDialog
          action="create"
          summary={confirm.summary}
          busy={busy}
          note={
            confirm.kind === "graph"
              ? "该操作会创建多个新任务。请求固定使用图级 require_dispatch=true：创建 ≠ 执行，创建后仍需逐个显式派发。"
              : undefined
          }
          onCancel={() => setConfirm(null)}
          onConfirm={confirm.kind === "graph" ? runCreateGraph : runCreate}
        />
      )}
    </div>
  );
}
function decodeHash() {
  try {
    const params = new URLSearchParams(window.location.hash.slice(1));
    return params.get("task") || "";
  } catch {
    return "";
  }
}
function Detail({
  id,
  client,
  slots,
  token,
  refresh,
  disconnect,
  onChanged,
  close,
}: {
  id: string;
  client: ApiClient;
  slots: TokenSlot[];
  token: string | null;
  refresh: number;
  disconnect: (message?: string) => void;
  onChanged: () => void;
  close: () => void;
}) {
  const [tab, setTab] = useState("overview");
  const [data, setData] = useState<{
    status: Status;
    trace: Trace;
    events: EventPage;
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [older, setOlder] = useState(false);
  const [olderError, setOlderError] = useState("");
  const [retry, setRetry] = useState(0);
  const generation = useRef(0);
  const olderController = useRef<AbortController | null>(null);
  const panel = useRef<HTMLElement>(null);
  // 写操作需要任务的最新版本与内容哈希。
  const [taskRead, setTaskRead] = useState<TaskRead | null>(null);
  const [covered, setCovered] = useState<string[]>([]);
  const [actionError, setActionError] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [confirm, setConfirm] = useState<{
    action: ActionName;
    summary: ConfirmSummary;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      if (e.key === "Tab" && panel.current) {
        const controls = Array.from(
          panel.current.querySelectorAll<HTMLElement>(
            'button:not(:disabled),a[href],[tabindex="0"]',
          ),
        );
        const first = controls[0],
          last = controls.at(-1);
        if (
          e.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === panel.current)
        ) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      previous?.focus();
    };
  }, []);
  useEffect(() => {
    const c = new AbortController();
    const current = ++generation.current;
    olderController.current?.abort();
    setOlder(false);
    setOlderError("");
    setLoading(true);
    setData(null);
    setError("");
    const base = `/v1/tasks/${encodeURIComponent(id)}`;
    if (!token) {
      setLoading(false);
      setError(
        "没有持有 read scope 的令牌，无法读取任务详情。请在「访问卡」中添加一枚只读卡。",
      );
      return;
    }
    Promise.all([
      get<Status>(`${base}/status`, token, c.signal),
      get<Trace>(`${base}/trace`, token, c.signal),
      get<EventPage>(`${base}/events/query?limit=50`, token, c.signal),
    ])
      .then(([status, trace, events]) => {
        if (current === generation.current) setData({ status, trace, events });
      })
      .catch((e) => {
        if (current === generation.current) {
          if (e instanceof ApiError && e.status === 401) disconnect(e.message);
          else setError(errorText(e));
        }
      })
      .finally(() => {
        if (current === generation.current) setLoading(false);
      });
    return () => {
      generation.current++;
      c.abort();
      olderController.current?.abort();
    };
  }, [id, token, refresh, retry]);
  // 写操作前重新读取版本与内容哈希，不沿用页面上可能过期的值。
  useEffect(() => {
    let active = true;
    if (!token) {
      setTaskRead(null);
      return;
    }
    client
      .getTask(token, id)
      .then((fresh) => {
        if (active) setTaskRead(fresh);
      })
      .catch(() => {
        if (active) setTaskRead(null);
      });
    return () => {
      active = false;
    };
  }, [client, id, token, refresh]);
  async function loadOlder() {
    if (!data?.events.next_cursor || older) return;
    const current = generation.current;
    const c = new AbortController();
    olderController.current = c;
    setOlder(true);
    setOlderError("");
    try {
      const result = await get<EventPage>(
        `/v1/tasks/${encodeURIComponent(id)}/events/query?limit=50&cursor=${encodeURIComponent(data.events.next_cursor)}`,
        token!,
        c.signal,
      );
      if (current === generation.current)
        setData((d) =>
          d
            ? {
                ...d,
                events: {
                  ...result,
                  items: mergeEvents(result.items, d.events.items),
                },
              }
            : d,
        );
    } catch (e) {
      if (current === generation.current) {
        if (e instanceof ApiError && e.status === 401) disconnect(e.message);
        else setOlderError(errorText(e));
      }
    } finally {
      if (current === generation.current) setOlder(false);
    }
  }
  const task = data?.status.task;
  const sensitiveTags = (taskRead?.tags ?? []).filter((tag) =>
    SENSITIVE_TAGS.includes(String(tag).trim().toLowerCase()),
  );
  const pending: ActionAvailability = {
    allowed: false,
    reason: token
      ? "正在读取任务详情…"
      : "需要一枚持有 read scope 的令牌来读取最新版本与内容哈希。",
  };
  const dispatchAvailability = taskRead ? canDispatch(taskRead, slots) : pending;
  const approveAvailability = taskRead
    ? canApprove(taskRead, slots, covered)
    : pending;
  const cancelAvailability = taskRead ? canCancel(taskRead, slots) : pending;
  const rejectAvailability = selectSlotForAction(slots, "reject")
    ? { allowed: true, reason: null }
    : {
        allowed: false,
        reason: "没有持有 approve scope 的令牌，无法拒绝。",
      };

  function openConfirm(action: ActionName) {
    setActionError("");
    setActionNotice("");
    const summary = buildConfirmSummary(
      {
        goal: taskRead?.goal ?? task?.goal ?? "",
        content_hash: taskRead?.content_hash ?? null,
        status: taskRead?.status ?? task?.status,
      },
      action,
    );
    setConfirm({ action, summary });
  }

  async function runConfirmed() {
    if (!confirm) return;
    const action = confirm.action;
    setBusy(true);
    setActionError("");
    try {
      const slot = selectSlotForAction(slots, action);
      const actionToken = slot ? (slot.secret ?? null) : null;
      if (!actionToken) {
        throw new ApiError(
          403,
          `没有可用于${ACTION_LABELS[action]}的令牌（缺少对应 scope）。`,
        );
      }
      // 确认之后、提交之前：重新拉取版本与内容哈希。
      const fresh = await loadFreshVersion(
        client,
        token ?? actionToken,
        id,
      );
      if (action === "dispatch") {
        await client.dispatchTask(actionToken, id, fresh);
        setConfirm(null);
        setActionNotice(
          "已派发：等待 worker 领取。派发 ≠ 立即执行，需要本机运行 max-agent worker。",
        );
      } else if (action === "approve") {
        await client.approveTask(actionToken, id, { ...fresh, scope: covered });
        setConfirm(null);
        setActionNotice("已批准：任务可以进入执行流程。");
      } else if (action === "reject") {
        await client.rejectTask(actionToken, id, fresh);
        setConfirm(null);
        setActionNotice("已拒绝：该任务不会继续执行。");
      } else {
        try {
          await client.cancelTask(actionToken, id, fresh);
          setConfirm(null);
          setActionNotice("任务已取消。");
        } catch (e) {
          const outcome = resolveCancelOutcome(e);
          if (outcome.kind === "already-cancelled") {
            setConfirm(null);
            setActionNotice("任务已处于 cancelled（已完成），无需再次提交。");
          } else {
            setConfirm(null);
            setActionError(
              outcome.kind === "error"
                ? writeErrorText(outcome.error)
                : writeErrorText(e),
            );
          }
        }
      }
      onChanged();
    } catch (e) {
      setConfirm(null);
      setActionError(writeErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="detail-layer">
      <button className="scrim" onClick={close} aria-label="关闭任务详情" />
      <section
        className="detail"
        role="dialog"
        aria-modal="true"
        aria-label="任务详情"
        ref={panel}
        tabIndex={-1}
      >
        <header>
          <span className="eyebrow">TASK DETAILS</span>
          <button onClick={close} aria-label="关闭详情">
            <X size={20} />
          </button>
        </header>
        {loading ? (
          <div className="loading" role="status">
            <RefreshCw className="spin" size={20} />
            正在读取任务详情…
          </div>
        ) : error ? (
          <div className="error-area" role="alert">
            <h2 id="detail-title">无法读取任务</h2>
            <p>{error}</p>
            <button onClick={() => setRetry((n) => n + 1)}>重试</button>
          </div>
        ) : task && data ? (
          <>
            <div className="detail-title">
              <Badge status={task.status} />
              <h2 id="detail-title">{task.goal}</h2>
              <code>{task.id}</code>
              <div className="detail-meta">
                <span>
                  <Terminal size={14} />
                  {task.worker_kind}
                </span>
                <span>
                  <Clock3 size={14} />
                  {dateTime(task.updated_at)}
                </span>
              </div>
            </div>
            <section className="actions-panel">
              <h3>
                <ShieldCheck size={15} />
                写操作
              </h3>
              <div className="action-row">
                <button
                  className="action"
                  disabled={!dispatchAvailability.allowed}
                  title={dispatchAvailability.reason ?? "派发任务"}
                  onClick={() => openConfirm("dispatch")}
                >
                  派发
                </button>
                <button
                  className="action"
                  disabled={!approveAvailability.allowed}
                  title={approveAvailability.reason ?? "批准任务"}
                  onClick={() => openConfirm("approve")}
                >
                  审批
                </button>
                <button
                  className="action"
                  disabled={!rejectAvailability.allowed}
                  title={rejectAvailability.reason ?? "拒绝任务"}
                  onClick={() => openConfirm("reject")}
                >
                  拒绝
                </button>
                <button
                  className="action danger"
                  disabled={!cancelAvailability.allowed}
                  title={cancelAvailability.reason ?? "取消任务"}
                  onClick={() => openConfirm("cancel")}
                >
                  取消
                </button>
              </div>
              <ul className="action-reasons">
                {!dispatchAvailability.allowed && (
                  <li>派发不可用：{dispatchAvailability.reason}</li>
                )}
                {!approveAvailability.allowed && (
                  <li>审批不可用：{approveAvailability.reason}</li>
                )}
                {!rejectAvailability.allowed && (
                  <li>拒绝不可用：{rejectAvailability.reason}</li>
                )}
                {!cancelAvailability.allowed && (
                  <li>取消不可用：{cancelAvailability.reason}</li>
                )}
              </ul>
              {sensitiveTags.length > 0 && (
                <div className="scope-picker">
                  <span>审批覆盖的敏感标签：</span>
                  {sensitiveTags.map((tag) => (
                    <label key={tag}>
                      <input
                        type="checkbox"
                        checked={covered.includes(tag)}
                        onChange={(e) =>
                          setCovered((current) =>
                            e.target.checked
                              ? [...current, tag]
                              : current.filter((t) => t !== tag),
                          )
                        }
                      />
                      {tag}
                    </label>
                  ))}
                </div>
              )}
              <p className="action-note">
                派发 ≠ 立即执行；需本机运行 max-agent worker。
                <a
                  href="docs/operations/worker.md"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  worker 运维说明
                </a>
              </p>
              {actionNotice && (
                <div className="notice" role="status">
                  {actionNotice}
                </div>
              )}
              {actionError && (
                <div className="error" role="alert">
                  {actionError}
                </div>
              )}
            </section>
            <div className="tabs" role="tablist" aria-label="任务详情视图">
              {[
                ["overview", "概览"],
                ["events", "事件记录"],
                ["evidence", "验证与证据"],
              ].map(([k, v]) => (
                <button
                  key={k}
                  role="tab"
                  aria-selected={tab === k}
                  onClick={() => setTab(k)}
                >
                  {v}
                  {k === "events" && <span>{data.events.items.length}</span>}
                </button>
              ))}
            </div>
            <div className="detail-body" role="tabpanel">
              {tab === "overview" ? (
                <>
                  <div className="metric-grid">
                    <div>
                      <span>执行尝试</span>
                      <b>
                        {task.attempts}
                        <small> / {task.max_attempts}</small>
                      </b>
                    </div>
                    <div>
                      <span>执行器类型</span>
                      <b className="mono">{task.worker_kind}</b>
                    </div>
                  </div>
                  <div className="info-note">
                    <Activity size={16} />
                    <span>
                      {cliProof(data.events.items)}
                      <small>
                        基于已加载 worker_output 事件，不根据类型推断。
                      </small>
                    </span>
                  </div>
                  {task.dependencies?.length ||
                  task.next_retry_at ||
                  task.approval_decision ||
                  task.status === "waiting_approval" ? (
                    <section className="detail-section">
                      <h3>调度与审批</h3>
                      <dl>
                        {task.dependencies?.length ? (
                          <div>
                            <dt>依赖任务</dt>
                            <dd>{task.dependencies.join(", ")}</dd>
                          </div>
                        ) : null}
                        {task.next_retry_at && (
                          <div>
                            <dt>下次重试</dt>
                            <dd>{dateTime(task.next_retry_at)}</dd>
                          </div>
                        )}
                        {task.status === "waiting_approval" && (
                          <div>
                            <dt>审批状态</dt>
                            <dd>等待人工审批 · 可在下方写操作面板处理</dd>
                          </div>
                        )}
                        {task.approval_decision && (
                          <div>
                            <dt>审批决定</dt>
                            <dd>{task.approval_decision}</dd>
                          </div>
                        )}
                        {task.approval_scope?.length ? (
                          <div>
                            <dt>审批范围</dt>
                            <dd>{task.approval_scope.join(", ")}</dd>
                          </div>
                        ) : null}
                        {task.approval_expires_at && (
                          <div>
                            <dt>审批到期</dt>
                            <dd>{dateTime(task.approval_expires_at)}</dd>
                          </div>
                        )}
                      </dl>
                    </section>
                  ) : null}
                  {task.result_summary && (
                    <section className="detail-section">
                      <h3>结果摘要</h3>
                      <p className="prose">{task.result_summary}</p>
                    </section>
                  )}
                  {task.last_error && (
                    <section className="detail-section">
                      <h3>
                        最近错误{" "}
                        {task.failure_class && (
                          <small>{task.failure_class}</small>
                        )}
                      </h3>
                      <div className="error prose">{task.last_error}</div>
                    </section>
                  )}
                  <section className="detail-section">
                    <h3>
                      尝试记录 <span>{data.trace.attempts.length}</span>
                    </h3>
                    {data.trace.attempts.length ? (
                      data.trace.attempts.map((a) => (
                        <div className="attempt" key={a.attempt_no}>
                          <div className="attempt-title">
                            <b>尝试 {String(a.attempt_no).padStart(2, "0")}</b>
                            <Badge status={a.status} />
                          </div>
                          <dl>
                            <div>
                              <dt>执行器</dt>
                              <dd>{a.worker_kind}</dd>
                            </div>
                            <div>
                              <dt>模型 / 提供方</dt>
                              <dd>
                                {a.model || "未报告"} / {a.provider || "未报告"}
                              </dd>
                            </div>
                            <div>
                              <dt>运行时间</dt>
                              <dd>
                                {a.runtime_ms == null
                                  ? "未知"
                                  : `${(a.runtime_ms / 1000).toFixed(2)} 秒`}
                              </dd>
                            </div>
                            <div>
                              <dt>费用</dt>
                              <dd>{costText(a.cost)}</dd>
                            </div>
                            <div>
                              <dt>费用来源</dt>
                              <dd>{a.cost?.source || "unknown"}</dd>
                            </div>
                            {a.failure_class && (
                              <div>
                                <dt>失败类型</dt>
                                <dd>{a.failure_class}</dd>
                              </div>
                            )}
                          </dl>
                        </div>
                      ))
                    ) : (
                      <p className="muted">尚无执行尝试。</p>
                    )}
                  </section>
                  <section className="detail-section">
                    <h3>时间</h3>
                    <dl>
                      <div>
                        <dt>创建于</dt>
                        <dd>{dateTime(task.created_at)}</dd>
                      </div>
                      <div>
                        <dt>更新于</dt>
                        <dd>{dateTime(task.updated_at)}</dd>
                      </div>
                    </dl>
                  </section>
                </>
              ) : tab === "events" ? (
                <>
                  <div className="event-caption">
                    按时间先后显示 · 初始加载最近 50 条
                  </div>
                  {data.events.has_more && (
                    <button
                      className="load-older"
                      disabled={older}
                      onClick={loadOlder}
                    >
                      {older ? "正在加载…" : "加载更早事件"}{" "}
                      <ArrowDownLeft size={14} />
                    </button>
                  )}
                  {olderError && (
                    <p className="error" role="alert">
                      {olderError}
                    </p>
                  )}
                  <div className="timeline">
                    {data.events.items.map((e) => (
                      <article className="event" key={e.id}>
                        <i />
                        <div>
                          <div className="event-heading">
                            <strong>{e.event_type}</strong>
                            <time dateTime={e.created_at}>
                              {dateTime(e.created_at)}
                            </time>
                          </div>
                          <p>{e.message}</p>
                          <details>
                            <summary>
                              查看事件数据 <span>#{e.id}</span>
                            </summary>
                            <Json value={e.payload} />
                          </details>
                        </div>
                      </article>
                    ))}
                  </div>
                  {!data.events.items.length && (
                    <Empty title="尚无事件">该任务没有可显示的事件记录。</Empty>
                  )}
                </>
              ) : (
                <>
                  <section className="detail-section">
                    <h3>
                      <Check size={16} />
                      验收标准
                    </h3>
                    {task.acceptance_criteria.length ? (
                      <ul className="criteria">
                        {task.acceptance_criteria.map((c, i) => (
                          <li key={i}>{c}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="muted">未设置验收标准。</p>
                    )}
                  </section>
                  <section className="detail-section">
                    <h3>验证结果</h3>
                    {data.status.verification ? (
                      <Json value={data.status.verification} />
                    ) : (
                      <p className="muted">暂无验证结果。执行完成不等于验收通过。</p>
                    )}
                  </section>
                  <section className="detail-section">
                    <h3>
                      <Link2 size={16} />
                      证据链
                    </h3>
                    {data.trace.chain.length ? (
                      data.trace.chain.map((c, i) => (
                        <div className="evidence" key={i}>
                          <div>
                            <b>{c.kind}</b>
                            <span>{c.marker || "unknown"}</span>
                          </div>
                          {safeUrl(c.ref) ? (
                            <a
                              href={safeUrl(c.ref)!}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              {c.ref} ↗
                            </a>
                          ) : (
                            c.ref && <p className="mono">{c.ref}</p>
                          )}
                          <Json value={c.detail} />
                        </div>
                      ))
                    ) : (
                      <p className="muted">暂无证据链。不会推断未记录的执行行为。</p>
                    )}
                  </section>
                  <details className="detail-section">
                    <summary>预算与导出元数据</summary>
                    <Json
                      value={{
                        budget: data.trace.budget,
                        exports: data.trace.exports,
                      }}
                    />
                  </details>
                </>
              )}
            </div>
          </>
        ) : null}
        <footer className="detail-footer">
          <LockKeyhole size={12} />
          读操作只读取状态 · 写操作需二次确认
        </footer>
      </section>
      {confirm && confirm.action !== "create" && (
        <ConfirmDialog
          action={confirm.action}
          summary={confirm.summary}
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={runConfirmed}
        />
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
