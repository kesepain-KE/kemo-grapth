"""RAG data models and stateless ranking/validation helpers."""

from __future__ import annotations

import hashlib
import time
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from provider.embedding import EmbeddingResult

from .config import AppConfig
from .query_planner import QueryPlan
from .rag_errors import IndexIntegrityError, RAGQueryError


@dataclass(frozen=True)
class _Candidate:
    vector_id: int
    chunk_id: str
    source_id: str
    content: str
    faiss_score: float
    granularity: str
    parent_chunk_id: str | None


@dataclass(frozen=True)
class _ChunkContext:
    chunk_id: str
    content: str
    granularity: str
    parent_chunk_id: str | None


@dataclass(frozen=True)
class PreparedQuery:
    """已完成规划和一次批量向量化、可供多个 FAISS 索引复用的查询。"""

    plan: QueryPlan
    embedding: EmbeddingResult


_DIRECT_LEXICAL_MATCH_SCORE = 0.75
_LEXICAL_CANDIDATE_SCORE = 0.9
_MIN_LEXICAL_QUERY_LENGTH = 2
_LEXICAL_EDGE_PUNCTUATION = " \t\r\n,，;；:：.!！?？()（）[]【】{}<>《》\"'“”‘’"


class _MixedVectorSpaceError(IndexIntegrityError):
    """数据库中混入了不能共同索引的向量空间。"""


def _coerce_embedding_result(
    value: EmbeddingResult | list[list[float]],
) -> EmbeddingResult:
    """消费新签名，并兼容旧注入测试返回的裸向量列表。"""

    if isinstance(value, EmbeddingResult):
        vector_space_id = _validate_vector_space_id(value.vector_space_id)
        return EmbeddingResult(
            vectors=value.vectors,
            vector_space_id=vector_space_id,
        )
    if isinstance(value, list):
        return EmbeddingResult(vectors=value, vector_space_id="unknown")
    raise TypeError("Embedding 返回值必须是 EmbeddingResult")


def _normalize_auxiliary_records(
    records: Sequence[tuple[str, str]],
    id_column: str,
) -> dict[str, str]:
    if isinstance(records, (str, bytes)):
        raise TypeError("records 必须是 (id, summary) 序列")
    normalized: dict[str, str] = {}
    for item in records:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError("records 中每一项必须是 (id, summary) 二元组")
        object_id, summary = item
        if not isinstance(object_id, str) or not object_id.strip():
            raise ValueError(f"{id_column} 必须是非空字符串")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary 必须是非空字符串")
        normalized_id = object_id.strip()
        normalized_summary = summary.strip()
        previous = normalized.get(normalized_id)
        if previous is not None and previous != normalized_summary:
            raise ValueError(f"同一 {id_column} 对应了不同 summary：{normalized_id}")
        normalized[normalized_id] = normalized_summary
    return normalized


def _normalize_object_ids(values: Sequence[str], field_name: str) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} 列表不能是字符串")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} 必须是非空字符串")
        item = value.strip()
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    return normalized


