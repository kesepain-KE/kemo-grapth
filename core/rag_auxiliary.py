"""Auxiliary entity/community vector-index domain for :class:RAGEngine."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import numpy as np

from .db import connect_graph, connect_rag
from .faiss_index import FaissIndexManager, _as_float32_matrix
from .rag_errors import IndexIntegrityError, RAGQueryError
from .rag_support import (
    PreparedQuery,
    _auxiliary_row_is_fresh,
    _auxiliary_row_matches_graph,
    _elapsed_ms,
    _fuse_vector_hits,
    _normalize_auxiliary_records,
    _normalize_object_ids,
    _summary_hash,
    _validate_prepared_query,
    _validate_vector_space_id,
)


class RAGAuxiliaryMixin:
    """Maintain and query non-chunk vector spaces owned by the RAG engine."""

    def _sync_auxiliary_vectors(
        self,
        kind: str,
        records: Sequence[tuple[str, str]],
    ) -> dict[str, Any]:
        table, id_column, manager = self._auxiliary_spec(kind)
        normalized = _normalize_auxiliary_records(records, id_column)
        if not normalized:
            return {"updated": 0, "skipped": 0, "vector_space_id": None}

        object_ids = list(normalized)
        placeholders = ",".join("?" for _ in object_ids)
        connection = connect_rag(self.paths)
        try:
            existing_rows = connection.execute(
                f"""
                SELECT vector_id, {id_column}, summary_hash, dimensions,
                       model_name, vector_space_id
                FROM {table}
                WHERE {id_column} IN ({placeholders})
                """,
                tuple(object_ids),
            ).fetchall()
        finally:
            connection.close()
        existing = {str(row[id_column]): row for row in existing_rows}
        pending = [
            (object_id, summary)
            for object_id, summary in normalized.items()
            if not _auxiliary_row_is_fresh(
                existing.get(object_id),
                summary,
                self.settings,
            )
        ]
        if not pending:
            return {
                "updated": 0,
                "skipped": len(normalized),
                "vector_space_id": self._auxiliary_vector_space_id(kind),
            }

        started_at = time.perf_counter()
        embedding_result = self._embed_texts(
            [summary for _, summary in pending],
            input_type="document",
        )
        matrix = _as_float32_matrix(
            embedding_result.vectors,
            self.settings.models.embedding_dimensions,
        )
        if len(matrix) != len(pending):
            raise IndexIntegrityError(
                f"{kind} Embedding 返回数量不一致："
                f"期望 {len(pending)}，实际 {len(matrix)}"
            )
        vector_space_id = _validate_vector_space_id(
            embedding_result.vector_space_id
        )
        pending_ids = [object_id for object_id, _ in pending]
        pending_placeholders = ",".join("?" for _ in pending_ids)
        connection = connect_rag(self.paths)
        try:
            retained_spaces = {
                _validate_vector_space_id(row["vector_space_id"])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT vector_space_id FROM {table}
                    WHERE {id_column} NOT IN ({pending_placeholders})
                    """,
                    tuple(pending_ids),
                ).fetchall()
            }
            if retained_spaces.difference({vector_space_id}):
                raise IndexIntegrityError(
                    f"不能在 {table} 中混用 vector_space_id："
                    f"现有={sorted(retained_spaces)}，新增={vector_space_id}"
                )
            connection.execute("BEGIN IMMEDIATE")
            old_vector_ids = [
                int(row["vector_id"])
                for row in connection.execute(
                    f"""
                    SELECT vector_id FROM {table}
                    WHERE {id_column} IN ({pending_placeholders})
                    """,
                    tuple(pending_ids),
                ).fetchall()
            ]
            connection.execute(
                f"DELETE FROM {table} WHERE {id_column} IN ({pending_placeholders})",
                tuple(pending_ids),
            )
            now = datetime.now(timezone.utc).isoformat()
            new_vector_ids: list[int] = []
            for (object_id, summary), vector in zip(pending, matrix, strict=True):
                cursor = connection.execute(
                    f"""
                    INSERT INTO {table} (
                        {id_column}, summary, summary_hash, vector_blob,
                        dimensions, model_name, vector_space_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        object_id,
                        summary,
                        _summary_hash(summary),
                        vector.tobytes(),
                        self.settings.models.embedding_dimensions,
                        self.settings.models.embedding,
                        vector_space_id,
                        now,
                        now,
                    ),
                )
                new_vector_ids.append(int(cursor.lastrowid))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        try:
            manager.replace(old_vector_ids, new_vector_ids, matrix)
        except Exception as replace_error:
            try:
                self._rebuild_auxiliary_index(kind)
            except Exception as rebuild_error:
                raise IndexIntegrityError(
                    f"{kind} FAISS 增量替换失败，且无法从 SQLite 恢复："
                    f"replace={replace_error}; rebuild={rebuild_error}"
                ) from rebuild_error
        self._log_event(
            "auxiliary_embedding_build",
            f"kind={kind}, updated={len(pending)}, model={self.settings.models.embedding}",
            _elapsed_ms(started_at),
        )
        return {
            "updated": len(pending),
            "skipped": len(normalized) - len(pending),
            "vector_space_id": vector_space_id,
        }

    def _search_auxiliary_vectors(
        self,
        kind: str,
        query: str,
        top_k: int,
        weight: float,
        *,
        prepared_query: PreparedQuery | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip():
            raise RAGQueryError("query 必须是非空字符串")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise RAGQueryError("top_k 必须是大于等于 1 的整数")
        _, _, manager = self._auxiliary_spec(kind)
        if manager.count == 0:
            return []

        prepared = prepared_query or self.prepare_query(query)
        _validate_prepared_query(query, prepared)
        embedding = prepared.embedding
        database_space = self._auxiliary_vector_space_id(kind)
        if database_space is not None and embedding.vector_space_id != database_space:
            raise RAGQueryError(
                f"查询向量空间与 {kind} 索引不一致："
                f"查询={embedding.vector_space_id}，索引={database_space}"
            )
        per_query_limit = top_k
        if len(prepared.plan.variants) > 1:
            per_query_limit = max(top_k * 2, self.settings.query_planning.candidate_pool_size)
        raw_hits = manager.search(embedding.vectors, per_query_limit)
        vector_scores = _fuse_vector_hits(
            raw_hits,
            prepared.plan.weights,
            self.settings.query_planning.rrf_k,
        )
        return self._load_auxiliary_results(kind, vector_scores, float(weight))[:top_k]

    def _load_auxiliary_results(
        self,
        kind: str,
        vector_scores: Mapping[int, float],
        weight: float,
    ) -> list[dict[str, Any]]:
        if not vector_scores:
            return []
        table, id_column, _ = self._auxiliary_spec(kind)
        placeholders = ",".join("?" for _ in vector_scores)
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                f"""
                SELECT vector_id, {id_column} FROM {table}
                WHERE vector_id IN ({placeholders})
                """,
                tuple(vector_scores),
            ).fetchall()
        finally:
            connection.close()
        found_ids = {int(row["vector_id"]) for row in rows}
        if found_ids != set(vector_scores):
            raise IndexIntegrityError(
                f"{kind} FAISS 中存在 SQLite 缺失的 vector_id："
                f"{sorted(set(vector_scores).difference(found_ids))}"
            )
        object_by_vector = {
            int(row["vector_id"]): str(row[id_column]) for row in rows
        }
        object_ids = set(object_by_vector.values())
        object_placeholders = ",".join("?" for _ in object_ids)
        graph = connect_graph(self.paths)
        try:
            if kind == "entity":
                graph_rows = graph.execute(
                    f"""
                    SELECT node_id, keyword, summary FROM nodes
                    WHERE node_id IN ({object_placeholders})
                    """,
                    tuple(sorted(object_ids)),
                ).fetchall()
                details = {
                    str(row["node_id"]): {
                        "node_id": str(row["node_id"]),
                        "keyword": str(row["keyword"]),
                        "summary": str(row["summary"]),
                        "source": "entity",
                    }
                    for row in graph_rows
                }
            else:
                graph_rows = graph.execute(
                    f"""
                    SELECT group_id, summary, node_count FROM groups
                    WHERE group_id IN ({object_placeholders})
                    """,
                    tuple(sorted(object_ids)),
                ).fetchall()
                details = {
                    str(row["group_id"]): {
                        "group_id": str(row["group_id"]),
                        "summary": str(row["summary"]),
                        "node_count": int(row["node_count"] or 0),
                        "source": "community",
                    }
                    for row in graph_rows
                }
        finally:
            graph.close()
        missing_objects = object_ids.difference(details)
        if missing_objects:
            raise IndexIntegrityError(
                f"{table} 引用了不存在的图谱对象：{sorted(missing_objects)}"
            )

        results: list[dict[str, Any]] = []
        for vector_id, score in sorted(
            vector_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        ):
            detail = details[object_by_vector[vector_id]].copy()
            detail["score"] = float(score) * weight
            results.append(detail)
        return results

    def _delete_auxiliary_vectors(
        self,
        kind: str,
        object_ids: Sequence[str],
    ) -> int:
        table, id_column, manager = self._auxiliary_spec(kind)
        normalized_ids = _normalize_object_ids(object_ids, id_column)
        if not normalized_ids:
            return 0
        placeholders = ",".join("?" for _ in normalized_ids)
        connection = connect_rag(self.paths)
        try:
            connection.execute("BEGIN IMMEDIATE")
            vector_ids = [
                int(row["vector_id"])
                for row in connection.execute(
                    f"""
                    SELECT vector_id FROM {table}
                    WHERE {id_column} IN ({placeholders})
                    """,
                    tuple(normalized_ids),
                ).fetchall()
            ]
            connection.execute(
                f"DELETE FROM {table} WHERE {id_column} IN ({placeholders})",
                tuple(normalized_ids),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        try:
            manager.delete(vector_ids)
        except Exception:
            self._rebuild_auxiliary_index(kind)
        return len(vector_ids)

    def _ensure_entity_index_consistency(self) -> None:
        self._ensure_auxiliary_index_consistency("entity")

    def _ensure_community_index_consistency(self) -> None:
        self._ensure_auxiliary_index_consistency("community")

    def _ensure_auxiliary_index_consistency(self, kind: str) -> None:
        _, _, manager = self._auxiliary_spec(kind)
        database_ids = self._database_auxiliary_vector_ids(kind)
        if manager.index_path.exists():
            try:
                manager.load()
                if manager.ids == database_ids:
                    return
            except IndexIntegrityError:
                pass
        self._rebuild_auxiliary_index(kind)

    def _rebuild_auxiliary_index(self, kind: str) -> int:
        table, _, manager = self._auxiliary_spec(kind)
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                f"""
                SELECT vector_id, vector_blob, dimensions, model_name,
                       vector_space_id FROM {table} ORDER BY vector_id
                """
            ).fetchall()
        finally:
            connection.close()
        vector_ids, matrix = self._validated_auxiliary_matrix(rows, table)
        manager.rebuild(vector_ids, matrix)
        self._log_event(
            "faiss_rebuild",
            f"kind={kind}, vectors={len(vector_ids)}",
        )
        return len(vector_ids)

    def _database_auxiliary_vector_ids(self, kind: str) -> set[int]:
        table, _, _ = self._auxiliary_spec(kind)
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                f"""
                SELECT vector_id, vector_blob, dimensions, model_name,
                       vector_space_id FROM {table}
                """
            ).fetchall()
        finally:
            connection.close()
        self._validated_auxiliary_matrix(rows, table)
        return {int(row["vector_id"]) for row in rows}

    def _validated_auxiliary_matrix(
        self,
        rows: Sequence[Any],
        table: str,
    ) -> tuple[list[int], np.ndarray]:
        spaces = {
            _validate_vector_space_id(row["vector_space_id"], int(row["vector_id"]))
            for row in rows
        }
        if len(spaces) > 1:
            raise IndexIntegrityError(
                f"{table} 包含多个 vector_space_id：{sorted(spaces)}"
            )
        vector_ids: list[int] = []
        vectors: list[np.ndarray] = []
        for row in rows:
            vector_id = int(row["vector_id"])
            if int(row["dimensions"]) != self.settings.models.embedding_dimensions:
                raise IndexIntegrityError(f"{table}.vector_id={vector_id} 维度不一致")
            if str(row["model_name"]) != self.settings.models.embedding:
                raise IndexIntegrityError(f"{table}.vector_id={vector_id} 模型不一致")
            vector = np.frombuffer(row["vector_blob"], dtype=np.float32)
            if (
                len(vector) != self.settings.models.embedding_dimensions
                or not np.isfinite(vector).all()
            ):
                raise IndexIntegrityError(
                    f"{table}.vector_id={vector_id} 的向量 BLOB 无效"
                )
            vector_ids.append(vector_id)
            vectors.append(vector.copy())
        matrix = (
            np.vstack(vectors).astype(np.float32, copy=False)
            if vectors
            else np.empty(
                (0, self.settings.models.embedding_dimensions),
                dtype=np.float32,
            )
        )
        return vector_ids, matrix

    def _auxiliary_vector_space_id(self, kind: str) -> str | None:
        table, _, _ = self._auxiliary_spec(kind)
        connection = connect_rag(self.paths)
        try:
            rows = connection.execute(
                f"SELECT vector_id, vector_space_id FROM {table}"
            ).fetchall()
        finally:
            connection.close()
        spaces = {
            _validate_vector_space_id(row["vector_space_id"], int(row["vector_id"]))
            for row in rows
        }
        if len(spaces) > 1:
            raise IndexIntegrityError(
                f"{table} 包含多个 vector_space_id：{sorted(spaces)}"
            )
        return next(iter(spaces), None)

    def _prune_stale_auxiliary_embeddings(self) -> None:
        graph = connect_graph(self.paths)
        try:
            entity_hashes = {
                str(row["node_id"]): _summary_hash(str(row["summary"]))
                for row in graph.execute("SELECT node_id, summary FROM nodes").fetchall()
            }
            community_hashes = {
                str(row["group_id"]): _summary_hash(str(row["summary"]))
                for row in graph.execute(
                    "SELECT group_id, summary FROM groups"
                ).fetchall()
            }
        finally:
            graph.close()
        connection = connect_rag(self.paths)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table, id_column, expected_hashes in (
                ("entity_embeddings", "node_id", entity_hashes),
                ("community_embeddings", "group_id", community_hashes),
            ):
                rows = connection.execute(
                    f"""
                    SELECT vector_id, {id_column}, summary_hash, vector_blob,
                           dimensions, model_name FROM {table}
                    """
                ).fetchall()
                stale_ids = [
                    int(row["vector_id"])
                    for row in rows
                    if not _auxiliary_row_matches_graph(
                        row,
                        expected_hashes.get(str(row[id_column])),
                        self.settings,
                    )
                ]
                if stale_ids:
                    placeholders = ",".join("?" for _ in stale_ids)
                    connection.execute(
                        f"DELETE FROM {table} WHERE vector_id IN ({placeholders})",
                        tuple(stale_ids),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._refresh_entity_embedding_flags()

    def _refresh_entity_embedding_flags(self) -> None:
        rag = connect_rag(self.paths)
        try:
            node_ids = {
                str(row["node_id"])
                for row in rag.execute(
                    "SELECT node_id FROM entity_embeddings"
                ).fetchall()
            }
        finally:
            rag.close()
        graph = connect_graph(self.paths)
        try:
            graph.execute("BEGIN IMMEDIATE")
            graph.execute("UPDATE nodes SET has_entity_embedding = 0")
            if node_ids:
                placeholders = ",".join("?" for _ in node_ids)
                graph.execute(
                    f"""
                    UPDATE nodes SET has_entity_embedding = 1
                    WHERE node_id IN ({placeholders})
                    """,
                    tuple(sorted(node_ids)),
                )
            graph.commit()
        except Exception:
            graph.rollback()
            raise
        finally:
            graph.close()

    def _auxiliary_spec(
        self,
        kind: str,
    ) -> tuple[str, str, FaissIndexManager]:
        if kind == "entity":
            return "entity_embeddings", "node_id", self.entity_index
        if kind == "community":
            return "community_embeddings", "group_id", self.community_index
        raise ValueError(f"未知辅助向量类型：{kind}")

