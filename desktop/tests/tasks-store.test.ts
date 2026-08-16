// @vitest-environment jsdom
/**
 * tasks store 测试（M5 §17 Task Dashboard）：stats / failed / retry。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useTasksStore } from "../src/renderer/src/stores/tasks";

function mockApi() {
  const getTaskStatus = vi.fn().mockResolvedValue({
    queue_length: 2, processing: 1, success: 10, failed: 1, total: 14,
  });
  const getFailedTasks = vi.fn().mockResolvedValue([
    { id: 7, file_id: 3, filename: "bad.pdf", attempt: 2, max_attempts: 3, last_error: "ocr failed" },
  ]);
  const retryTask = vi.fn().mockResolvedValue({ status: "retried" });
  (window as any).omnisearch = { getTaskStatus, getFailedTasks, retryTask };
  return { getTaskStatus, getFailedTasks, retryTask };
}

describe("tasks store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("load：stats + failed 列表", async () => {
    const { getTaskStatus, getFailedTasks } = mockApi();
    const store = useTasksStore();
    await store.load();
    expect(getTaskStatus).toHaveBeenCalled();
    expect(getFailedTasks).toHaveBeenCalled();
    expect(store.stats.queue_length).toBe(2);
    expect(store.stats.failed).toBe(1);
    expect(store.failed[0].filename).toBe("bad.pdf");
    expect(store.failed[0].attempt).toBe(2);
  });

  it("retry：调用 API 并刷新列表", async () => {
    const { retryTask, getFailedTasks } = mockApi();
    const store = useTasksStore();
    await store.load();
    const status = await store.retry(7);
    expect(retryTask).toHaveBeenCalledWith(7);
    expect(status).toBe("retried");
    expect(getFailedTasks).toHaveBeenCalledTimes(2); // load + retry 后刷新
  });
});
