/**
 * search store —— M5 Hybrid Search（architecture.md §12）。
 * 状态：mode(keyword|semantic|hybrid，默认 hybrid) / results / parsed（FilterChips）/
 *       degraded_channels / searching / error / searched。
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import type { ParsedQuery, SearchMode, SearchResultItem } from "../../../shared/contracts";

const EMPTY_PARSED: ParsedQuery = {
  time_range: null,
  file_types: [],
  extensions: [],
  semantic_text: "",
  parse_method: "rule",
};

export const useSearchStore = defineStore("search", () => {
  const query = ref("");
  const mode = ref<SearchMode>("hybrid"); // M5 §14：当前默认 Hybrid
  const results = ref<SearchResultItem[]>([]);
  const total = ref(0);
  const latencyMs = ref<number | null>(null);
  const parsed = ref<ParsedQuery>(EMPTY_PARSED);
  const degradedChannels = ref<string[]>([]);
  const searching = ref(false);
  const error = ref<string | null>(null);
  /** 是否已执行过搜索（空 query 或未搜索时不显示 empty 状态） */
  const searched = ref(false);

  function reset(): void {
    results.value = [];
    total.value = 0;
    latencyMs.value = null;
    parsed.value = EMPTY_PARSED;
    degradedChannels.value = [];
    error.value = null;
    searched.value = false;
  }

  /** 切换模式：清空结果（避免跨模式渲染结构不匹配——M4 GUI 修复）。 */
  function setMode(m: SearchMode): void {
    if (mode.value === m) return;
    mode.value = m;
    reset();
  }

  async function search(): Promise<void> {
    const q = query.value.trim();
    if (!q) {
      reset();
      return;
    }
    searching.value = true;
    error.value = null;
    try {
      const resp = await window.omnisearch.search({ query: q, topK: 50, mode: mode.value });
      results.value = resp.results;
      total.value = resp.total;
      latencyMs.value = resp.latency_ms;
      parsed.value = resp.parsed;
      degradedChannels.value = resp.degraded_channels;
    } catch (e) {
      error.value = e instanceof Error ? e.message : "搜索失败";
      results.value = [];
      total.value = 0;
    } finally {
      searched.value = true;
      searching.value = false;
    }
  }

  return {
    query, mode, results, total, latencyMs, parsed, degradedChannels,
    searching, error, searched, search, reset, setMode,
  };
});
