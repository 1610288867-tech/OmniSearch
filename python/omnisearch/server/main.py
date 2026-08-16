"""FastAPI 应用工厂（architecture.md §6）。

- lifespan：执行 migration + 组装 Service/Repository（Composition Root，DI 经 configure_* 注入）
- 安全基线（architecture.md §12）：本机 token 鉴权（X-Omni-Token），/health 放行（Main 探测用）
- token 由 Electron Main / dev.py 生成并注入（OMNISEARCH_TOKEN）；未注入时开发模式放行
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnisearch.common.config import APP_NAME, db_path, dev_data_dir, log_dir
from omnisearch.common.database import Database
from omnisearch.common.logging_setup import setup_logging
from omnisearch.server.api.health import configure_health, configure_semantic_ready, router as health_router
from omnisearch.server.api.index import configure_index, router as index_router
from omnisearch.server.api.search import configure_search, router as search_router
from omnisearch.server.api.semantic import configure_semantic, router as semantic_router
from omnisearch.server.api.settings import configure_settings, router as settings_router
from omnisearch.server.api.tasks import configure_tasks, router as tasks_router
from omnisearch.server.database.migrations.migrate import migrate
from omnisearch.server.repository.files import FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.repository.jobs import IndexJobRepository
from omnisearch.server.repository.settings import SettingsRepository
from omnisearch.server.repository.tasks import TaskRepository
from omnisearch.server.service.filter_builder import FilterBuilderService
from omnisearch.server.service.index import IndexService
from omnisearch.server.service.query_parser import QueryParser
from omnisearch.server.service.search import SearchService
from omnisearch.server.service.settings import SettingsService
from omnisearch.server.service.time_range import TimeRangeService
from omnisearch.server.service.watch import WatchService

VERSION = "0.1.0"
logger = logging.getLogger("omnisearch.server")


def create_app() -> FastAPI:
    data_dir = dev_data_dir()
    logger = setup_logging("omnisearch.server", log_dir(data_dir))
    db = Database(db_path(data_dir))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        version = migrate(db)
        logger.info("schema migrated to v%s (%s)", version, db.path)

        # Composition Root：实例化 Repository + Service，注入 Router 层（architecture.md §6.1）
        files_repo = FileRepository(db)
        fts_repo = FtsRepository(db)
        jobs_repo = IndexJobRepository(db)
        settings_repo = SettingsRepository(db)

        configure_health(db, VERSION)
        tasks_repo = TaskRepository(db)
        index_service = IndexService(db, files_repo, fts_repo, jobs_repo, tasks_repo)

        # 实时文件监听（architecture.md §11.4）：事件线程只入缓冲，防抖后批量处理
        watch = WatchService(
            on_changes=index_service.handle_changes,
            on_deleted_paths=lambda paths: [index_service.handle_delete_path(p) for p in paths],
            on_renamed=index_service.handle_rename,
        )
        configure_index(index_service, jobs_repo, settings_repo, watch, db)

        # M5：时间/解析/过滤（架构 §8/§12.2 单一实现）
        time_ranges = TimeRangeService()
        parser = QueryParser(time_ranges)
        filter_builder = FilterBuilderService()

        # M4/M5 语义通道：BGE（查询侧）+ Qdrant。
        # Model warmup（§15）：ready 前预加载 BGE（首次用户搜索不承担模型加载成本）；
        # 预加载失败不阻塞健康检查 → 语义通道降级（FTS 不受影响）。
        # M5 收口 4：readiness 显式上报（health.components.semantic）。
        semantic_svc = None
        semantic_ready = False
        try:
            from omnisearch.common.config import qdrant_url
            from omnisearch.common.embedding import BGEEmbeddingProvider
            from omnisearch.common.utils.models import models_dir
            from omnisearch.common.vector import VectorStore
            from omnisearch.server.service.semantic_search import SemanticSearchService

            embedder = BGEEmbeddingProvider(models_dir(data_dir))
            vector_store = VectorStore(qdrant_url(), embedder.dim)
            vector_store.ensure_collection()
            embedder.embed_query("")  # warmup：ONNX 会话就绪（避免首次搜索承担加载成本）
            semantic_svc = SemanticSearchService(db, embedder, vector_store)
            configure_semantic(semantic_svc)
            semantic_ready = True
            logger.info("semantic channel ready (dim=%d, warmup ok)", embedder.dim)
        except Exception:  # noqa: BLE001 —— 模型/Qdrant 缺失：语义通道降级（健康检查不受影响）
            logger.warning("semantic channel unavailable (FTS-only)", exc_info=True)
        configure_semantic_ready(semantic_ready)

        weights = lambda: (  # noqa: E731 —— Settings 权重（w_kw/w_sem，§12.4）
            float(settings_repo.get("w_kw", 1.0)),
            float(settings_repo.get("w_sem", 1.0)),
        )
        configure_search(SearchService(db, files_repo, fts_repo, parser, filter_builder, semantic_svc, weights))
        configure_tasks(tasks_repo)
        configure_settings(SettingsService(db, settings_repo, models_dir(data_dir)))

        # 重启场景恢复监听（roots 为 dict 结构 [{path, enabled, created_at}]）；首次扫描经 add_roots 动态启动
        watch.start([r["path"] for r in settings_repo.get_index_roots() if r.get("enabled", True)])
        yield
        watch.stop()
        logger.info("server shutting down: WAL checkpoint")
        db.checkpoint()

    app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)

    # 本机 token 鉴权（architecture.md §12）；/health 放行
    token = os.environ.get("OMNISEARCH_TOKEN", "")

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path == "/health" or not token:
            return await call_next(request)
        if request.headers.get("X-Omni-Token") != token:
            return JSONResponse(status_code=401, content={"error": {"code": "UNAUTHORIZED", "message": "invalid token"}})
        return await call_next(request)

    app.include_router(health_router)
    app.include_router(search_router)
    app.include_router(semantic_router)
    app.include_router(index_router)
    app.include_router(settings_router)
    app.include_router(tasks_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("OMNISEARCH_FASTAPI_PORT", "8734")))
