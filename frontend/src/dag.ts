/**
 * 只读 DAG 视图的数据层。
 *
 * 图完全来自任务已经存在的 `dependencies` 字段：前端不新增任何 graph GET 端点，
 * 也不替后端推测调度顺序，只把「谁依赖谁」如实画出来。
 *
 * 环检测与后端 `max_agent.kernel.dag.find_cycle` / `assert_acyclic` 保持同一规则：
 * 把「节点 → 它依赖的上游」当作有向边做 DFS 三色标记（白/灰/黑），
 * 遇到指向灰色（正在访问中）节点的回边，就得到一个环。
 */
import type { TaskRead } from "./model";

export interface DagNode {
  id: string;
  goal: string;
  status: string;
  awaiting_dispatch: boolean;
}

export interface DagEdge {
  /** 依赖方（前置任务） */
  from: string;
  /** 依赖它的任务 */
  to: string;
}

export interface DagGraph {
  nodes: DagNode[];
  edges: DagEdge[];
  /** 拓扑层级 */
  levels: Record<string, number>;
  /** 检出的环（节点路径）；无环时为空数组 */
  cycles: string[][];
}

const WHITE = 0;
const GRAY = 1;
const BLACK = 2;

function isUsableId(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * 依赖映射：只保留「两端都在当前任务集合里」的上游。
 * 指向缺失上游的依赖既不生成边，也不参与环检测——我们无从知道缺失上游依赖谁。
 */
function dependencyMap(tasks: TaskRead[]): Record<string, string[]> {
  const list = Array.isArray(tasks) ? tasks : [];
  const ids = new Set<string>();
  for (const task of list) {
    if (task && isUsableId(task.id)) ids.add(task.id);
  }
  const map: Record<string, string[]> = {};
  for (const task of list) {
    if (!task || !isUsableId(task.id)) continue;
    if (Object.prototype.hasOwnProperty.call(map, task.id)) continue;
    const raw = Array.isArray(task.dependencies) ? task.dependencies : [];
    const kept: string[] = [];
    for (const dep of raw) {
      if (!isUsableId(dep)) continue;
      if (!ids.has(dep)) continue;
      if (kept.includes(dep)) continue;
      kept.push(dep);
    }
    map[task.id] = kept;
  }
  return map;
}

/** 归一化调用方传入的依赖映射，容忍缺字段 / 非数组值。 */
function normalizeDependencies(
  dependencies: Record<string, string[]>,
): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  if (!dependencies || typeof dependencies !== "object") return out;
  for (const key of Object.keys(dependencies)) {
    const value = dependencies[key];
    out[key] = Array.isArray(value)
      ? value.filter((item): item is string => isUsableId(item))
      : [];
  }
  return out;
}

/** 环的规范化标识：同方向旋转视为同一个环。 */
function cycleKey(cycle: string[]): string {
  if (!cycle.length) return "";
  let at = 0;
  for (let i = 1; i < cycle.length; i += 1) {
    if (cycle[i] < cycle[at]) at = i;
  }
  return cycle.slice(at).concat(cycle.slice(0, at)).join(">");
}

/**
 * 在「节点 → 它的 dependencies」有向图上做 DFS 染色，找出所有环。
 * 返回的每个环是节点 id 路径（不必重复首尾）。
 */
function findCycles(deps: Record<string, string[]>): string[][] {
  const color = new Map<string, number>();
  const stack: string[] = [];
  const cycles: string[][] = [];
  const seen = new Set<string>();

  const visit = (node: string): void => {
    color.set(node, GRAY);
    stack.push(node);
    for (const next of deps[node] ?? []) {
      // 只有在依赖映射里出现过的节点才有出边；其余按叶子处理。
      if (!Object.prototype.hasOwnProperty.call(deps, next)) continue;
      const state = color.get(next) ?? WHITE;
      if (state === WHITE) {
        visit(next);
      } else if (state === GRAY) {
        // 回边：栈顶到 next 之间的那一段就是环。
        const start = stack.indexOf(next);
        if (start >= 0) {
          const cycle = stack.slice(start);
          const key = cycleKey(cycle);
          if (!seen.has(key)) {
            seen.add(key);
            cycles.push(cycle);
          }
        }
      }
    }
    stack.pop();
    color.set(node, BLACK);
  };

  for (const node of Object.keys(deps)) {
    if ((color.get(node) ?? WHITE) === WHITE) visit(node);
  }
  return cycles;
}

