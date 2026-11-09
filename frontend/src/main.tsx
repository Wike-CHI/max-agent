import React, { useEffect, useRef, useState } from "react";
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
  Layers3,
  Link2,
  LockKeyhole,
  LogOut,
  RefreshCw,
  Search,
  ShieldCheck,
  Terminal,
  X,
} from "lucide-react";
import {
  ApiError,
  cliProof,
  costText,
  dateTime,
  errorText,
  get,
  mergeEvents,
  safeUrl,
  statuses,
  taskQuery,
  type EventPage,
  type Status,
  type Task,
  type Trace,
} from "./model";
import "./style.css";
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
function App() {
  const [token, setToken] = useState("");
  const [notice, setNotice] = useState("");
  const [session, setSession] = useState(0);
  function disconnect(message = "") {
    setToken("");
    setNotice(message);
    setSession((s) => s + 1);
  }
  return token ? (
    <Console key={session} token={token} disconnect={disconnect} />
  ) : (
    <div className="connect-shell">
      <div className="connect-brand">
        <span className="logo">M</span>
        <b>MAX</b>
        <span>任务优先，结果可验证。</span>
      </div>
      <main className="connect-card">
        <span className="eyebrow">MAX WORKSPACE / 只读访问</span>
        <div className="connect-icon">
          <LockKeyhole size={27} />
        </div>
        <h1>连接你的任务空间</h1>
        <p>
          把执行过程放在眼前。查看任务、追踪事件，
          <br />
          核对每一次尝试的结果与证据。
        </p>
        {notice && (
          <div className="error" role="alert">
            {notice}
          </div>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const form = e.currentTarget;
            const input = form.elements.namedItem("token") as HTMLInputElement;
            const value = input.value.trim();
            input.value = "";
            if (value) {
              setNotice("");
              setToken(value);
            }
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
          <button className="primary" type="submit">
            连接控制台 <ArrowRight size={16} />
          </button>
        </form>
        <p className="security-note">
          <ShieldCheck size={15} />
          令牌仅保存在当前页面内存中，不会写入浏览器存储。
        </p>
      </main>
      <footer>
        MAX CONSOLE <span>只读模式 · 不会创建、执行或审批任务</span>
      </footer>
    </div>
  );
}
function Console({
  token,
  disconnect,
}: {
  token: string;
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
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError("");
    setData(null);
    get<{ items: Task[]; total: number; next_cursor: string | null }>(
      taskQuery(search, filter, cursors[page]),
      token,
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
  }, [search, filter, page, cursors, refresh, token]);
  function choose(id: string) {
    window.location.hash = `task=${encodeURIComponent(id)}`;
    setSelected(id);
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
              只读模式<small>观察执行，不改变状态</small>
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
              <LockKeyhole size={12} /> READ ONLY
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
            <button
              className="refresh"
              onClick={() => {
                setPage(0);
                setCursors([null]);
                setRefresh((n) => n + 1);
              }}
              disabled={loading}
            >
              <RefreshCw size={16} className={loading ? "spin" : ""} />
              刷新
            </button>
          </div>
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
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
            </div>
            {loading ? (
              <div className="loading" role="status">
                <RefreshCw className="spin" size={20} />
                正在读取任务…
              </div>
            ) : error ? (
              <div className="error-area" role="alert">
                <p>{error}</p>
                <button
                  onClick={() => {
                    setPage(0);
                    setCursors([null]);
                    setRefresh((n) => n + 1);
                  }}
                >
                  重试
                </button>
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
                  : "任务创建后会在这里显示。此控制台只提供查看功能。"}
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
          token={token}
          refresh={refresh}
          disconnect={disconnect}
          close={() => {
            window.location.hash = "";
            setSelected("");
          }}
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
  token,
  refresh,
  disconnect,
  close,
}: {
  id: string;
  token: string;
  refresh: number;
  disconnect: (message?: string) => void;
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
        token,
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
                            <dd>等待人工审批 · 请通过 CLI / API 操作</dd>
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
                      <p className="muted">
                        暂无验证结果。执行完成不等于验收通过。
                      </p>
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
                      <p className="muted">
                        暂无证据链。不会推断未记录的执行行为。
                      </p>
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
          只读查看 · 不执行任何任务操作
        </footer>
      </section>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
