-- ============================================================
-- OmniSearch schema v001（architecture.md §7.1 / §8.1）
-- 注意：fts_files 表由 migrate.py 按 sqlite_version 动态创建（ADR-005），
--       不在此文件中。
-- ============================================================

-- 1) 文件主表（元数据仓库的核心）
CREATE TABLE files (
    id             INTEGER PRIMARY KEY,
    path           TEXT    NOT NULL,
    filename       TEXT    NOT NULL,
    dir_path       TEXT    NOT NULL,
    extension      TEXT    NOT NULL DEFAULT '',
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    mtime_ns       INTEGER NOT NULL,
    ctime_ns       INTEGER NOT NULL,
    content_hash   TEXT,
    file_type      TEXT    NOT NULL,
    mime_type      TEXT,
    status         TEXT    NOT NULL DEFAULT 'METADATA_ONLY',
    is_deleted     INTEGER NOT NULL DEFAULT 0,
    scanned_at     INTEGER,
    created_at     INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at     INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE UNIQUE INDEX idx_files_path ON files(path);
CREATE INDEX idx_files_dir     ON files(dir_path);
CREATE INDEX idx_files_type    ON files(file_type, status);
CREATE INDEX idx_files_mtime   ON files(mtime_ns);
CREATE INDEX idx_files_hash    ON files(content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX idx_files_deleted ON files(is_deleted, status);

-- 2) EXIF 元数据（图片专用，1:1）
CREATE TABLE exif (
    file_id            INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    datetime_original  TEXT,
    datetime_digitized TEXT,
    gps_lat            REAL,
    gps_lon            REAL,
    gps_altitude       REAL,
    camera_make        TEXT,
    camera_model       TEXT,
    width              INTEGER,
    height             INTEGER,
    orientation        INTEGER,
    iso                INTEGER,
    exposure_time      TEXT,
    f_number           REAL
);
CREATE INDEX idx_exif_dt  ON exif(datetime_original);
CREATE INDEX idx_exif_gps ON exif(gps_lat, gps_lon);

-- 3) 文本块（FTS5 + Embedding 的统一入口；source_type 创建后不可变）
CREATE TABLE chunks (
    id               INTEGER PRIMARY KEY,
    file_id          INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source_type      TEXT    NOT NULL,
    chunk_index      INTEGER NOT NULL DEFAULT 0,
    chunk_text       TEXT    NOT NULL,
    chunk_text_seg   TEXT    NOT NULL,
    token_count      INTEGER,
    embedding_status INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at       INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(file_id, source_type, chunk_index)
);
CREATE INDEX idx_chunks_file ON chunks(file_id, source_type, chunk_index);
CREATE INDEX idx_chunks_emb  ON chunks(embedding_status, id);

-- 4) OCR 原始结果存档（详情展示/调试/重新处理；与 chunks(ocr) 职责不同）
CREATE TABLE ocr_text (
    file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    lang       TEXT DEFAULT 'zh+en',
    confidence REAL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- 5) 搜索历史
CREATE TABLE search_history (
    id             INTEGER PRIMARY KEY,
    query_text     TEXT NOT NULL,
    parsed_filters TEXT,
    result_count   INTEGER,
    top_results    TEXT,
    latency_ms     INTEGER,
    created_at     INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX idx_history_ts ON search_history(created_at DESC);

-- 6) AI 任务队列（单机单 Worker 轻量持久化队列，一个文件一个任务）
CREATE TABLE ai_tasks (
    id              INTEGER PRIMARY KEY,
    file_id         INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    task_type       TEXT    NOT NULL DEFAULT 'index_file',
    priority        INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'PENDING',
    attempt         INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    next_attempt_at INTEGER,
    last_error      TEXT,
    created_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX idx_tasks_status ON ai_tasks(status, priority);
CREATE INDEX idx_tasks_file  ON ai_tasks(file_id);
-- 数据库级保证：同一 file_id 最多一个活跃任务（防重复入队）
CREATE UNIQUE INDEX idx_tasks_active ON ai_tasks(file_id) WHERE status IN ('PENDING','RUNNING');

-- 7) 索引作业（扫描进度；cursor_path 为 P2 断点续扫预留）
CREATE TABLE index_jobs (
    id            INTEGER PRIMARY KEY,
    root_path     TEXT NOT NULL,
    scan_type     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'RUNNING',
    cursor_path   TEXT,
    total_files   INTEGER DEFAULT 0,
    scanned_files INTEGER DEFAULT 0,
    error_count   INTEGER DEFAULT 0,
    started_at    INTEGER,
    finished_at   INTEGER
);
CREATE INDEX idx_jobs_status ON index_jobs(status);

-- 8) 设置（KV）
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ============================================================
-- FTS5：fts_body（external content → 过滤 VIEW，rebuild 只含 doc_chunk+ocr）
-- ============================================================
CREATE VIEW fts_chunks_source AS
    SELECT id, chunk_text, chunk_text_seg
    FROM chunks
    WHERE source_type IN ('doc_chunk', 'ocr');

CREATE VIRTUAL TABLE fts_body USING fts5(
    chunk_text, chunk_text_seg,
    content='fts_chunks_source', content_rowid='id',
    tokenize='unicode61'
);

-- 触发器：仅 doc_chunk / ocr 进入关键词通道；image_caption 仅走语义通道
CREATE TRIGGER chunks_ai AFTER INSERT ON chunks
WHEN NEW.source_type IN ('doc_chunk','ocr') BEGIN
    INSERT INTO fts_body(rowid, chunk_text, chunk_text_seg)
    VALUES (NEW.id, NEW.chunk_text, NEW.chunk_text_seg);
END;

CREATE TRIGGER chunks_ad AFTER DELETE ON chunks
WHEN OLD.source_type IN ('doc_chunk','ocr') BEGIN
    INSERT INTO fts_body(fts_body, rowid, chunk_text, chunk_text_seg)
    VALUES ('delete', OLD.id, OLD.chunk_text, OLD.chunk_text_seg);
END;

CREATE TRIGGER chunks_au AFTER UPDATE ON chunks
WHEN NEW.source_type IN ('doc_chunk','ocr') BEGIN
    INSERT INTO fts_body(fts_body, rowid, chunk_text, chunk_text_seg)
    VALUES ('delete', OLD.id, OLD.chunk_text, OLD.chunk_text_seg);
    INSERT INTO fts_body(rowid, chunk_text, chunk_text_seg)
    VALUES (NEW.id, NEW.chunk_text, NEW.chunk_text_seg);
END;
