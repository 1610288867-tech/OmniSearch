// @vitest-environment jsdom
/**
 * index store 测试（扫描位置管理）：roots 加载 / 添加（含错误映射）/ 删除 / 切换 / 进度。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useIndexStore } from "../src/renderer/src/stores/index";
import type { IndexRoot, RootsResponse } from "../src/shared/contracts";

const ROOT_A: IndexRoot = { path: "D:\\Photos", enabled: true, created_at: 1, file_count: 42 };
const ROOT_B: IndexRoot = { path: "E:\\Projects", enabled: false, created_at: 2, file_count: 7 };

function mockApi() {
  const getRoots = vi.fn().mockResolvedValue({ roots: [ROOT_A, ROOT_B] } satisfies RootsResponse);
  const listDrives = vi.fn().mockResolvedValue(["C:\\", "D:\\"]);
  const addRoot = vi.fn().mockResolvedValue(ROOT_A);
  const removeRoot = vi.fn().mockResolvedValue({ roots: [ROOT_B] });
  const toggleRoot = vi.fn().mockResolvedValue({ ...ROOT_A, enabled: false });
  const getIndexStatus = vi.fn().mockResolvedValue({
    running: true,
    jobs: [
      { id: 1, root_path: "D:\\Photos", scan_type: "full", status: "DONE", total_files: 42, scanned_files: 42, error_count: 0, started_at: 1, finished_at: 2 },
      { id: 2, root_path: "E:\\Projects", scan_type: "full", status: "RUNNING", total_files: 52, scanned_files: 12, error_count: 0, started_at: 3, finished_at: null },
    ],
  });
  (window as any).omnisearch = { getRoots, listDrives, addRoot, removeRoot, toggleRoot, getIndexStatus };
  return { getRoots, listDrives, addRoot, removeRoot, toggleRoot, getIndexStatus };
}

describe("index store (扫描位置管理)", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("loadRoots：填充 root 列表", async () => {
    const { getRoots } = mockApi();
    const store = useIndexStore();
    await store.loadRoots();
    expect(getRoots).toHaveBeenCalled();
    expect(store.roots.map((r) => r.path)).toEqual(["D:\\Photos", "E:\\Projects"]);
  });

  it("addRoot：成功后刷新 roots + 状态", async () => {
    const { addRoot } = mockApi();
    const store = useIndexStore();
    const res = await store.addRoot("D:\\Photos");
    expect(addRoot).toHaveBeenCalledWith("D:\\Photos");
    expect(res.ok).toBe(true);
    expect(store.roots.length).toBe(2);
  });

  it("addRoot 失败（ROOT_ALREADY_COVERED）：ok=false + 错误展示", async () => {
    mockApi();
    (window as any).omnisearch.addRoot = vi.fn().mockRejectedValue(new Error("ROOT_ALREADY_COVERED"));
    const store = useIndexStore();
    const res = await store.addRoot("D:\\");
    expect(res.ok).toBe(false);
    expect(res.message).toContain("ROOT_ALREADY_COVERED");
    expect(store.error).toContain("ROOT_ALREADY_COVERED");
  });

  it("removeRoot / toggleRoot：调用 API 并刷新", async () => {
    mockApi();
    // 调用序列：初始 2 个 → 删除后 1 个 → 切换后 2 个（D:\Photos 禁用）
    (window as any).omnisearch.getRoots
      .mockResolvedValueOnce({ roots: [ROOT_A, ROOT_B] })
      .mockResolvedValueOnce({ roots: [ROOT_B] })
      .mockResolvedValueOnce({ roots: [{ ...ROOT_A, enabled: false }, ROOT_B] });
    const store = useIndexStore();
    await store.loadRoots();
    await store.removeRoot("D:\\Photos");
    expect(window.omnisearch.removeRoot).toHaveBeenCalledWith("D:\\Photos");
    expect(store.roots.map((r) => r.path)).toEqual(["E:\\Projects"]);
    await store.toggleRoot("D:\\Photos", false);
    expect(window.omnisearch.toggleRoot).toHaveBeenCalledWith("D:\\Photos", false);
    expect(store.roots.find((r) => r.path === "D:\\Photos")?.enabled).toBe(false);
  });

  it("扫描进度：runningJob + 当前 Root 序号（多 Root 顺序扫描）", async () => {
    mockApi();
    const store = useIndexStore();
    await store.loadStatus();
    expect(store.runningJob()?.root_path).toBe("E:\\Projects");
    expect(store.runningRootIndex()).toBe(2); // 第 2 个 job（D:\Photos 已完成 → 当前 2/2）
  });
});
