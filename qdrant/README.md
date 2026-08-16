# Qdrant Sidecar（architecture.md §9）

Qdrant 以本地 Sidecar 进程运行（非 Docker）：Electron Main spawn/监控/退出，
FastAPI 经 localhost REST 通信；存储目录在用户数据目录 `qdrant/`（开发态为 `dev-data/qdrant/`）。

**Qdrant 是 MVP 语义通道的必需组件**（BGE 向量检索，单 collection `omnisearch`）；
它只是**可重建索引**——事实数据源始终是 SQLite（chunks 表），Qdrant 丢失后由 Worker 重新嵌入重建。

## 二进制放置

将 qdrant 可执行文件放入本目录，二选一：

1. 下载官方 Windows 二进制（https://qdrant.tech/install/ 或 GitHub releases），
   解压后把 `qdrant.exe` 放到 `qdrant/bin/qdrant.exe`（`bin/` 已被 .gitignore 排除，不入库）；
2. 或设置环境变量 `OMNISEARCH_QDRANT_BIN` 指向已有 qdrant 可执行文件。

未放置时：dev.py / Electron ProcessManager 跳过 Qdrant 并告警，
`/health` 的 `components.qdrant.ok=false`、`components.semantic.ok=false`——
关键词搜索照常工作，语义搜索自动降级（degraded_channels 如实标注）。

## 端口

HTTP/gRPC 成对顺延：6333/6334 → 6335/6336 → …；由 Electron Main 探测并注入 FastAPI 配置。
测试环境使用 6335/6336（见 `python/tests/conftest.py`）。
