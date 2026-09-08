"""FAISS 索引管理和 RAG 查询主流程。"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from provider.embedding import EmbeddingResult, embed
from provider.rerank import clear_cache, rerank

from .chunker import chunking_signature
from .config import AppConfig, load_config
from .db import (
    connect_graph,
    connect_rag,
    connect_sources,
    initialize_databases,
    read_rag_meta,
    write_rag_meta,
)
from .logger import DailyTSVLogger
from .faiss_index import FaissIndexManager, _as_float32_matrix
from .rag_errors import FaissUnavailableError, IndexIntegrityError, RAGError, RAGQueryError
from .rag_auxiliary import RAGAuxiliaryMixin
from .rag_support import (
    PreparedQuery,
    _Candidate,
    _ChunkContext,
    _DIRECT_LEXICAL_MATCH_SCORE,
    _LEXICAL_CANDIDATE_SCORE,
    _LEXICAL_EDGE_PUNCTUATION,
    _MIN_LEXICAL_QUERY_LENGTH,
    _MixedVectorSpaceError,
    _auxiliary_row_is_fresh,
    _candidate_context_content,
    _chunk_family_id,
    _collapse_candidates_by_family,
    _coerce_embedding_result,
    _direct_lexical_match_score,
    _elapsed_ms,
    _fuse_vector_hits,
    _merge_candidates,
    _normalize_lexical_text,
    _planned_lexical_match_score,
    _rag_result_item,
    _validate_prepared_query,
    _validate_score_multipliers,
    _validate_vector_space_id,
)
from .query_planner import QueryPlan, filter_semantic_drift, plan_query




Embedder = Callable[[list[str]], EmbeddingResult | list[list[float]]]
Reranker = Callable[[str, list[str], int], list[tuple[int, float]]]

class RAGEngine(RAGAuxiliaryMixin):
    """串联 query chunk、embedding、FAISS、rerank 和阈值过滤。"""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        *,
        settings: AppConfig | None = None,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.settings = settings or load_config()
        self.paths = initialize_databases(data_dir, self.settings)
        self.index = FaissIndexManager(
            self.paths.faiss_index,
            self.settings.models.embedding_dimensions,
            autoload=False,
        )
        self.entity_index = FaissIndexManager(
            self.paths.entity_faiss_index,
            self.settings.models.embedding_dimensions,
            autoload=False,
        )
        self.community_index = FaissIndexManager(
            self.paths.community_faiss_index,
            self.settings.models.embedding_dimensions,
            autoload=False,
        )
        self._embedder = embedder
        self._reranker = reranker
        self._logger = DailyTSVLogger(
            self.settings.resolve_log_dir(),
            self.settings.log_level,
        )
        self._prune_stale_auxiliary_embeddings()
        self._ensure_index_consistency()
        self._ensure_entity_index_consistency()
        self._ensure_community_index_consistency()

    def query(
        self,
        query: str,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
        score_multipliers: Mapping[str, float] | None = None,
        prepared_query: PreparedQuery | None = None,
    ) -> dict[str, Any]:
        """执行完整 RAG 查询并返回 API 约定的结构化结果。"""

        if not isinstance(query, str) or not query.strip():
            raise RAGQueryError("query 必须是非空字符串")
        effective_top_k = top_k if top_k is not None else self.settings.default_top_k
        effective_threshold = (
            threshold
            if threshold is not None
            else self.settings.rag_similarity_threshold
        )
        if not isinstance(effective_top_k, int) or isinstance(effective_top_k, bool):
            raise RAGQueryError("top_k 必须是整数")
        if effective_top_k < 1:
            raise RAGQueryError("top_k 必须大于等于 1")
        if not isinstance(effective_threshold, (int, float)) or isinstance(
            effective_threshold, bool
        ):
            raise RAGQueryError("threshold 必须是数字")
        if not 0.0 <= float(effective_threshold) <= 1.0:
            raise RAGQueryError("threshold 必须在 0 到 1 之间")
        if self.index.count == 0:
            return {"query": query, "results": []}

        prepared = prepared_query or self.prepare_query(query)
        _validate_prepared_query(query, prepared)
        query_embedding = prepared.embedding
        query_vectors = query_embedding.vectors
        database_vector_space = self._database_vector_space_id()
        if (
            database_vector_space is not None
            and query_embedding.vector_space_id != database_vector_space
        ):
            raise RAGQueryError(
                "查询向量空间与 FAISS 索引不一致："
                f"查询={query_embedding.vector_space_id}，索引={database_vector_space}"
            )
        candidate_pool_limit = max(
            effective_top_k,
            self.settings.query_planning.candidate_pool_size,
        )
        search_limit = max(effective_top_k * 3, candidate_pool_limit)
        raw_hits = self.index.search(
            query_vectors,
            search_limit,
        )
        candidates = self._load_candidates(
            raw_hits,
            query_weights=prepared.plan.weights,
            rrf_k=self.settings.query_planning.rrf_k,
        )
        # FAISS is excellent for semantic similarity, but a short exact term
        # can receive a poor embedding score (especially for mixed Chinese /
        # Latin text, identifiers, and newly indexed chunks).  Add a bounded
        # lexical candidate pass before hierarchy collapse so an exact hit is
        # not lost merely because it fell outside the ANN top-k window.
        lexical_candidates = self._load_lexical_candidates(
            prepared.plan,
            limit=search_limit,
        )
        if lexical_candidates:
            candidates = _merge_candidates(candidates, lexical_candidates)
        if not candidates:
            return {"query": query, "results": []}
        if score_multipliers:
            _validate_score_multipliers(score_multipliers)
            candidates = sorted(
                (
                    replace(
                        candidate,
                        faiss_score=candidate.faiss_score
                        * float(score_multipliers.get(candidate.chunk_id, 1.0)),
                    )
                    for candidate in candidates
                ),
                key=lambda candidate: candidate.faiss_score,
                reverse=True,
            )

        hierarchy = self._load_chunk_hierarchy(candidates)
        candidates = _collapse_candidates_by_family(
            candidates,
            hierarchy,
            candidate_pool_limit,
        )
        if not candidates:
            return {"query": query, "results": []}

        requested_rerank_limit = max(effective_top_k, self.settings.rerank_top_n)
        if len(prepared.plan.variants) > 1:
            requested_rerank_limit = max(
                requested_rerank_limit,
                self.settings.query_planning.candidate_pool_size,
            )
        rerank_limit = min(len(candidates), requested_rerank_limit)
        documents = [
            _candidate_context_content(candidate, hierarchy)
            for candidate in candidates
        ]
        rerank_started_at = time.perf_counter()
        if self._reranker is None:
            try:
                ranked = rerank(
                    query,
                    documents,
                    rerank_limit,
                    settings=self.settings,
                    cache_path=self.paths.rerank_cache,
                    document_ids=[candidate.chunk_id for candidate in candidates],
                )
            except Exception:
                self._log_event(
                    "rerank_request",
                    (
                        f"model={self.settings.models.rerank}, "
                        f"candidates={len(documents)}, status=failed"
                    ),
                    _elapsed_ms(rerank_started_at),
                    level="ERROR",
                )
                raise
        else:
            ranked = self._reranker(query, documents, rerank_limit)
        self._log_event(
            "rerank_request",
            f"model={self.settings.models.rerank}, candidates={len(documents)}",
            _elapsed_ms(rerank_started_at),
        )
        source_paths = self._load_source_paths(
            {candidate.source_id for candidate in candidates}
        )
        results: list[dict[str, Any]] = []
        rescue_candidates: list[tuple[_Candidate, float]] = []
        seen_indexes: set[int] = set()
        seen_families: set[str] = set()
        for candidate_index, score in ranked:
            if (
                not isinstance(candidate_index, int)
                or candidate_index in seen_indexes
                or not 0 <= candidate_index < len(candidates)
            ):
                raise RAGQueryError("rerank 返回了无效或重复的文档索引")
            if not isinstance(score, (int, float, np.integer, np.floating)):
                raise RAGQueryError("rerank 返回了非数值分数")
            if not np.isfinite(score):
                raise RAGQueryError("rerank 返回了 NaN 或 Infinity")
            seen_indexes.add(candidate_index)
            candidate = candidates[candidate_index]
            effective_score = max(
                float(score),
                _planned_lexical_match_score(
                    prepared.plan,
                    _candidate_context_content(candidate, hierarchy),
                ),
            )
            if effective_score < effective_threshold:
                if effective_score > 0:
                    rescue_candidates.append((candidate, effective_score))
                continue
            family_id = _chunk_family_id(candidate.chunk_id, hierarchy)
            if family_id in seen_families:
                continue
            seen_families.add(family_id)
            results.append(
                _rag_result_item(candidate, effective_score, hierarchy, source_paths)
            )
            if len(results) == effective_top_k:
                break
        if (
            not results
            and threshold is None
            and len(prepared.plan.variants) > 1
            and self.settings.query_planning.low_confidence_rescue_count > 0
        ):
            for candidate, score in sorted(
                rescue_candidates,
                key=lambda item: item[1],
                reverse=True,
            ):
                family_id = _chunk_family_id(candidate.chunk_id, hierarchy)
                if family_id in seen_families:
                    continue
                seen_families.add(family_id)
                item = _rag_result_item(candidate, score, hierarchy, source_paths)
                item["low_confidence"] = True
                results.append(item)
                if len(results) == min(
                    effective_top_k,
                    self.settings.query_planning.low_confidence_rescue_count,
                ):
                    break
        return {"query": query, "results": results}

    def prepare_query(self, query: str) -> PreparedQuery:
        """规划查询并一次批量向量化，供 chunk、实体和群组索引共同使用。"""

        if not isinstance(query, str) or not query.strip():
            raise RAGQueryError("query 必须是非空字符串")
        plan = plan_query(query, settings=self.settings)
        embedding_started_at = time.perf_counter()
        try:
            embedding = self._embed_texts(plan.texts, input_type="query")
        except Exception:
            self._log_event(
                "embedding_request",
                (
                    f"purpose=query, model={self.settings.models.embedding}, "
                    f"count={len(plan.variants)}, status=failed"
                ),
                _elapsed_ms(embedding_started_at),
                level="ERROR",
            )
            raise
        if len(embedding.vectors) != len(plan.variants):
            raise RAGQueryError("Embedding 返回向量数量与查询计划数量不一致")
        try:
            filtered_plan, kept_indexes = filter_semantic_drift(
                plan,
                embedding.vectors,
                self.settings.query_planning.semantic_drift_threshold,
            )
        except Exception as exc:
            raise RAGQueryError(f"查询扩展向量校验失败：{exc}") from exc
        filtered_embedding = EmbeddingResult(
            vectors=[embedding.vectors[index] for index in kept_indexes],
            vector_space_id=embedding.vector_space_id,
        )
        self._log_event(
            "embedding_request",
            (
                f"purpose=query, model={self.settings.models.embedding}, "
                f"planned={len(plan.variants)}, retained={len(filtered_plan.variants)}, "
                f"planner_mode={plan.mode}, degraded={str(plan.degraded).lower()}"
            ),
            _elapsed_ms(embedding_started_at),
        )
        return PreparedQuery(filtered_plan, filtered_embedding)

    def build_entity_vectors(self, node_id: str, summary: str) -> dict[str, Any]:
        """为单个实体建立或刷新向量；新鲜度由摘要及向量元数据决定。"""

        return self.sync_entity_vectors([(node_id, summary)])

    def build_community_vectors(
        self,
        group_id: str,
        summary: str,
    ) -> dict[str, Any]:
        """为单个群组建立或刷新向量。"""

        return self.sync_community_vectors([(group_id, summary)])

    def sync_entity_vectors(
        self,
        entities: Sequence[tuple[str, str]],
    ) -> dict[str, Any]:
        """批量补齐或刷新实体向量，跳过摘要和模型元数据均未变化的项。"""

        result = self._sync_auxiliary_vectors("entity", entities)
        self._refresh_entity_embedding_flags()
        return result

    def sync_community_vectors(
        self,
        communities: Sequence[tuple[str, str]],
    ) -> dict[str, Any]:
        """批量补齐或刷新群组向量。"""

        return self._sync_auxiliary_vectors("community", communities)

    def search_entities(
        self,
        query: str,
        top_k: int | None = None,
        *,
        prepared_query: PreparedQuery | None = None,
    ) -> list[dict[str, Any]]:
        """在实体摘要索引中执行语义检索。"""

        effective_top_k = (
            top_k if top_k is not None else self.settings.vector_search.entity_top_k
        )
        return self._search_auxiliary_vectors(
            "entity",
            query,
            effective_top_k,
            self.settings.vector_search.entity_weight,
            prepared_query=prepared_query,
        )

    def search_communities(
        self,
        query: str,
        top_k: int | None = None,
        *,
        prepared_query: PreparedQuery | None = None,
    ) -> list[dict[str, Any]]:
        """在群组总结索引中执行语义检索。"""

        effective_top_k = (
            top_k
            if top_k is not None
            else self.settings.vector_search.community_top_k
        )
        return self._search_auxiliary_vectors(
            "community",
            query,
            effective_top_k,
            self.settings.vector_search.community_weight,
            prepared_query=prepared_query,
        )

    def delete_entity_vectors(self, node_ids: Sequence[str]) -> int:
        """删除实体向量，并在索引增量删除失败时从 SQLite 恢复。"""

        removed = self._delete_auxiliary_vectors("entity", node_ids)
        self._refresh_entity_embedding_flags()
        return removed

    def delete_community_vectors(self, group_ids: Sequence[str]) -> int:
        """删除群组向量，并在索引增量删除失败时从 SQLite 恢复。"""

        return self._delete_auxiliary_vectors("community", group_ids)

    def rebuild_entity_index(self) -> int:
        """从 entity_embeddings 全量重建实体索引。"""

        return self._rebuild_auxiliary_index("entity")

    def rebuild_community_index(self) -> int:
        """从 community_embeddings 全量重建群组索引。"""

        return self._rebuild_auxiliary_index("community")

    def ensure_entity_index_consistency(self) -> None:
        self._ensure_entity_index_consistency()

    def ensure_community_index_consistency(self) -> None:
        self._ensure_community_index_consistency()

    def refresh_auxiliary_consistency(self) -> None:
        """清理图谱对象已删除或摘要已变化的辅助向量并同步两个索引。"""

        self._prune_stale_auxiliary_embeddings()
        self._ensure_entity_index_consistency()
        self._ensure_community_index_consistency()

    def add_vectors(
        self,
        vector_ids: Sequence[int],
        vectors: Sequence[Sequence[float]] | np.ndarray,
    ) -> None:
        """将已提交到 SQLite 的向量加入 FAISS 并清空 rerank 缓存。"""

        self.index.add(vector_ids, vectors)
        self._refresh_meta()
        clear_cache(self.paths.rerank_cache)

    def delete_vectors(self, vector_ids: Sequence[int]) -> int:
        """从 FAISS 物理删除向量并清空 rerank 缓存。"""

        removed = self.index.delete(vector_ids)
        if removed:
            self._refresh_meta()
            clear_cache(self.paths.rerank_cache)
        return removed

    def replace_vectors(
        self,
        remove_ids: Sequence[int],
        add_ids: Sequence[int],
        vectors: Sequence[Sequence[float]] | np.ndarray,
    ) -> int:
        """提交 SQLite 后，以一次原子索引替换完成单文档向量更新。"""

        removed = self.index.replace(remove_ids, add_ids, vectors)
        self._refresh_meta()
        clear_cache(self.paths.rerank_cache)
        return removed

    def refresh_meta(self) -> None:
        """根据当前 rag.db 计数刷新 rag_meta.json。"""

        self._refresh_meta()

    def ensure_index_consistency(self) -> None:
        """校验 FAISS 与 rag.db 的 ID 集合，不一致时从数据库原子重建。"""

        self._ensure_index_consistency()

    def search_vectors(
        self,
        vectors: Sequence[Sequence[float]] | np.ndarray,
        top_k: int,
    ) -> list[list[tuple[int, float]]]:
        return self.index.search(vectors, top_k)

    def rebuild_index(self) -> int:
        """从 rag.db 原始 embedding 全量重建 FAISS。"""

        started_at = time.perf_counter()
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                """
                SELECT vector_id, vector_blob, dimensions, model_name,
                       vector_space_id
                FROM embeddings
                ORDER BY vector_id
                """
            ).fetchall()
        finally:
            connection.close()

        vector_space_ids = {
            _validate_vector_space_id(row["vector_space_id"], int(row["vector_id"]))
            for row in rows
        }
        if len(vector_space_ids) > 1:
            raise IndexIntegrityError(
                "embeddings 包含多个 vector_space_id，不能构建同一个 FAISS 索引："
                f"{sorted(vector_space_ids)}"
            )

        vector_ids: list[int] = []
        vectors: list[np.ndarray] = []
        for row in rows:
            if row["dimensions"] != self.settings.models.embedding_dimensions:
                raise IndexIntegrityError(
                    f"vector_id={row['vector_id']} 的维度与当前配置不一致"
                )
            if row["model_name"] != self.settings.models.embedding:
                raise IndexIntegrityError(
                    f"vector_id={row['vector_id']} 的模型与当前配置不一致"
                )
            vector = np.frombuffer(row["vector_blob"], dtype=np.float32)
            if len(vector) != self.settings.models.embedding_dimensions:
                raise IndexIntegrityError(
                    f"vector_id={row['vector_id']} 的 BLOB 长度与 dimensions 不一致"
                )
            if not np.isfinite(vector).all():
                raise IndexIntegrityError(
                    f"vector_id={row['vector_id']} 包含 NaN 或 Infinity"
                )
            vector_ids.append(int(row["vector_id"]))
            vectors.append(vector.copy())

        matrix = (
            np.vstack(vectors).astype(np.float32, copy=False)
            if vectors
            else np.empty(
                (0, self.settings.models.embedding_dimensions), dtype=np.float32
            )
        )
        self.index.rebuild(vector_ids, matrix)
        self._refresh_meta()
        clear_cache(self.paths.rerank_cache)
        self._log_event(
            "faiss_rebuild",
            f"vectors={len(vector_ids)}",
            _elapsed_ms(started_at),
        )
        return len(vector_ids)

    def _embed_texts(self, texts: list[str], *, input_type: str) -> EmbeddingResult:
        if self._embedder is None:
            value = embed(
                texts,
                settings=self.settings,
                input_type=input_type,
            )
        else:
            value = self._embedder(texts)
        return _coerce_embedding_result(value)

    def _ensure_index_consistency(self) -> None:
        try:
            database_ids = self._database_vector_ids()
        except _MixedVectorSpaceError:
            # 明确触发一次从数据库重建；rebuild_index 会给出不可混用的诊断。
            self.rebuild_index()
            return
        if self.paths.faiss_index.exists():
            try:
                self.index.load()
                if self.index.ids == database_ids:
                    return
            except IndexIntegrityError:
                pass
        self.rebuild_index()

    def _database_vector_ids(self) -> set[int]:
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                """
                SELECT vector_id, dimensions, model_name, vector_space_id
                FROM embeddings
                """
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            if row["dimensions"] != self.settings.models.embedding_dimensions:
                raise IndexIntegrityError(
                    f"vector_id={row['vector_id']} 的维度与当前配置不一致，需重新 embedding"
                )
            if row["model_name"] != self.settings.models.embedding:
                raise IndexIntegrityError(
                    f"vector_id={row['vector_id']} 的模型与当前配置不一致，需重新 embedding"
                )
        vector_space_ids = {
            _validate_vector_space_id(row["vector_space_id"], int(row["vector_id"]))
            for row in rows
        }
        if len(vector_space_ids) > 1:
            raise _MixedVectorSpaceError(
                "embeddings 包含多个 vector_space_id，需重新 embedding："
                f"{sorted(vector_space_ids)}"
            )
        return {int(row["vector_id"]) for row in rows}

    def _database_vector_space_id(self) -> str | None:
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                "SELECT vector_id, vector_space_id FROM embeddings"
            ).fetchall()
        finally:
            connection.close()
        spaces = {
            _validate_vector_space_id(row["vector_space_id"], int(row["vector_id"]))
            for row in rows
        }
        if len(spaces) > 1:
            raise IndexIntegrityError(
                f"embeddings 包含多个 vector_space_id：{sorted(spaces)}"
            )
        return next(iter(spaces), None)

    def _load_candidates(
        self,
        raw_hits: Iterable[Iterable[tuple[int, float]]],
        *,
        query_weights: Sequence[float] | None = None,
        rrf_k: int = 60,
    ) -> list[_Candidate]:
        hit_lists = [list(query_hits) for query_hits in raw_hits]
        weights = list(query_weights) if query_weights is not None else [1.0] * len(hit_lists)
        vector_scores = _fuse_vector_hits(hit_lists, weights, rrf_k)
        if not vector_scores:
            return []

        placeholders = ",".join("?" for _ in vector_scores)
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                f"""
                SELECT e.vector_id, c.chunk_id, c.source_id, c.content,
                       c.granularity, c.parent_chunk_id
                FROM embeddings e
                JOIN chunks c ON c.chunk_id = e.chunk_id
                WHERE e.vector_id IN ({placeholders})
                """,
                tuple(vector_scores),
            ).fetchall()
        finally:
            connection.close()

        found_vector_ids = {int(row["vector_id"]) for row in rows}
        missing_vector_ids = set(vector_scores).difference(found_vector_ids)
        if missing_vector_ids:
            raise IndexIntegrityError(
                f"FAISS 中的 vector_id 在 rag.db 中不存在：{sorted(missing_vector_ids)}"
            )

        by_chunk: dict[str, _Candidate] = {}
        for row in rows:
            score = vector_scores[int(row["vector_id"])]
            candidate = _Candidate(
                vector_id=int(row["vector_id"]),
                chunk_id=row["chunk_id"],
                source_id=row["source_id"],
                content=row["content"],
                faiss_score=score,
                granularity=row["granularity"],
                parent_chunk_id=row["parent_chunk_id"],
            )
            previous = by_chunk.get(candidate.chunk_id)
            if previous is None or candidate.faiss_score > previous.faiss_score:
                by_chunk[candidate.chunk_id] = candidate
        return sorted(
            by_chunk.values(),
            key=lambda candidate: candidate.faiss_score,
            reverse=True,
        )

    def _load_lexical_candidates(
        self,
        plan: QueryPlan,
        *,
        limit: int,
    ) -> list[_Candidate]:
        """Load a small exact-text candidate pool from ``rag.db``.

        This is deliberately a recall supplement, not a replacement for FAISS:
        terms are bounded, parameterized, and each variant is capped.  The
        returned score is only used to select the pre-rerank pool; the final
        result score still comes from rerank/lexical validation.
        """

        if limit < 1:
            raise RAGQueryError("词面候选池上限必须大于等于 1")
        terms: list[tuple[str, float]] = []
        seen_terms: set[str] = set()
        for variant in plan.variants:
            raw = unicodedata.normalize("NFKC", variant.text).strip()
            raw = raw.strip(_LEXICAL_EDGE_PUNCTUATION)
            # Full natural-language questions are poor LIKE candidates and can
            # force a table scan.  Keep lexical fallback for compact terms and
            # planner-produced subqueries only.
            if len(raw) < _MIN_LEXICAL_QUERY_LENGTH or len(raw) > 128:
                continue
            key = _normalize_lexical_text(raw)
            if len(key) < _MIN_LEXICAL_QUERY_LENGTH or key in seen_terms:
                continue
            seen_terms.add(key)
            terms.append((raw, float(variant.weight)))
        if not terms:
            return []

        by_chunk: dict[str, _Candidate] = {}
        # Keep the total lexical supplement bounded even when the planner emits
        # several rewrites; each term receives a fair share of the pool.
        per_term_limit = max(1, (limit + len(terms) - 1) // len(terms))
        connection = connect_rag(self.paths)
        try:
            for term, weight in terms:
                rows = connection.execute(
                    """
                    SELECT e.vector_id, c.chunk_id, c.source_id, c.content,
                           c.granularity, c.parent_chunk_id
                    FROM chunks c
                    JOIN embeddings e ON e.chunk_id = c.chunk_id
                    WHERE instr(c.content, ?) > 0
                       OR instr(lower(c.content), lower(?)) > 0
                    ORDER BY c.source_id, c.chunk_index, c.chunk_id
                    LIMIT ?
                    """,
                    (term, term, int(per_term_limit)),
                ).fetchall()
                # Keep lexical candidates ahead of semantically similar but
                # unrelated chunks.  This score is internal to candidate
                # ordering and is intentionally not exposed as final relevance.
                lexical_score = _LEXICAL_CANDIDATE_SCORE + max(0.0, weight) * 0.01
                for row in rows:
                    candidate = _Candidate(
                        vector_id=int(row["vector_id"]),
                        chunk_id=str(row["chunk_id"]),
                        source_id=str(row["source_id"]),
                        content=str(row["content"]),
                        faiss_score=lexical_score,
                        granularity=str(row["granularity"]),
                        parent_chunk_id=row["parent_chunk_id"],
                    )
                    previous = by_chunk.get(candidate.chunk_id)
                    if previous is None or candidate.faiss_score > previous.faiss_score:
                        by_chunk[candidate.chunk_id] = candidate
        finally:
            connection.close()
        return sorted(
            by_chunk.values(),
            key=lambda candidate: (-candidate.faiss_score, candidate.chunk_id),
        )

    def _load_chunk_hierarchy(
        self,
        candidates: Sequence[_Candidate],
    ) -> dict[str, _ChunkContext]:
        hierarchy = {
            candidate.chunk_id: _ChunkContext(
                chunk_id=candidate.chunk_id,
                content=candidate.content,
                granularity=candidate.granularity,
                parent_chunk_id=candidate.parent_chunk_id,
            )
            for candidate in candidates
        }
        frontier = {
            candidate.parent_chunk_id
            for candidate in candidates
            if candidate.parent_chunk_id is not None
        }
        connection = connect_rag(self.paths)
        try:
            while frontier:
                pending = frontier.difference(hierarchy)
                if not pending:
                    break
                placeholders = ",".join("?" for _ in pending)
                rows = connection.execute(
                    f"""
                    SELECT chunk_id, content, granularity, parent_chunk_id
                    FROM chunks
                    WHERE chunk_id IN ({placeholders})
                    """,
                    tuple(sorted(pending)),
                ).fetchall()
                if len(rows) != len(pending):
                    found = {row["chunk_id"] for row in rows}
                    raise IndexIntegrityError(
                        "分层 chunk 的 parent_chunk_id 不存在："
                        f"{sorted(pending.difference(found))}"
                    )
                frontier = set()
                for row in rows:
                    context = _ChunkContext(
                        chunk_id=row["chunk_id"],
                        content=row["content"],
                        granularity=row["granularity"],
                        parent_chunk_id=row["parent_chunk_id"],
                    )
                    hierarchy[context.chunk_id] = context
                    if context.parent_chunk_id is not None:
                        frontier.add(context.parent_chunk_id)
        finally:
            connection.close()
        return hierarchy

    def _load_source_paths(self, source_ids: set[str]) -> dict[str, str]:
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        connection = connect_sources(self.paths)
        try:
            rows = connection.execute(
                f"""
                SELECT source_id, relative_path
                FROM sources
                WHERE source_id IN ({placeholders})
                """,
                tuple(source_ids),
            ).fetchall()
        finally:
            connection.close()
        paths = {row["source_id"]: row["relative_path"] for row in rows}
        missing_source_ids = source_ids.difference(paths)
        if missing_source_ids:
            raise IndexIntegrityError(
                f"chunk 对应的 source_id 在 sources.db 中不存在：{sorted(missing_source_ids)}"
            )
        return paths

    def _refresh_meta(self) -> None:
        connection = connect_rag(self.paths)
        try:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM chunks) AS total_chunks,
                    (SELECT COUNT(*) FROM embeddings) AS total_vectors
                """
            ).fetchone()
        finally:
            connection.close()
        vector_space_id = self._database_vector_space_id()
        meta = read_rag_meta(self.paths, self.settings)
        meta.update(
            {
                "total_chunks": int(counts["total_chunks"]),
                "total_vectors": int(counts["total_vectors"]),
                "vector_dimensions": self.settings.models.embedding_dimensions,
                "embedding_model": self.settings.models.embedding,
                "vector_space_id": vector_space_id,
                "chunking_signature": chunking_signature(self.settings),
                "faiss_index_type": "IndexIDMap2+IndexFlatIP",
                "last_built_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        write_rag_meta(self.paths, meta)

    def _log_event(
        self,
        action: str,
        detail: str,
        elapsed_ms: int | float | str = "-",
        *,
        level: str = "INFO",
    ) -> None:
        try:
            self._logger.log("rag_engine", action, detail, elapsed_ms, level)
        except Exception:
            pass
