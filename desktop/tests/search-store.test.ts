// @vitest-environment jsdom
/**
 * search store 测试（M5：Hybrid Search）。
 * mock window.omnisearch.search，验证 store 状态机：loading / results / empty / error /
 * 模式切换清空 / parsed chips / degraded。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSearchStore } from "../src/renderer/src/stores/search";
import type { SearchResponse, SearchResultItem } from "../src/shared/contracts";

function makeResponse(filename: string, total = 1, degraded: string[] = []): SearchResponse {
  const item: SearchResultItem = {
    file_id: 1, path: `/x/${filename}`, filename, dir_path: "/x",
    extension: ".txt", file_type: "doc", size_bytes: 10, mtime_ns: 1,
    rrf_score: 0.03, keyword_score: 5.0, semantic_score: null,
    time_info: { basis: null, confidence: null, value: null },
    match_reasons: [{ channel: "keyword", text: "文件名匹配", score: 5.0 }],
  };
  return {
    query: filename, total, latency_ms: 3, results: total ? [item] : [],
    parsed: {
      time_range: null, file_types: [], extensions: [], semantic_text: filename, parse_method: "rule",
    },
    degraded_channels: degraded,
  };
}

function mockSearch(resp: SearchResponse): ReturnType<typeof vi.fn> {
  const search = vi.fn().mockResolvedValue(resp);
  (window as any).omnisearch = { search };
  return search;
}

describe("search store (M5 hybrid)", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("默认模式为 hybrid（M5 §14）", () => {
    const store = useSearchStore();
    expect(store.mode).toBe("hybrid");
  });

  it("成功搜索：loading → results + parsed + degraded", async () => {
    const search = mockSearch(makeResponse("resume.pdf", 1, ["semantic"]));
    const store = useSearchStore();
    store.query = "resume";
    const p = store.search();
    expect(store.searching).toBe(true);
    await p;
    expect(store.searching).toBe(false);
    expect(store.results[0].filename).toBe("resume.pdf");
    expect(store.total).toBe(1);
    expect(store.error).toBeNull();
    expect(store.searched).toBe(true);
    expect(store.parsed.semantic_text).toBe("resume.pdf");
    expect(store.degradedChannels).toEqual(["semantic"]);
    expect(search).toHaveBeenCalledWith(expect.objectContaining({ mode: "hybrid" }));
  });

  it("空 query：reset，不触发搜索", async () => {
    const search = mockSearch(makeResponse("x"));
    const store = useSearchStore();
    store.query = "   ";
    await store.search();
    expect(search).not.toHaveBeenCalled();
    expect(store.searched).toBe(false);
  });

  it("无结果：empty 状态（searched=true 且 results=[]）", async () => {
    mockSearch(makeResponse("zzz", 0));
    const store = useSearchStore();
    store.query = "zzz";
    await store.search();
    expect(store.searched).toBe(true);
    expect(store.results).toEqual([]);
    expect(store.error).toBeNull();
  });

  it("后端失败：error 状态（结果清空）", async () => {
    const search = vi.fn().mockRejectedValue(new Error("BACKEND_UNAVAILABLE"));
    (window as any).omnisearch = { search };
    const store = useSearchStore();
    store.query = "x";
    await store.search();
    expect(store.error).toBe("BACKEND_UNAVAILABLE");
    expect(store.results).toEqual([]);
    expect(store.searching).toBe(false);
  });

  it("setMode：三模式循环切换并清空结果（防跨模式渲染结构不匹配，M4 GUI 修复）", async () => {
    mockSearch(makeResponse("resume.pdf"));
    const store = useSearchStore();
    store.query = "resume";
    await store.search();
    expect(store.results.length).toBe(1);
    store.setMode("keyword");
    expect(store.mode).toBe("keyword");
    expect(store.results).toEqual([]);
    expect(store.searched).toBe(false);
    store.setMode("semantic");
    expect(store.mode).toBe("semantic");
    store.setMode("hybrid");
    expect(store.mode).toBe("hybrid");
  });

  it("setMode 同模式不重置", async () => {
    mockSearch(makeResponse("resume.pdf"));
    const store = useSearchStore();
    store.query = "resume";
    await store.search();
    store.setMode("hybrid"); // 已是 hybrid
    expect(store.results.length).toBe(1);
  });
});
