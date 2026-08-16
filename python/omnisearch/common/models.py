"""领域枚举与状态常量（architecture.md §7.1 / §10.3）。

枚举值必须与数据库存储、API 响应完全一致——三处共用，禁止各自造字面量。
"""
from __future__ import annotations

from enum import IntEnum, StrEnum


class FileStatus(StrEnum):
    """files.status —— 文件级 AI pipeline 状态。"""

    METADATA_ONLY = "METADATA_ONLY"
    PROCESSING = "PROCESSING"
    AI_DONE = "AI_DONE"
    FAILED = "FAILED"


class FileType(StrEnum):
    """files.file_type。"""

    IMAGE = "image"
    DOC = "doc"
    VIDEO = "video"
    AUDIO = "audio"
    ARCHIVE = "archive"
    OTHER = "other"


class SourceType(StrEnum):
    """chunks.source_type（创建后不可变；与 Qdrant payload 一致，architecture.md §7.1/§9.2）。"""

    DOC_CHUNK = "doc_chunk"
    OCR = "ocr"
    IMAGE_CAPTION = "image_caption"


class EmbeddingStatus(IntEnum):
    """chunks.embedding_status —— 单个 chunk 的向量索引状态。

    embedding 失败不影响该 chunk 的 FTS 可搜索性（architecture.md §7.1）。
    """

    PENDING = 0
    SUCCESS = 1
    FAILED = 2


class TaskType(StrEnum):
    """ai_tasks.task_type —— MVP 只有 index_file 一种（无 DAG，architecture.md §10.3）。"""

    INDEX_FILE = "index_file"


class TaskStatus(StrEnum):
    """ai_tasks.status —— 4 态状态机。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ScanType(StrEnum):
    """index_jobs.scan_type。"""

    FULL = "full"
    INCREMENTAL = "incremental"


class JobStatus(StrEnum):
    """index_jobs.status。"""

    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
