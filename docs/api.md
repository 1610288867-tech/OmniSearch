# OmniSearch API 参考（MVP Final）

Base：`http://127.0.0.1:{port}/api/v1`（开发默认 8734；Electron Main 探测注入）。
鉴权：Electron Main / dev.py 生成随机 token，请求须带 `X-Omni-Token` 头；`/health` 放行。
与 `desktop/src/shared/contracts.ts`、`python/omnisearch/server/api/schemas.py` 对齐（单一事实源）。

---

## GET /health

就绪探针（Electron Main 每 5s 探测）。返回四组件 readiness（MVP 收口 4）：

```json
{
  "status": "ok",                    // 仅由 sqlite/qdrant/worker 决定（语义失败 ≠ 服务崩溃）
  "version": "0.1.0",
  "components": {
    "sqlite":   {"ok": true},
    "qdrant":   {"ok": true},
    "worker":   {"ok": true},        // worker_heartbeat 表 15s 内有心跳
    "semantic": {"ok": true}         // BGE + Qdrant 语义通道就绪；false → 语义自动降级
  }
}
```

---

## POST /search —— Hybrid Search（核心）

```jsonc
// 请求
{ "query": "昨天的自由女神照片", "topK": 50, "mode": "hybrid", "stages": false }
//   mode:   keyword | semantic | hybrid（缺省 hybrid）
//   stages: true 时返回分项耗时（parser/fts/semantic/finalize/total，benchmark 用）
```

```jsonc
// 响应
{
  "query": "昨天的自由女神照片",
  "parsed": {
    "time_range": {"from": "2026-08-15T00:00:00+08:00", "to": "2026-08-16T00:00:00+08:00", "basis_hint": "exif"},
    "file_types": ["image"],
    "extensions": [],
    "semantic_text": "自由女神",
    "parse_method": "rule"           // rule | fallback（Parser 失败 → semantic_text=原始 query）
  },
  "results": [{
    "file_id": 42, "path": "D:\\Photos\\IMG_08421.jpg", "filename": "IMG_08421.jpg",
    "dir_path": "D:\\Photos", "extension": ".jpg", "file_type": "image",
    "size_bytes": 204800, "mtime_ns": 1755242400000000000,  // UTC 纳秒时间戳（示例值）
    "rrf_score": 0.0371,             // 仅 FTS+Vector 双通道；metadata-only = null
    "keyword_score": 18.4,           // BM25 原始分；未命中通道 = null
    "semantic_score": 0.81,          // cosine；未命中通道 = null
    "time_info": {"basis": "exif", "confidence": "exact", "value": "2026-08-15T10:00:00+08:00"},
    "match_reasons": [
      {"channel": "keyword",  "text": "文件名匹配", "score": 18.4},
      {"channel": "ocr",      "text": "识别到文字：New York 2026", "score": 6.1},
      {"channel": "semantic", "text": "AI 描述：自由女神像照片", "score": 0.81},
      {"channel": "metadata", "text": "拍摄于 2026-08-15T10:00:00+08:00", "basis": "exif", "confidence": "exact"}
    ]
  }],
  "total": 23,
  "latency_ms": 148,
  "degraded_channels": []            // ["keyword" | "semantic"]，§12.8 降级矩阵
}
```

行为要点：

- **QueryParser**（规则）：今天/昨天/前天/本周/上周/本月/具体日期/日期区间；类型词仅在语义末尾抽取（「图片搜索系统」不误判）；动词决定时间字段（拍/摄/照→exif，创建/保存→ctime，修改→mtime）
- **双通道并行**：FTS 1s / Vector 3s 超时；超时通道降级标注，另一通道照常返回；双通道均失败 → 502
- **Metadata-only**：`semantic_text` 为空且有过滤条件（时间/类型/扩展名）→ 按 basis 时间倒序返回文件（三分数全 null，仅 metadata 原因）
- **时间过滤**：EXIF exact（hard）/ mtime·ctime fallback；canonical WHERE 三处一致（files 查询 / FTS join / 向量回表）

## POST /search/semantic（兼容）

M4 独立语义通道（M5 起 UI 已合并进 /search，端点保留）：`{query, topK}` → `{query, total, latency_ms, results: [{file_id, path, filename, source_type, chunk_index, text, semantic_score}]}`。

## 索引

| Method | Path | 说明 |
|---|---|---|
| POST | `/index/scan` | `{root_paths: [..], scan_type: "full"|"incremental"}` → `{job_id, root_path, status}`（后台执行） |
| GET | `/index/status` | `{running, jobs: [...]}`（最近作业；多 Root 顺序扫描进度） |

## 扫描位置管理（Roots，产品增强）

| Method | Path | 说明 |
|---|---|---|
| GET | `/index/roots` | `{roots: [{path, enabled, created_at, file_count}]}`（file_count = 已索引未删除文件数） |
| POST | `/index/roots/add` | `{path}` → 规范化 + 校验后添加并**自动开始后台 full scan**；错误：`INVALID_ROOT`（400，不存在/不可访问）、`ROOT_ALREADY_EXISTS`（400，重复）、`ROOT_ALREADY_COVERED`（400，父子冲突，双向检测） |
| POST | `/index/roots/remove` | `{path}` → 停止监听 + 不再扫描；**已索引数据保留**（不 DELETE 记录，默认语义） |
| POST | `/index/roots/toggle` | `{path, enabled}` → 禁用=停止监听（数据保留）；启用=恢复监听（不自动重扫）；`ROOT_NOT_FOUND`（404） |

规则：path 经 `canonical_root` 规范化（大小写 / slash / trailing slash / 盘符根统一）；重复与父子检测大小写不敏感；多 Root 顺序扫描（每 Root 一个 index_jobs，后台串行，无并行无 DAG）；持久化于 settings KV（重启后恢复，旧 `list[str]` 格式自动升级）。Renderer 经 Main 进程的原生对话框（`dialog.showOpenDialog`）/盘符枚举选择位置，不直接访问 fs。

## Settings（M5 §16）

| Method | Path | 说明 |
|---|---|---|
| GET | `/settings` | `{search_mode, w_kw, w_sem, topK, index_roots, models: {bge, caption}, storage: {db_bytes, models_bytes}}` |
| PUT | `/settings` | 部分更新（校验：mode ∈ keyword/semantic/hybrid；w ∈ [0.1,10]；topK ∈ [1,200]） |

## Task Dashboard（M5 §17）

| Method | Path | 说明 |
|---|---|---|
| GET | `/task/status` | `{queue_length, processing, success, failed, total}` |
| GET | `/task/failed` | 最近失败任务明细（id/file_id/filename/attempt/max_attempts/last_error） |
| POST | `/task/{id}/retry` | FAILED→PENDING；attempt ≥ max_attempts → **409 MAX_ATTEMPTS_EXCEEDED** |
| POST | `/task/{id}/reindex` | 新建任务（活跃存在 → `ALREADY_ACTIVE`，partial unique index 兜底） |

## 错误约定

- 401：token 缺失/无效
- 422：请求校验失败
- 502：搜索双通道均失败（`BOTH_CHANNELS_FAILED` 等）
- 409/404：任务重试/重建语义错误
- 其余 5xx：SQLite 不可用等整体失败（不静默返回空结果）