def _summary_hash(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def _auxiliary_row_is_fresh(
    row: Any,
    summary: str,
    settings: AppConfig,
) -> bool:
    return bool(
        row is not None
        and str(row["summary_hash"]) == _summary_hash(summary)
        and int(row["dimensions"]) == settings.models.embedding_dimensions
        and str(row["model_name"]) == settings.models.embedding
        and isinstance(row["vector_space_id"], str)
        and bool(str(row["vector_space_id"]).strip())
    )


def _auxiliary_row_matches_graph(
    row: Any,
    expected_summary_hash: str | None,
    settings: AppConfig,
) -> bool:
    if (
        expected_summary_hash is None
        or str(row["summary_hash"]) != expected_summary_hash
        or int(row["dimensions"]) != settings.models.embedding_dimensions
        or str(row["model_name"]) != settings.models.embedding
    ):
        return False
    vector = np.frombuffer(row["vector_blob"], dtype=np.float32)
    return bool(
        len(vector) == settings.models.embedding_dimensions
        and np.isfinite(vector).all()
    )


def _fuse_vector_hits(
    raw_hits: Iterable[Iterable[tuple[int, float]]],
    query_weights: Sequence[float],
    rrf_k: int,
) -> dict[int, float]:
    """融合多查询 FAISS 排名，同时保留向量相似度的局部顺序。"""

    hit_lists = [list(hits) for hits in raw_hits]
    if len(hit_lists) != len(query_weights):
        raise RAGQueryError("FAISS 查询结果数量与查询权重数量不一致")
    if rrf_k < 1:
        raise RAGQueryError("rrf_k 必须大于等于 1")
    if len(hit_lists) == 1:
        weight = float(query_weights[0])
        if not np.isfinite(weight) or weight <= 0:
            raise RAGQueryError("查询权重必须是正有限数")
        return {
            vector_id: weight * float(score)
            for vector_id, score in hit_lists[0]
        }

    max_scores: dict[int, float] = {}
    rrf_scores: dict[int, float] = {}
    for hits, raw_weight in zip(hit_lists, query_weights):
        weight = float(raw_weight)
        if not np.isfinite(weight) or weight <= 0:
            raise RAGQueryError("查询权重必须是正有限数")
        for rank, (vector_id, raw_score) in enumerate(hits, start=1):
            score = float(raw_score)
            if not np.isfinite(score):
                raise RAGQueryError("FAISS 返回了 NaN 或 Infinity")
            weighted_score = weight * score
            max_scores[vector_id] = max(
                weighted_score,
                max_scores.get(vector_id, float("-inf")),
            )
            rrf_scores[vector_id] = rrf_scores.get(vector_id, 0.0) + weight / (
                rrf_k + rank
            )
    return {
        vector_id: max_scores[vector_id] + rrf_scores.get(vector_id, 0.0)
        for vector_id in max_scores
    }


def _merge_candidates(
    semantic_candidates: Sequence[_Candidate],
    lexical_candidates: Sequence[_Candidate],
) -> list[_Candidate]:
    """Merge ANN and exact-text candidates, retaining the strongest evidence."""

    by_chunk: dict[str, _Candidate] = {
        candidate.chunk_id: candidate for candidate in semantic_candidates
    }
    for candidate in lexical_candidates:
        previous = by_chunk.get(candidate.chunk_id)
        if previous is None or candidate.faiss_score > previous.faiss_score:
            by_chunk[candidate.chunk_id] = candidate
    return sorted(
        by_chunk.values(),
        key=lambda candidate: (-candidate.faiss_score, candidate.chunk_id),
    )


def _collapse_candidates_by_family(
    candidates: Sequence[_Candidate],
    hierarchy: Mapping[str, _ChunkContext],
    limit: int,
) -> list[_Candidate]:
    """Keep one precise representative per hierarchy before Rerank.

    Large chunks remain available as parent context.  They only become direct
    candidates when a legacy or partial hierarchy has no smaller searchable
    chunks at all.
    """

    if limit < 1:
        raise RAGQueryError("候选池上限必须大于等于 1")
    searchable = [candidate for candidate in candidates if candidate.granularity != "large"]
    if not searchable:
        searchable = list(candidates)
    rank = {"small": 0, "medium": 1, "large": 2}
    by_family: dict[str, _Candidate] = {}
    for candidate in searchable:
        family_id = _chunk_family_id(candidate.chunk_id, hierarchy)
        previous = by_family.get(family_id)
        if previous is None or (
            candidate.faiss_score > previous.faiss_score
            or (
                candidate.faiss_score == previous.faiss_score
                and rank.get(candidate.granularity, 3)
                < rank.get(previous.granularity, 3)
            )
        ):
            by_family[family_id] = candidate
    return sorted(
        by_family.values(),
        key=lambda candidate: candidate.faiss_score,
        reverse=True,
    )[:limit]


def _candidate_context_content(
    candidate: _Candidate,
    hierarchy: Mapping[str, _ChunkContext],
) -> str:
    if candidate.parent_chunk_id is None:
        return candidate.content
    parent = hierarchy.get(candidate.parent_chunk_id)
    return parent.content if parent is not None and parent.content.strip() else candidate.content


def _planned_lexical_match_score(plan: QueryPlan, document: str) -> float:
    """原始词优先，并允许通过漂移校验的扩展词提供较弱字面保底。"""

    return max(
        (
            _direct_lexical_match_score(variant.text, document) * variant.weight
            for variant in plan.variants
        ),
        default=0.0,
    )


def _rag_result_item(
    candidate: _Candidate,
    score: float,
    hierarchy: Mapping[str, _ChunkContext],
    source_paths: Mapping[str, str | None],
) -> dict[str, Any]:
    parent = (
        hierarchy.get(candidate.parent_chunk_id)
        if candidate.parent_chunk_id is not None
        else None
    )
    return {
        "chunk_id": candidate.chunk_id,
        "content": candidate.content,
        "score": score,
        "granularity": candidate.granularity,
        "parent_chunk_id": candidate.parent_chunk_id,
        "context": (
            {
                "chunk_id": parent.chunk_id,
                "content": parent.content,
                "granularity": parent.granularity,
            }
            if parent is not None
            else None
        ),
        "source": {
            "source_id": candidate.source_id,
            "relative_path": source_paths.get(candidate.source_id),
        },
    }


def _direct_lexical_match_score(query: str, document: str) -> float:
    """为文档中的直接关键词命中提供保底分，避免短词被 Rerank 尺度吞没。"""

    normalized_query = _normalize_lexical_text(query).strip(_LEXICAL_EDGE_PUNCTUATION)
    if len(normalized_query) < _MIN_LEXICAL_QUERY_LENGTH:
        return 0.0
    normalized_document = _normalize_lexical_text(document)
    if normalized_query == normalized_document:
        return 1.0
    if normalized_query in normalized_document:
        return _DIRECT_LEXICAL_MATCH_SCORE
    return 0.0


def _chunk_family_id(
    chunk_id: str,
    hierarchy: Mapping[str, _ChunkContext],
) -> str:
    current = chunk_id
    visited: set[str] = set()
    while True:
        if current in visited:
            raise IndexIntegrityError(f"分层 chunk 出现父子循环：{chunk_id}")
        visited.add(current)
        context = hierarchy.get(current)
        if context is None or context.parent_chunk_id is None:
            return current
        current = context.parent_chunk_id


def _normalize_lexical_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _validate_prepared_query(query: str, prepared: PreparedQuery) -> None:
    if not isinstance(prepared, PreparedQuery):
        raise RAGQueryError("prepared_query 类型无效")
    normalized = " ".join(unicodedata.normalize("NFKC", query).strip().split())
    if prepared.plan.original != normalized:
        raise RAGQueryError("prepared_query 与当前 query 不一致")
    if len(prepared.plan.variants) != len(prepared.embedding.vectors):
        raise RAGQueryError("prepared_query 的计划与向量数量不一致")


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _validate_vector_space_id(value: Any, vector_id: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        prefix = f"vector_id={vector_id} 的" if vector_id is not None else ""
        raise IndexIntegrityError(f"{prefix}vector_space_id 为空")
    return value.strip()


def _validate_score_multipliers(values: Mapping[str, float]) -> None:
    for chunk_id, factor in values.items():
        if not isinstance(chunk_id, str) or not chunk_id:
            raise RAGQueryError("score_multipliers 的键必须是非空 chunk_id")
        if (
            not isinstance(factor, (int, float, np.integer, np.floating))
            or isinstance(factor, bool)
            or not np.isfinite(factor)
            or float(factor) < 1.0
        ):
            raise RAGQueryError("score_multipliers 的增强系数必须是大于等于 1 的有限数")
