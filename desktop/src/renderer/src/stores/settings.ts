/**
 * settings store —— M5 §16：search mode / weights / topK / index roots / model status / storage。
 * 变更即时 PUT 后端（settings KV 表持久化）。
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import type { SearchMode, SettingsResponse } from "../../../shared/contracts";

export const useSettingsStore = defineStore("settings", () => {
  const loaded = ref(false);
  const searchMode = ref<SearchMode>("hybrid");
  const wKw = ref(1.0);
  const wSem = ref(1.0);
  const topK = ref(50);
  const indexRoots = ref<string[]>([]);
  const models = ref<Record<string, string>>({});
  const storage = ref({ db_bytes: 0, models_bytes: 0 });
  const saving = ref(false);
  const error = ref<string | null>(null);

  function apply(s: SettingsResponse): void {
    searchMode.value = s.search_mode;
    wKw.value = s.w_kw;
    wSem.value = s.w_sem;
    topK.value = s.topK;
    indexRoots.value = s.index_roots;
    models.value = s.models;
    storage.value = s.storage;
    loaded.value = true;
  }

  async function load(): Promise<void> {
    try {
      apply(await window.omnisearch.getSettings());
    } catch (e) {
      error.value = e instanceof Error ? e.message : "设置加载失败";
    }
  }

  async function save(patch: Record<string, unknown>): Promise<void> {
    saving.value = true;
    error.value = null;
    try {
      apply(await window.omnisearch.setSettings(patch));
    } catch (e) {
      error.value = e instanceof Error ? e.message : "设置保存失败";
    } finally {
      saving.value = false;
    }
  }

  return { loaded, searchMode, wKw, wSem, topK, indexRoots, models, storage, saving, error, load, save, apply };
});
