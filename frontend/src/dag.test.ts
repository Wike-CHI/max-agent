import { describe, expect, it } from "vitest";

import { assertAcyclic, buildDag, dagCycles } from "./dag";
import type { TaskRead } from "./model";

function task(id: string, dependencies: string[] = [], overrides: Partial<TaskRead> = {}): TaskRead {
  return {
    id,
    goal: `goal ${id}`,
    status: "pending",
    version: 1,
    awaiting_dispatch: true,
    content_hash: "c".repeat(64),
    dependencies,
    tags: [],
    created_by: "tester",
    ...overrides,
  };
}

describe("DAG 构图", () => {
  it("节点与边都来自既有 dependencies 字段", () => {
    const graph = buildDag([task("a"), task("b", ["a"]), task("c", ["a", "b"])]);

    expect(graph.nodes.map((n) => n.id)).toEqual(["a", "b", "c"]);
    expect(graph.edges).toEqual([
      { from: "a", to: "b" },
      { from: "a", to: "c" },
      { from: "b", to: "c" },
    ]);
  });

  it("边方向是 依赖 → 依赖者", () => {
    const graph = buildDag([task("up"), task("down", ["up"])]);
    const edge = graph.edges[0];
    expect(edge.from).toBe("up");
    expect(edge.to).toBe("down");
  });

  it("按依赖层级分层", () => {
    const graph = buildDag([task("a"), task("b", ["a"]), task("c", ["b"])]);
    expect(graph.levels.a).toBe(0);
    expect(graph.levels.b).toBe(1);
    expect(graph.levels.c).toBe(2);
  });

  it("只保留图中出现的依赖边（不新增 graph GET 端点）", () => {
    const graph = buildDag([task("a", ["missing-upstream"])]);
    expect(graph.edges).toEqual([]);
    expect(graph.nodes).toHaveLength(1);
  });

  it("携带节点状态与派发门标记", () => {
    const graph = buildDag([
      task("a", [], { status: "waiting_approval", awaiting_dispatch: false }),
    ]);
    expect(graph.nodes[0].status).toBe("waiting_approval");
    expect(graph.nodes[0].awaiting_dispatch).toBe(false);
  });
});

describe("环状依赖被拒绝", () => {
  it("两节点环能被检出", () => {
    const graph = buildDag([task("a", ["b"]), task("b", ["a"])]);
    expect(graph.cycles.length).toBeGreaterThan(0);
    expect(graph.cycles[0]).toContain("a");
    expect(graph.cycles[0]).toContain("b");
  });

  it("三节点环能被检出", () => {
    const graph = buildDag([task("a", ["c"]), task("b", ["a"]), task("c", ["b"])]);
    expect(graph.cycles.length).toBeGreaterThan(0);
  });

  it("无环时 cycles 为空且 assertAcyclic 返回 null", () => {
    const graph = buildDag([task("a"), task("b", ["a"])]);
    expect(graph.cycles).toEqual([]);
    expect(assertAcyclic({ a: [], b: ["a"] })).toBeNull();
  });

  it("assertAcyclic 返回环路径", () => {
    const cycle = assertAcyclic({ a: ["b"], b: ["a"] });
    expect(cycle).not.toBeNull();
    expect(cycle).toContain("a");
    expect(cycle).toContain("b");
  });

  it("dagCycles 与 buildDag 的判定一致", () => {
    const tasks = [task("a", ["b"]), task("b", ["a"])];
    expect(dagCycles(tasks).length).toBe(buildDag(tasks).cycles.length);
  });
});
