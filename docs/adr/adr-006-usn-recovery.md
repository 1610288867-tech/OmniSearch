# ADR-006：USN Journal 启动恢复 + 断点续扫（P2.1）

- 状态：Accepted（P2.1）
- 日期：2026-08-16

## Context

MVP 限制：应用关闭期间的文件变化只能在下次启动时通过 mtime_ns+size 对比发现（且当时未自动执行，
依赖用户手动扫描）。P2.1 引入 Windows NTFS USN Journal 用于快速启动恢复，并补上断点续扫。

## 决策

### 1. 为什么使用 USN

NTFS USN Journal 是卷级变化日志（CREATE/MODIFY/DELETE/RENAME 均记录，含 USN 序号与时间戳），
启动时从上次 cursor 增量读取即可恢复关闭期变化，无需遍历全目录——大目录下启动恢复远快于完整 DFS 重扫。

### 2. USN 是加速机制，不是正确性来源

- 非 NTFS 卷 / Journal 不存在 / 权限不足（普通用户打开卷句柄需管理员）/ Journal 回绕
  → **自动降级为启动增量扫描**（mtime+size 对比，复用 IndexService 扫描，后台执行，不阻塞应用启动）
- 正确性兜底始终是 SQLite 事实数据源 + 现有搜索通道

### 3. Cursor 语义（settings KV，不新增表）

- `usn_cursor` = `{volume: {journal_id, usn, ts}}`（settings KV，架构 §7.1 已允许）
- 更新时机：**事件全部成功应用后**才推进 cursor；处理中途崩溃 → cursor 停留在旧值 →
  重启重放（upsert / ai_tasks partial unique / FTS replace 均幂等，不产生错误状态）
- 回绕判定：journal_id 变化 或 cursor.usn < LowestValidUsn → 弃用旧 cursor + 降级增量扫描 + 重置 cursor
- 首次启用（无 cursor）→ 跳过 journal 历史，cursor = NextUsn（无先前关闭期基线）

### 4. 事件语义（USN → IndexService，不复制索引逻辑）

| USN Reason | IndexService 入口 |
|---|---|
| FILE_CREATE / DATA_EXTEND / DATA_OVERWRITE | `handle_changes`（存在→upsert，不存在→delete） |
| FILE_DELETE | `handle_delete_path`（is_deleted=1 先行） |
| RENAME_OLD_NAME + RENAME_NEW_NAME（同 file_ref 配对） | `handle_rename`（保留 file_id，MVP rename 规则） |
| 只有 OLD / 只有 NEW | 旧路径 delete / 新路径 upsert |

路径解析：USN 记录只有「文件名最后一段 + 父目录 FRN」→ `FSCTL_GET_NTFS_FILE_RECORD` 读取
MFT 记录解析 `$FILE_NAME` 属性递归得到完整路径（父 FRN→路径缓存）。解析失败（记录已释放等）→
丢弃该事件并 warning；delete 事件解析失败由 fallback 增量扫描的 sync_deleted 兜底。

### 5. 实现细节

- `UsnReader`：纯 ctypes + Win32 API（CreateFileW / DeviceIoControl / GetVolumeInformationW），
  零新依赖；64 位句柄显式声明；卷设备路径 `\\.\D:`
- 卷级共享：同卷多 Root 共用一个 journal reader，事件按 `root_covers(path, active_root)` 过滤
  （enabled/removed root 事件丢弃）
- 断点续扫：`index_jobs.cursor_path`（架构已预留字段）记录 DFS 扫描栈顶（每 1000 目录），
  中断后从该目录继续；与 USN cursor（settings KV）**语义分离，不混用**
- 启动集成：lifespan 内同步跑 USN 恢复（只读变化批次，快）；fallback 增量扫描后台执行

## 后果

- 正面：关闭期变化恢复从「手动全量扫描」变为「启动自动恢复」；大目录启动显著加速
- 限制：真实 USN 读取需管理员权限（普通用户自动降级增量扫描——正确性不受影响）；
  USN 记录不含完整路径，MFT 解析依赖记录未被释放
- 不做：不做复杂去重（幂等兜底）；不引入新队列/表/基础设施
