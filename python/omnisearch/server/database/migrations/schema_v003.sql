-- ============================================================
-- OmniSearch schema v003（M5 收口 4：Health readiness）
-- Worker 心跳表：/health 的 worker_ready 判定依据（Worker 主循环每 5s 写一次，
-- 超过 15s 未更新视为 Worker 不可用）。单机单 Worker，worker_id 恒为 'worker'。
-- ============================================================
CREATE TABLE worker_heartbeat (
    worker_id TEXT PRIMARY KEY,
    last_seen INTEGER NOT NULL
);
