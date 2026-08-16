# ADR-002：SQLite 作为单机轻量持久化任务队列

- 状态：Accepted
- 日期：2026-08-15

## Context

FastAPI（生产者）与 AI Worker（消费者）之间需要任务传递。备选：进程内队列、Redis/Celery、HTTP 回调、共享 SQLite 表。

## Decision

**共享 SQLite（ai_tasks 表）+ Worker 轮询 claim**，轮询间隔为可调参数 `poll_interval_ms=500`（默认值，非架构约束）。

适用边界（明确声明）：ai_tasks 是「**单机、单 Worker、桌面应用场景下的持久化轻量任务队列**」——它**不能替代通用消息队列**（无多消费者竞争协议、无流式/广播语义）。本项目恰好只需：单生产者 + 单消费者 + 持久化 + 状态可查询，SQLite 全部满足且零额外依赖。

并发与写入竞争对策（明确风险，不回避）：

| 风险点 | 对策 |
|---|---|
| FastAPI 与 Worker 并发写 | WAL：写写串行；`busy_timeout=5000` 等待而非报错 |
| 长事务持锁阻塞对端 | 所有事务**短事务**；推理/OCR/Embedding **一律在事务外执行** |
| claim 竞争 | 单 Worker 无消费者竞争；claim 事务边界：`BEGIN IMMEDIATE → SELECT ... LIMIT 8 → UPDATE ... SET status='RUNNING' → COMMIT`（原子且短） |
| 进程崩溃 | MVP 由 UI 手动重试兜底；P2 启动时恢复 RUNNING 任务 |

## Consequences

- 排除 Redis/Celery（桌面场景引入外部服务，违反「不引入无关复杂度」）
- 排除 HTTP 回调（进程死亡丢回调、环形依赖）
- SQLite 既是队列、也是状态存储、也是 UI 数据源——一处落地，三处消费
- 获得崩溃可恢复性：任务不因进程重启丢失
