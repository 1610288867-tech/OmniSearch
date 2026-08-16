// @vitest-environment jsdom
/**
 * SearchPage UI 测试（M1 阶段 6 E + M5 Hybrid：loading / results / empty / error 渲染）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import SearchPage from "../src/renderer/src/views/SearchPage.vue";
import type { SearchResponse, SearchResultItem } from "../src/shared/contracts";

const item: SearchResultItem = {
  file_id: 1, path: "/x/resume.pdf", filename: "resume.pdf", dir_path: "/x",
  extension: ".pdf", file_type: "doc", size_bytes: 2048, mtime_ns: 1_700_000_000_000_000_000,
  rrf_score: 0.032, keyword_score: 5.0, semantic_score: null,
  time_info: { basis: null, confidence: null, value: null },
  match_reasons: [{ channel: "keyword", text: "文件名匹配", score: 5.0 }],
};

function resp(total: number, degraded: string[] = []): SearchResponse {
  return {
    query: "resume", total, latency_ms: 2, results: total ? [item] : [],
    parsed: {
      time_range: null, file_types: [], extensions: [], semantic_text: "resume", parse_method: "rule",
    },
    degraded_channels: degraded,
  };
}

describe("SearchPage", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    (window as any).omnisearch = {
      getSystemStatus: vi.fn().mockResolvedValue({ backend: null, worker: { state: "running" }, fastapi: { state: "running" }, qdrant: { state: "running" } }),
      onHealthStatus: vi.fn(() => () => {}),
      search: vi.fn(),
      searchSemantic: vi.fn().mockResolvedValue({ query: "", total: 0, latency_ms: 0, results: [] }),
      getSettings: vi.fn().mockRejectedValue(new Error("n/a")),
    };
  });

  /** 点击搜索按钮（最后一个按钮，前一个是模式切换） */
  async function clickSearch(wrapper: ReturnType<typeof mount>) {
    const buttons = wrapper.findAll("button");
    await buttons[buttons.length - 1].trigger("click");
    await new Promise((r) => setTimeout(r, 0));
  }

  it("results：输入关键词搜索后显示结果卡（RRF + match reasons）", async () => {
    (window as any).omnisearch.search.mockResolvedValue(resp(1));
    const wrapper = mount(SearchPage);
    await wrapper.find("input").setValue("resume");
    await clickSearch(wrapper);
    expect(wrapper.text()).toContain("resume.pdf");
    expect(wrapper.text()).toContain("共 1 个结果");
    expect(wrapper.text()).toContain("文件名匹配");
  });

  it("degraded：语义通道降级提示（M5 §12.8）", async () => {
    (window as any).omnisearch.search.mockResolvedValue(resp(1, ["semantic"]));
    const wrapper = mount(SearchPage);
    await wrapper.find("input").setValue("resume");
    await clickSearch(wrapper);
    expect(wrapper.text()).toContain("语义通道不可用");
  });

  it("empty：无结果显示空状态", async () => {
    (window as any).omnisearch.search.mockResolvedValue(resp(0));
    const wrapper = mount(SearchPage);
    await wrapper.find("input").setValue("zzz");
    await clickSearch(wrapper);
    expect(wrapper.text()).toContain("未找到匹配");
  });

  it("error：后端失败显示错误信息", async () => {
    (window as any).omnisearch.search.mockRejectedValue(new Error("BACKEND_UNAVAILABLE"));
    const wrapper = mount(SearchPage);
    await wrapper.find("input").setValue("x");
    await clickSearch(wrapper);
    expect(wrapper.text()).toContain("搜索失败");
  });

  it("loading：搜索期间显示搜索中", async () => {
    let resolveFn: (v: SearchResponse) => void;
    (window as any).omnisearch.search.mockImplementation(
      () => new Promise<SearchResponse>((resolve) => (resolveFn = resolve)),
    );
    const wrapper = mount(SearchPage);
    await wrapper.find("input").setValue("resume");
    const buttons = wrapper.findAll("button");
    await buttons[buttons.length - 1].trigger("click");
    await new Promise((r) => setTimeout(r, 0));
    expect(wrapper.text()).toContain("搜索中");
    resolveFn!(resp(1));
  });
});