/**
 * 检查依赖映射是否存在环。
 * 无环返回 null；有环返回环上的节点路径（不必重复首尾）。
 */
export function assertAcyclic(
  dependencies: Record<string, string[]>,
): string[] | null {
  const cycles = findCycles(normalizeDependencies(dependencies));
  return cycles.length ? cycles[0] : null;
}

/** 任务集合中检出的所有环；无环时是空数组。 */
export function dagCycles(tasks: TaskRead[]): string[][] {
  return findCycles(dependencyMap(tasks));
}

/**
 * 拓扑层级：无依赖节点为 0，其余按最长依赖链递增（a←b←c ⇒ a=0, b=1, c=2）。
 * 环上的节点没有合法层级：先把环内边摘掉，剩下的部分再算，
 * 环内节点最终落在 0 层。
 */
function computeLevels(
  ids: string[],
  deps: Record<string, string[]>,
  cyclic: Set<string>,
): Record<string, number> {
  const chain: Record<string, string[]> = {};
  for (const id of ids) {
    chain[id] = (deps[id] ?? []).filter(
      (dep) => !cyclic.has(dep) && dep !== id,
    );
  }

  const levels: Record<string, number> = {};
  const done = new Set<string>();
  // 显式栈做深度优先：依赖链很长时也不会把调用栈压爆。
  for (const root of ids) {
    if (done.has(root)) continue;
    const stack: Array<{ id: string; deps: string[]; index: number }> = [
      { id: root, deps: chain[root] ?? [], index: 0 },
    ];
    const path = new Set<string>([root]);
    while (stack.length) {
      const frame = stack[stack.length - 1];
      if (frame.index < frame.deps.length) {
        const dep = frame.deps[frame.index++];
        if (done.has(dep) || path.has(dep)) continue;
        path.add(dep);
        stack.push({ id: dep, deps: chain[dep] ?? [], index: 0 });
        continue;
      }
      let level = 0;
      for (const dep of frame.deps) {
        level = Math.max(level, (levels[dep] ?? 0) + 1);
      }
      levels[frame.id] = level;
      done.add(frame.id);
      path.delete(frame.id);
      stack.pop();
    }
  }
  for (const id of ids) {
    if (!Object.prototype.hasOwnProperty.call(levels, id)) levels[id] = 0;
  }
  return levels;
}

/**
 * 由任务集合构建只读 DAG。
 * 节点顺序与传入顺序一致；边方向是「依赖 → 依赖者」。
 */
export function buildDag(tasks: TaskRead[]): DagGraph {
  const list = (Array.isArray(tasks) ? tasks : []).filter(
    (task): task is TaskRead =>
      !!task && isUsableId((task as TaskRead).id),
  );
  const deps = dependencyMap(list);

  const nodes: DagNode[] = [];
  const seen = new Set<string>();
  for (const task of list) {
    if (seen.has(task.id)) continue;
    seen.add(task.id);
    nodes.push({
      id: task.id,
      goal: typeof task.goal === "string" ? task.goal : "",
      status:
        typeof task.status === "string" && task.status.trim()
          ? task.status
          : "unknown",
      // 列表接口不返回 awaiting_dispatch 时按 false 处理：不猜测「可以派发」。
      awaiting_dispatch: task.awaiting_dispatch === true,
    });
  }

  const edges: DagEdge[] = [];
  for (const node of nodes) {
    for (const dep of deps[node.id] ?? []) {
      edges.push({ from: dep, to: node.id });
    }
  }

  const cycles = findCycles(deps);
  const cyclic = new Set<string>();
  for (const cycle of cycles) {
    for (const id of cycle) cyclic.add(id);
  }

  return {
    nodes,
    edges,
    levels: computeLevels(
      nodes.map((node) => node.id),
      deps,
      cyclic,
    ),
    cycles,
  };
}
