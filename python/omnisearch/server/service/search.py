"""Hybrid SearchService —— M5 双通道融合（architecture.md §12 Hybrid Search）。

链路：Query → QueryParser → UnifiedFilter + semantic_text → FTS5 ∥ Vector（并行、超时）
→ Candidate Union（file_id 去重，保留双通道证据）→ SQLite canonical WHERE（三处一致）
→ RRF（仅 FTS + Vector；k=60，w_kw/w_sem 默认 1.0，Settings 可调）→ Match Reasons → Results。

降级（§12.8）：单通道失败/超时 → degraded_channels 标注，另一通道继续；
两通道均失败（或单通道模式下该通道失败）→ 明确错误；SQLite 错误 → 整体失败。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from omnisearch.common.database import Database
from omnisearch.common.utils.seg import fts_query_forms
from omnisearch.common.utils.time import epoch_to_local_iso
from omnisearch.server.repository.files import FileRepository
from omnisearch.server.repository.fts import FtsRepository
from omnisearch.server.service.filter_builder import FilterBuilderService, UnifiedFilter
from omnisearch.server.service.query_parser import ParsedQuery, QueryParser

logger = logging.getLogger("omnisearch.server.search")

RRF_K = 60
FTS_TIMEOUT_S = 1.0
VECTOR_TIMEOUT_S = 3.0
# FTS 形式降级阈值：候选低于该值 → 尝试下一形式（phrase → AND → OR，§8.3）
FTS_MIN_HITS = 3
# 单个 FTS form 的最小时间预算（秒）：剩余不足则提前终止后续形式（M5 收口 5）
FTS_FORM_MIN_BUDGET_S = 0.2
FTS_TOP = 200  # 单通道候选上限（RRF 前截断，§12.3 top-200）
DEFAULT_WEIGHTS = (1.0, 1.0)  # (w_kw, w_sem)

# 通道失败类型
KW_FAILED = "keyword"
SEM_FAILED = "semantic"


class SearchError(Exception):
    """可映射为 HTTP 错误明确响应的搜索失败（SQLite 可用时两通道均失败）。"""


@dataclass
class ChannelCandidate:
    """单通道候选（每 file 一条）。"""

    score: float            # 通道原始分（bm25 / cosine）
    rank: int               # 通道内 1-based 排名（RRF 用）
    evidence: list[dict] = field(default_factory=list)  # match_reasons 素材


@dataclass
class HybridOutcome:
    parsed: ParsedQuery
    results: list[dict]
    degraded: list[str] = field(default_factory=list)
    # 分项耗时（ms，诊断用；API 仅在请求带 stages=true 时返回，M5 §20 benchmark）
    stages: dict[str, float] | None = None


class SearchService:
    def __init__(
        self,
        db: Database,
        files: FileRepository,
        fts: FtsRepository,
        parser: QueryParser,
        filter_builder: FilterBuilderService,
        semantic=None,
        weights: Callable[[], tuple[float, float]] | None = None,
    ):
        self._db = db
        self._files = files
        self._fts = fts
        self._parser = parser
        self._filters = filter_builder
        self._semantic = semantic  # SemanticSearchService | None（BGE/Qdrant 缺失 → 语义通道不可用）
        self._weights = weights or (lambda: DEFAULT_WEIGHTS)

    # ================= 主入口 =================

    def search(self, query: str, top_k: int = 50, mode: str = "hybrid") -> HybridOutcome:
        """Hybrid Search（mode: hybrid | keyword | semantic；缺省 hybrid）。

        stages 分项耗时（ms）：parser / fts / semantic（含 embedding）/ finalize / total。
        """
        started = time.perf_counter()
        t_parser = time.perf_counter()
        parsed = self._parser.parse(query)
        parser_ms = (time.perf_counter() - t_parser) * 1000
        unified = UnifiedFilter(
            time_range=parsed.time_range,
            file_types=parsed.file_types,
            extensions=parsed.extensions,
        )
        where, params = self._filters.build(unified)
        degraded: list[str] = []
        # 语义通道不可用（BGE/Qdrant 缺失，启动时降级）→ 如实标注（§12.8）
        if (
            mode in ("hybrid", "semantic")
            and parsed.semantic_text.strip()
            and self._semantic is None
        ):
            degraded.append(SEM_FAILED)
        kw_candidates: dict[int, ChannelCandidate] = {}
        sem_candidates: dict[int, ChannelCandidate] = {}

        stages: dict[str, float] = {}
        # 注意：不用 `with ThreadPoolExecutor` —— 其 __exit__ 执行 shutdown(wait=True)，
        # 即使 future 已超时降级，仍会阻塞等待后台任务完成（timeout 失效的根源）。
        # 必须 shutdown(wait=False, cancel_futures=True)：超时即放弃，请求按时返回。
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futures = {}
            # semantic_text 为空 → 只走 FTS（§12.8，非降级；semantic 模式同样回退）
            use_kw = mode in ("hybrid", "keyword") or not parsed.semantic_text.strip()
            if use_kw:
                futures["kw"] = pool.submit(self._kw_timed, parsed.semantic_text, where, params)
            if mode in ("hybrid", "semantic") and self._semantic is not None and parsed.semantic_text.strip():
                futures["sem"] = pool.submit(self._sem_timed, parsed.semantic_text, where, params, top_k)

            if "kw" in futures:
                kw_candidates, kw_ms, kw_ok = self._await(futures["kw"], FTS_TIMEOUT_S, KW_FAILED, sqlite_fatal=True)
                stages["fts"] = kw_ms
                if not kw_ok:
                    degraded.append(KW_FAILED)
            if "sem" in futures:
                sem_candidates, sem_ms, sem_ok = self._await(futures["sem"], VECTOR_TIMEOUT_S, SEM_FAILED, sqlite_fatal=True)
                stages["semantic"] = sem_ms
                if not sem_ok:
                    degraded.append(SEM_FAILED)
        finally:
            # 超时后立即取消未开始任务；运行中的任务自然结束（线程非泄漏，executor 关闭后退出）
            pool.shutdown(wait=False, cancel_futures=True)

        # Metadata-only fallback：semantic_text 为空 + 至少一个有效 Metadata Filter（M5 收口 3）
        if (
            not kw_candidates
            and not sem_candidates
            and not parsed.semantic_text.strip()
            and bool(unified.time_range or unified.file_types or unified.extensions)
        ):
            t_meta = time.perf_counter()
            results = self._metadata_only(unified, where, params, top_k)
            stages["finalize"] = (time.perf_counter() - t_meta) * 1000
            stages["total"] = (time.perf_counter() - started) * 1000
            return HybridOutcome(parsed=parsed, results=results, degraded=[], stages=stages)

        # 两通道均失败 → 明确错误（SQLite 可用时不得静默返回空，§12.8）
        if KW_FAILED in degraded and SEM_FAILED in degraded:
            raise SearchError("BOTH_CHANNELS_FAILED")
        if mode == "keyword" and KW_FAILED in degraded:
            raise SearchError("KEYWORD_CHANNEL_FAILED")
        if mode == "semantic" and SEM_FAILED in degraded:
            raise SearchError("SEMANTIC_CHANNEL_FAILED")

        # RRF 融合（仅 FTS + Vector；Metadata 不参与，§12.4）
        t_final = time.perf_counter()
        w_kw, w_sem = self._weights()
        ranked = self._rrf(kw_candidates, sem_candidates, w_kw, w_sem)
        results = self._finalize(ranked, top_k, kw_candidates, sem_candidates, unified, where, params)
        total_ms = (time.perf_counter() - started) * 1000
        stages.update({"parser": parser_ms, "finalize": (time.perf_counter() - t_final) * 1000, "total": total_ms})
        logger.debug(
            "hybrid %r mode=%s → %d results degraded=%s (%.1fms, stages=%s)",
            query, mode, len(results), degraded, total_ms, stages,
        )
        return HybridOutcome(parsed=parsed, results=results, degraded=degraded, stages=stages)

    # ================= 双通道 =================

    def _keyword_channel(
        self, fts_query: str, where: str, params: list, deadline: float | None = None
    ) -> dict[int, ChannelCandidate]:
        """FTS 通道：phrase → AND → OR 逐级降级（召回 < FTS_MIN_HITS 进下一形式，§8.3）。

        deadline：请求级整体时限（monotonic 秒）——剩余时间不足单个 form 预算时
        提前终止后续形式，避免最多 3 次 MATCH 突破 FTS 1s deadline（M5 收口 5）。
        """
        forms = fts_query_forms(fts_query)
        if not forms:
            return {}
        merged: dict[int, dict] = {}
        for form in forms:
            if deadline is not None and time.monotonic() + FTS_FORM_MIN_BUDGET_S > deadline:
                logger.debug("fts deadline reached, skipping form %r", form)
                break
            merged = self._fts_matches(form, fts_query, where, params)
            if len(merged) >= FTS_MIN_HITS or form is forms[-1]:
                break
        ordered = sorted(merged.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
        return {
            fid: ChannelCandidate(score=c["score"], rank=i + 1, evidence=c["evidence"])
            for i, (fid, c) in enumerate(ordered[:FTS_TOP])
        }

    def _fts_matches(self, form: str, raw_query: str, where: str, params: list) -> dict[int, dict]:
        """文件名 + 正文/OCR 候选 → canonical WHERE（is_deleted/类型/扩展名/时间，三处一致）→ 合并。"""
        merged: dict[int, dict] = {}
        kw_params = list(params)
        with self._db.connect() as c:
            # 通道 1：文件名（fts_files，rowid=files.id）→ 批量回表应用 canonical WHERE
            hits = self._fts.match(form, top_k=FTS_TOP)
            fids = [fid for fid, _ in hits]
            keep: set[int] = set()
            if fids:
                keep = {
                    r["id"]
                    for r in c.execute(
                        f"""SELECT f.id FROM files f LEFT JOIN exif e ON e.file_id = f.id
                            WHERE f.id IN ({','.join('?' * len(fids))}) AND {where}""",
                        (*fids, *kw_params),
                    ).fetchall()
                }
            for fid, s in hits:
                if fid in keep:
                    merged[fid] = {"score": s, "evidence": [{"kind": "filename", "score": s}]}
            # 通道 2：正文/OCR（fts_body join chunks 拿 file_id + source_type）
            for chunk_id, fid, score, source_type in self._fts.body_match(form, top_k=FTS_TOP):
                ok = c.execute(
                    f"""SELECT 1 FROM files f LEFT JOIN exif e ON e.file_id = f.id
                        WHERE f.id = ? AND {where}""",
                    (fid, *kw_params),
                ).fetchone()
                if not ok:
                    continue
                hit = merged.setdefault(fid, {"score": 0.0, "evidence": []})
                if score > hit["score"]:
                    hit["score"] = score
                hit["evidence"].append({"kind": "body" if source_type == "doc_chunk" else "ocr",
                                        "chunk_id": chunk_id, "score": score})
        return merged

    def _semantic_channel(self, semantic_text: str, where: str, params: list, top_k: int) -> dict[int, ChannelCandidate]:
        """Vector 通道：BGE embed → Qdrant topK → 三元组校验 → canonical → file_id 去重。

        H2 修正：候选截断固定 FTS_TOP（与 FTS 通道对称，§12.3 top-200）——
        原实现绑定用户 topK（默认 50）导致双通道候选池不对称、结果随 topK 变化。
        """
        assert self._semantic is not None
        hits = self._semantic.search(semantic_text, top_k=max(top_k, FTS_TOP), where=where, params=params)
        return {
            h["file_id"]: ChannelCandidate(
                score=h["semantic_score"], rank=i + 1,
                evidence=[{"kind": "semantic", "source_type": h["source_type"], "text": h["text"],
                           "score": h["semantic_score"]}],
            )
            for i, h in enumerate(sorted(hits, key=lambda x: -x["semantic_score"]))
        }

    def _kw_timed(self, fts_query: str, where: str, params: list) -> tuple[dict, float]:
        t = time.perf_counter()
        deadline = t + FTS_TIMEOUT_S  # 请求级整体 deadline（M5 收口 5）
        return self._keyword_channel(fts_query, where, params, deadline=deadline), (time.perf_counter() - t) * 1000

    def _sem_timed(self, semantic_text: str, where: str, params: list, top_k: int) -> tuple[dict, float]:
        t = time.perf_counter()
        return self._semantic_channel(semantic_text, where, params, top_k), (time.perf_counter() - t) * 1000

    @staticmethod
    def _await(future, timeout: float, channel: str, sqlite_fatal: bool) -> tuple[dict, float, bool]:
        """等待通道结果；(result, ms, ok)。超时/失败 → 降级语义；SQLite 错误 → 整体失败。"""
        try:
            result, ms = future.result(timeout=timeout)
            return result, ms, True
        except TimeoutError:
            logger.warning("channel %s timed out after %.1fs", channel, timeout)
            return {}, 0.0, False
        except sqlite3.Error as e:
            if sqlite_fatal:
                raise
            logger.warning("channel %s sqlite error: %s", channel, e)
            return {}, 0.0, False
        except Exception as e:  # noqa: BLE001 —— 通道级异常（Qdrant/BGE/推理）→ 降级
            logger.warning("channel %s failed: %s", channel, e, exc_info=True)
            return {}, 0.0, False

    # ================= Metadata-only（M5 收口 3：semantic_text 空 + 有 filter） =================

    def _metadata_only(self, unified: UnifiedFilter, where: str, params: list, top_k: int) -> list[dict]:
        """纯过滤查询（如「昨天的照片」「pdf」）：files canonical WHERE → 时间 basis 倒序 → LIMIT。

        不是第三个 RRF channel——keyword/semantic/rrf_score 全为 null，仅 metadata 原因。
        排序：有时间过滤 → 选定 basis 时间倒序；无时间过滤 → mtime desc（M5 收口 3）。
        """
        tr = unified.time_range
        if tr is None:
            order = "f.mtime_ns DESC"
        elif tr.basis_hint == "ctime":
            order = "f.ctime_ns DESC"
        elif tr.basis_hint == "mtime":
            order = "f.mtime_ns DESC"
        else:  # exif：EXIF epoch（无则 mtime）倒序
            order = "COALESCE(e.datetime_original_epoch, f.mtime_ns / 1000000000) DESC"
        with self._db.connect() as c:
            rows = c.execute(
                f"""SELECT f.*, e.datetime_original_epoch FROM files f
                    LEFT JOIN exif e ON e.file_id = f.id
                    WHERE {where} ORDER BY {order} LIMIT ?""",
                (*params, top_k),
            ).fetchall()

        results: list[dict] = []
        for row in rows:
            time_info = self._time_info(row, unified)
            if time_info["basis"] is None:
                # 无时间过滤：仍给出文件级时间原因（mtime fallback，每个文件都有）
                time_info = {"basis": "mtime", "confidence": "fallback",
                             "value": epoch_to_local_iso(row["mtime_ns"] // 1_000_000_000)}
            results.append(
                {
                    "file_id": row["id"],
                    "path": row["path"],
                    "filename": row["filename"],
                    "dir_path": row["dir_path"],
                    "extension": row["extension"],
                    "file_type": row["file_type"],
                    "size_bytes": row["size_bytes"],
                    "mtime_ns": row["mtime_ns"],
                    "rrf_score": None,
                    "keyword_score": None,
                    "semantic_score": None,
                    "time_info": time_info,
                    # 无时间过滤时也给出文件级 metadata 原因（如「修改于 …」）
                    "match_reasons": self._match_reasons(None, None, {}, time_info),
                }
            )
        return results

    # ================= RRF + 结果 =================

    @staticmethod
    def _rrf(kw: dict[int, ChannelCandidate], sem: dict[int, ChannelCandidate], w_kw: float, w_sem: float) -> list[tuple[int, float]]:
        """加权 RRF：rrf = w_kw/(k+rank_kw) + w_sem/(k+rank_sem)；仅两通道（§12.4）。"""
        k = RRF_K
        scores: dict[int, float] = {}
        for fid, c in kw.items():
            scores[fid] = w_kw / (k + c.rank)
        for fid, c in sem.items():
            scores[fid] = scores.get(fid, 0.0) + w_sem / (k + c.rank)
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    def _finalize(
        self,
        ranked: list[tuple[int, float]],
        top_k: int,
        kw: dict[int, ChannelCandidate],
        sem: dict[int, ChannelCandidate],
        unified: UnifiedFilter,
        where: str = "f.is_deleted = 0",
        params: list | None = None,
    ) -> list[dict]:
        top = ranked[:top_k]
        if not top:
            return []
        fids = [fid for fid, _ in top]
        with self._db.connect() as c:
            # H3 修正：回查同样应用 canonical WHERE（三处一致——通道过滤与 finalize 回查
            # 之间的删除竞态不得让 is_deleted=1 的文件进入本次结果）
            rows = {
                r["id"]: r
                for r in c.execute(
                    f"""SELECT f.*, e.datetime_original_epoch FROM files f
                        LEFT JOIN exif e ON e.file_id = f.id
                        WHERE f.id IN ({','.join('?' * len(fids))}) AND {where}""",
                    (*fids, *(params or [])),
                ).fetchall()
            }
            # 正文/OCR 证据片段（仅最终结果，一次批量取）
            chunk_ids = [
                e["chunk_id"]
                for c in kw.values()
                for e in c.evidence
                if e.get("chunk_id") is not None
            ]
            snippets: dict[int, str] = {}
            if chunk_ids:
                chunk_ids = list(dict.fromkeys(chunk_ids))
                snippets = {
                    r["id"]: r["chunk_text"]
                    for r in c.execute(
                        f"SELECT id, chunk_text FROM chunks WHERE id IN ({','.join('?' * len(chunk_ids))})",
                        chunk_ids,
                    ).fetchall()
                }

        results: list[dict] = []
        for fid, rrf_score in top:
            row = rows.get(fid)
            if row is None:
                continue  # canonical 后仍被并发删除（防御）
            kc, sc = kw.get(fid), sem.get(fid)
            time_info = self._time_info(row, unified)
            reasons = self._match_reasons(kc, sc, snippets, time_info)
            results.append(
                {
                    "file_id": fid,
                    "path": row["path"],
                    "filename": row["filename"],
                    "dir_path": row["dir_path"],
                    "extension": row["extension"],
                    "file_type": row["file_type"],
                    "size_bytes": row["size_bytes"],
                    "mtime_ns": row["mtime_ns"],
                    "rrf_score": rrf_score,
                    "keyword_score": kc.score if kc else None,
                    "semantic_score": sc.score if sc else None,
                    "time_info": time_info,
                    "match_reasons": reasons,
                }
            )
        return results

    @staticmethod
    def _match_reasons(kc, sc, snippets: dict[int, str], time_info: dict) -> list[dict]:
        """匹配原因（§12.6）：keyword/body/ocr/semantic/metadata（时间）。"""
        reasons: list[dict] = []
        if kc:
            for e in kc.evidence:
                if e["kind"] == "filename":
                    reasons.append({"channel": "keyword", "text": "文件名匹配", "score": e["score"]})
                elif e["kind"] == "body":
                    text = snippets.get(e["chunk_id"], "")
                    reasons.append({"channel": "body", "text": f"正文命中：{text[:80]}", "score": e["score"]})
                elif e["kind"] == "ocr":
                    text = snippets.get(e["chunk_id"], "")
                    reasons.append({"channel": "ocr", "text": f"识别到文字：{text[:80]}", "score": e["score"]})
        if sc:
            for e in sc.evidence:
                prefix = "AI 描述：" if e.get("source_type") == "image_caption" else "语义命中："
                reasons.append({"channel": "semantic", "text": f"{prefix}{e['text'][:80]}", "score": e["score"]})
        # 时间元数据（§12.6/§12.7）：basis=exif → exact；mtime/ctime → fallback
        if time_info.get("basis"):
            verb = {"exif": "拍摄于", "mtime": "修改于", "ctime": "创建于"}.get(time_info["basis"], "")
            reasons.append(
                {
                    "channel": "metadata",
                    "text": f"{verb} {time_info['value']}",
                    "basis": time_info["basis"],
                    "confidence": time_info["confidence"],
                }
            )
        return reasons

    @staticmethod
    def _time_info(row, unified: UnifiedFilter) -> dict:
        """时间可信度（§12.7）：exif=exact；mtime/ctime=fallback；无任何时间 → unknown。"""
        tr = unified.time_range
        if tr is None:
            return {"basis": None, "confidence": None, "value": None}
        if tr.basis_hint == "ctime":
            return {"basis": "ctime", "confidence": "fallback", "value": epoch_to_local_iso(row["ctime_ns"] // 1_000_000_000)}
        if tr.basis_hint == "mtime":
            return {"basis": "mtime", "confidence": "fallback", "value": epoch_to_local_iso(row["mtime_ns"] // 1_000_000_000)}
        epoch = row["datetime_original_epoch"]
        if epoch is not None:
            return {"basis": "exif", "confidence": "exact", "value": epoch_to_local_iso(epoch)}
        return {"basis": "mtime", "confidence": "fallback", "value": epoch_to_local_iso(row["mtime_ns"] // 1_000_000_000)}
