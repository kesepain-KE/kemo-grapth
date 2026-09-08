"""Atomic FAISS index persistence and vector validation."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Sequence

import numpy as np

try:
    import faiss
except ImportError:  # pragma: no cover - covered by runtime dependency checks
    faiss = None  # type: ignore[assignment]

from .rag_errors import FaissUnavailableError, IndexIntegrityError


class FaissIndexManager:
    """管理持久化的 IndexIDMap2 + IndexFlatIP 索引。"""

    def __init__(
        self,
        index_path: Path | str,
        dimensions: int,
        *,
        autoload: bool = True,
    ) -> None:
        if faiss is None:
            raise FaissUnavailableError("缺少 faiss-cpu，请先安装 requirements.txt")
        if dimensions < 1:
            raise ValueError("dimensions 必须大于等于 1")
        self.index_path = Path(index_path)
        self.dimensions = dimensions
        self._lock = threading.RLock()
        self._index: Any | None = None
        if autoload and self.index_path.exists():
            self.load()
        else:
            self.create()

    @property
    def count(self) -> int:
        with self._lock:
            return int(self._require_index().ntotal)

    @property
    def ids(self) -> set[int]:
        with self._lock:
            index = self._require_index()
            return {
                int(value) for value in faiss.vector_to_array(index.id_map).tolist()
            }

    def create(self, *, persist: bool = False) -> None:
        """创建空的精确内积索引。"""

        with self._lock:
            new_index = self._new_index()
            if persist:
                self._persist(new_index)
            self._index = new_index

    def load(self) -> None:
        """从磁盘加载并校验索引类型和维度。"""

        with self._lock:
            try:
                loaded = faiss.read_index(str(self.index_path))
            except Exception as exc:
                raise IndexIntegrityError(
                    f"无法加载 FAISS 索引：{self.index_path}: {exc}"
                ) from exc
            self._validate_index(loaded)
            self._index = loaded

    def save(self) -> None:
        """将当前索引原子写入磁盘。"""

        with self._lock:
            self._persist(self._require_index())

    def add(
        self,
        vector_ids: Sequence[int],
        vectors: Sequence[Sequence[float]] | np.ndarray,
    ) -> None:
        """原子添加带稳定 ID 的向量。"""

        ids = _validate_vector_ids(vector_ids)
        matrix = _as_float32_matrix(vectors, self.dimensions)
        if len(ids) != len(matrix):
            raise ValueError("vector_ids 数量必须与 vectors 数量一致")
        if not ids:
            return
        if len(set(ids)) != len(ids):
            raise IndexIntegrityError("同一批次包含重复 vector_id")
        with self._lock:
            existing_ids = {
                int(value)
                for value in faiss.vector_to_array(
                    self._require_index().id_map
                ).tolist()
            }
            duplicates = existing_ids.intersection(ids)
            if duplicates:
                raise IndexIntegrityError(
                    f"FAISS 中已存在 vector_id：{sorted(duplicates)}"
                )
            candidate = faiss.clone_index(self._require_index())
            candidate.add_with_ids(matrix, np.asarray(ids, dtype=np.int64))
            self._persist(candidate)
            self._index = candidate

    def delete(self, vector_ids: Sequence[int]) -> int:
        """物理删除指定向量并返回实际删除数量。"""

        ids = _validate_vector_ids(vector_ids)
        if not ids:
            return 0
        with self._lock:
            candidate = faiss.clone_index(self._require_index())
            removed = int(candidate.remove_ids(np.asarray(ids, dtype=np.int64)))
            if removed:
                self._persist(candidate)
                self._index = candidate
            return removed

    def replace(
        self,
        remove_ids: Sequence[int],
        add_ids: Sequence[int],
        vectors: Sequence[Sequence[float]] | np.ndarray,
    ) -> int:
        """在单次原子持久化中删除旧向量并加入新向量。"""

        old_ids = _validate_vector_ids(remove_ids)
        new_ids = _validate_vector_ids(add_ids)
        matrix = _as_float32_matrix(vectors, self.dimensions)
        if len(new_ids) != len(matrix):
            raise ValueError("add_ids 数量必须与 vectors 数量一致")
        if len(set(new_ids)) != len(new_ids):
            raise IndexIntegrityError("同一批次包含重复的新 vector_id")
        if not old_ids and not new_ids:
            return 0

        with self._lock:
            candidate = faiss.clone_index(self._require_index())
            removed = (
                int(candidate.remove_ids(np.asarray(old_ids, dtype=np.int64)))
                if old_ids
                else 0
            )
            remaining_ids = {
                int(value) for value in faiss.vector_to_array(candidate.id_map).tolist()
            }
            duplicates = remaining_ids.intersection(new_ids)
            if duplicates:
                raise IndexIntegrityError(
                    f"FAISS 中已存在新的 vector_id：{sorted(duplicates)}"
                )
            if new_ids:
                candidate.add_with_ids(matrix, np.asarray(new_ids, dtype=np.int64))
            self._persist(candidate)
            self._index = candidate
            return removed

    def search(
        self,
        vectors: Sequence[Sequence[float]] | np.ndarray,
        top_k: int,
    ) -> list[list[tuple[int, float]]]:
        """对每个查询向量返回按内积分数降序排列的 ``(id, score)``。"""

        if top_k < 1:
            raise ValueError("top_k 必须大于等于 1")
        matrix = _as_float32_matrix(vectors, self.dimensions)
        if len(matrix) == 0:
            return []
        with self._lock:
            index = self._require_index()
            if index.ntotal == 0:
                return [[] for _ in range(len(matrix))]
            limit = min(top_k, int(index.ntotal))
            scores, ids = index.search(matrix, limit)
        return [
            [
                (int(vector_id), float(score))
                for vector_id, score in zip(row_ids, row_scores, strict=True)
                if vector_id >= 0
            ]
            for row_ids, row_scores in zip(ids, scores, strict=True)
        ]

    def rebuild(
        self,
        vector_ids: Sequence[int],
        vectors: Sequence[Sequence[float]] | np.ndarray,
    ) -> None:
        """使用完整向量集合重建并原子替换索引。"""

        ids = _validate_vector_ids(vector_ids)
        matrix = _as_float32_matrix(vectors, self.dimensions)
        if len(ids) != len(matrix):
            raise ValueError("vector_ids 数量必须与 vectors 数量一致")
        if len(set(ids)) != len(ids):
            raise IndexIntegrityError("重建数据包含重复 vector_id")

        with self._lock:
            candidate = self._new_index()
            if ids:
                candidate.add_with_ids(matrix, np.asarray(ids, dtype=np.int64))
            self._persist(candidate)
            self._index = candidate

    def _new_index(self) -> Any:
        return faiss.IndexIDMap2(faiss.IndexFlatIP(self.dimensions))

    def _require_index(self) -> Any:
        if self._index is None:
            raise IndexIntegrityError("FAISS 索引尚未创建或加载")
        return self._index

    def _validate_index(self, index: Any) -> None:
        if not isinstance(index, faiss.IndexIDMap2):
            raise IndexIntegrityError("FAISS 索引类型必须是 IndexIDMap2")
        if int(index.d) != self.dimensions:
            raise IndexIntegrityError(
                f"FAISS 维度不一致：期望 {self.dimensions}，实际 {index.d}"
            )
        ids = faiss.vector_to_array(index.id_map)
        if len(ids) != len(set(int(value) for value in ids.tolist())):
            raise IndexIntegrityError("FAISS 索引包含重复 vector_id")

    def _persist(self, index: Any) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
        try:
            faiss.write_index(index, str(temporary_path))
            verification = faiss.read_index(str(temporary_path))
            self._validate_index(verification)
            expected_ids = {
                int(value) for value in faiss.vector_to_array(index.id_map).tolist()
            }
            actual_ids = {
                int(value)
                for value in faiss.vector_to_array(verification.id_map).tolist()
            }
            if expected_ids != actual_ids or int(index.ntotal) != int(
                verification.ntotal
            ):
                raise IndexIntegrityError("FAISS 临时索引校验失败")
            os.replace(temporary_path, self.index_path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _validate_vector_ids(vector_ids: Sequence[int]) -> list[int]:
    ids = list(vector_ids)
    if any(not isinstance(value, (int, np.integer)) or int(value) < 0 for value in ids):
        raise ValueError("vector_id 必须是非负整数")
    return [int(value) for value in ids]


def _as_float32_matrix(
    vectors: Sequence[Sequence[float]] | np.ndarray,
    dimensions: int,
) -> np.ndarray:
    try:
        matrix = np.asarray(vectors, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError("vectors 必须是规则的二维数值数组") from exc
    if matrix.size == 0:
        return np.empty((0, dimensions), dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != dimensions:
        raise ValueError(f"向量维度错误：期望 (*, {dimensions})，实际 {matrix.shape}")
    if not np.isfinite(matrix).all():
        raise ValueError("向量包含 NaN 或 Infinity")
    return np.ascontiguousarray(matrix)

