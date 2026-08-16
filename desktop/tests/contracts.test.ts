/**
 * 契约冒烟测试（vitest）：IPC channel 常量与类型对齐（architecture.md §4.2）。
 */
import { describe, expect, it } from "vitest";
import { IPC } from "../src/shared/contracts";

describe("IPC contracts", () => {
  it("channel 名称唯一且与 preload/main 使用一致", () => {
    const names = Object.values(IPC);
    expect(new Set(names).size).toBe(names.length);
    expect(names).toContain("system:status");
    expect(names).toContain("health:events");
    expect(names).toContain("search:query");
    expect(names).toContain("settings:get");
  });
});
