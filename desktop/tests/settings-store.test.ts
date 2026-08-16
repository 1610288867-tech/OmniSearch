// @vitest-environment jsdom
/**
 * settings store 测试（M5 §16）：load / save（即时 PUT 持久化）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSettingsStore } from "../src/renderer/src/stores/settings";
import type { SettingsResponse } from "../src/shared/contracts";

const RESP: SettingsResponse = {
  search_mode: "hybrid", w_kw: 1.0, w_sem: 1.0, topK: 50,
  index_roots: [{ path: "D:\\photos", enabled: true, created_at: 1 }], models: { bge: "ok", caption: "ok" },
  storage: { db_bytes: 1024, models_bytes: 2048 },
};

function mockApi(): { getSettings: ReturnType<typeof vi.fn>; setSettings: ReturnType<typeof vi.fn> } {
  const getSettings = vi.fn().mockResolvedValue(RESP);
  const setSettings = vi.fn().mockResolvedValue({ ...RESP, search_mode: "semantic" });
  (window as any).omnisearch = { getSettings, setSettings };
  return { getSettings, setSettings };
}

describe("settings store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("load：填充模式/权重/topK/roots/models/storage", async () => {
    const { getSettings } = mockApi();
    const store = useSettingsStore();
    await store.load();
    expect(getSettings).toHaveBeenCalled();
    expect(store.loaded).toBe(true);
    expect(store.searchMode).toBe("hybrid");
    expect(store.wKw).toBe(1.0);
    expect(store.indexRoots).toEqual([{ path: "D:\\photos", enabled: true, created_at: 1 }]);
    expect(store.models["bge"]).toBe("ok");
    expect(store.storage.models_bytes).toBe(2048);
  });

  it("save：PUT 后应用后端返回值", async () => {
    const { setSettings } = mockApi();
    const store = useSettingsStore();
    await store.load();
    await store.save({ search_mode: "semantic" });
    expect(setSettings).toHaveBeenCalledWith({ search_mode: "semantic" });
    expect(store.searchMode).toBe("semantic");
  });

  it("save 失败：error 状态", async () => {
    (window as any).omnisearch = {
      getSettings: vi.fn().mockResolvedValue(RESP),
      setSettings: vi.fn().mockRejectedValue(new Error("BACKEND_UNAVAILABLE")),
    };
    const store = useSettingsStore();
    await store.save({ topK: 10 });
    expect(store.error).toBe("BACKEND_UNAVAILABLE");
  });
});
