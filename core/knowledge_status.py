"""供 CLI、外部 API 和 Web 入口共用的知识库门面。"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

from provider.engine import chat
from provider.tools.document_tools import (
    DocumentConversionError,
    SUPPORTED_DOCUMENT_SUFFIXES,
    import_document as convert_document,
)

from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config
from .db import (
    connect_graph,
    connect_rag,
    connect_sources,
    get_database_paths,
    read_graph_meta,
    write_graph_meta,
)
from .graph_engine import GraphEngine
from .graph_organizer import GraphOrganizer
from .graph_visualization import (
    GraphDirection,
    get_neighborhood,
    get_visualization_meta,
    list_visualization_edges,
    list_visualization_nodes,
)
from .hybrid import HybridEngine
from .ingestor import (
    DocumentNotFoundError,
    FileMapError,
    IngestError,
    Ingestor,
    RecycleConflictError,
)
from .logger import DailyTSVLogger
from .locks import get_knowledge_base_lock
from .rag_engine import FaissIndexManager, RAGEngine
from .rebuilder import KnowledgeBaseRebuilder, ProgressCallback
from .services import (
    DocumentService,
    GraphService,
    MaintenanceService,
    RetrievalService,
)

from .knowledge_models import (
    CONFIG_API_KEY_MASK,
    MAX_IMPORT_BYTES,
    SUPPORTED_IMPORT_SUFFIXES,
    DocumentContentConflictError,
    DocumentImportConflictError,
    DocumentImportPathError,
    DocumentIngestError,
    DocumentTooLargeError,
    KnowledgeBaseNotInitializedError,
    KnowledgeBaseProcessingError,
    UnsupportedDocumentFormatError,
    _ImportSnapshot,
)
from .knowledge_support import (
    _build_answer_context,
    _clear_directory_contents,
    _clip_context_text,
    _detected_format,
    _dict_items,
    _elapsed_ms,
    _ingest_error_summary,
    _load_source_metadata,
    _normalize_expected_origin_hash,
    _now_iso,
    _parse_json_list,
    _relation_row,
    _resolve_import_source,
    _restore_import_destination,
    _safe_markdown_destination,
    _same_import_source_state,
    _sha256_snapshot,
    _snapshot_filename,
    _source_id_for_relative_path,
    _source_record_for_relative_path,
    _stable_import_snapshot,
    _stable_markdown_name,
    _write_bytes_atomic,
    _write_json_atomic,
)


class KnowledgeStatusMixin:
    """Internal knowledge status domain implementation."""

    def _status_impl(self) -> dict[str, Any]:
        """返回 03 文档约定的知识库状态，不隐式初始化数据库。"""

        initialized = self.paths.sources_db.exists()
        result: dict[str, Any] = {
            "initialized": initialized,
            "sources": {
                "total": 0,
                "active": 0,
                "pending_graph": 0,
                "pending_rag": 0,
            },
            "graph": {"total_nodes": 0, "total_edges": 0, "total_groups": 0},
            "rag": {
                "total_chunks": 0,
                "total_vectors": 0,
                "faiss_healthy": False,
            },
        }
        if not initialized:
            return result

        source_connection = connect_sources(self.paths)
        try:
            source_counts = source_connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN exists_status = 'active' THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN exists_status = 'active' AND graph_status = 'pending'
                                THEN 1 ELSE 0 END) AS pending_graph,
                       SUM(CASE WHEN exists_status = 'active' AND rag_status = 'pending'
                                THEN 1 ELSE 0 END) AS pending_rag
                FROM sources
                """
            ).fetchone()
        finally:
            source_connection.close()
        result["sources"] = {
            "total": int(source_counts["total"] or 0),
            "active": int(source_counts["active"] or 0),
            "pending_graph": int(source_counts["pending_graph"] or 0),
            "pending_rag": int(source_counts["pending_rag"] or 0),
        }

        if self.paths.graph_db.exists():
            graph_connection = connect_graph(self.paths)
            try:
                graph_counts = graph_connection.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM nodes) AS total_nodes,
                           (SELECT COUNT(*) FROM edges) AS total_edges,
                           (SELECT COUNT(*) FROM groups) AS total_groups
                    """
                ).fetchone()
            finally:
                graph_connection.close()
            result["graph"] = {
                "total_nodes": int(graph_counts["total_nodes"]),
                "total_edges": int(graph_counts["total_edges"]),
                "total_groups": int(graph_counts["total_groups"]),
            }

        vector_ids: set[int] = set()
        if self.paths.rag_db.exists():
            rag_connection = connect_rag(self.paths)
            try:
                rag_counts = rag_connection.execute(
                    """
                    SELECT (SELECT COUNT(*) FROM chunks) AS total_chunks,
                           (SELECT COUNT(*) FROM embeddings) AS total_vectors
                    """
                ).fetchone()
                vector_ids = {
                    int(row["vector_id"])
                    for row in rag_connection.execute(
                        "SELECT vector_id FROM embeddings"
                    ).fetchall()
                }
            finally:
                rag_connection.close()
            result["rag"] = {
                "total_chunks": int(rag_counts["total_chunks"]),
                "total_vectors": int(rag_counts["total_vectors"]),
                "faiss_healthy": self._faiss_is_healthy(vector_ids),
            }
        return result

    def _get_config_impl(self) -> dict[str, Any]:
        """返回可安全回写的配置，不向客户端暴露显式 API 密钥。"""

        payload = self.settings.model_dump(mode="json")
        kemo = payload["kemo"]
        has_explicit_key = bool(self.settings.kemo.api_key)
        environment_name = self.settings.kemo.api_key_env.strip()
        has_environment_key = bool(
            os.getenv(environment_name, "").strip() if environment_name else ""
        )
        kemo["api_key"] = CONFIG_API_KEY_MASK if has_explicit_key else ""
        kemo["api_key_source"] = (
            "config"
            if has_explicit_key
            else "environment" if has_environment_key else "none"
        )
        return payload

    def _save_config_impl(self, data: dict[str, Any]) -> dict[str, Any]:
        """保存配置并校验；密钥掩码表示保留当前显式密钥。"""
        from .config import AppConfig

        candidate = deepcopy(data)
        raw_kemo = candidate.get("kemo")
        if isinstance(raw_kemo, dict):
            submitted_key = raw_kemo.get("api_key", CONFIG_API_KEY_MASK)
            if submitted_key == CONFIG_API_KEY_MASK:
                raw_kemo["api_key"] = self.settings.kemo.api_key
            raw_kemo.pop("api_key_source", None)

        new_config = AppConfig.model_validate(candidate)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary.write_text(
            new_config.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self.config_path)
        self.settings = new_config
        self.data_dir = self._data_dir_override or new_config.resolve_data_dir()
        self.external_dir = (
            self._external_dir_override or new_config.resolve_external_dir()
        )
        self.paths = get_database_paths(self.data_dir)
        self._logger = DailyTSVLogger(
            new_config.resolve_log_dir(),
            new_config.log_level,
        )
        return self.get_config()

    def _log_event(
        self,
        action: str,
        detail: str,
        elapsed_ms: int | float | str = "-",
        *,
        level: str = "INFO",
    ) -> None:
        try:
            self._logger.log(
                "knowledge_base",
                action,
                detail,
                elapsed_ms,
                level,
            )
        except Exception:
            pass

    def _require_initialized(self) -> None:
        if not self.paths.sources_db.exists():
            raise KnowledgeBaseNotInitializedError("知识库尚未初始化")

    def _require_available(self, *targets: str) -> None:
        self._require_initialized()
        conditions: list[str] = []
        if "graph" in targets:
            conditions.append("graph_status = 'processing'")
        if "rag" in targets:
            conditions.append("rag_status = 'processing'")
        connection = connect_sources(self.paths)
        try:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sources "
                    "WHERE exists_status = 'active' AND ("
                    + " OR ".join(conditions)
                    + ")"
                ).fetchone()[0]
            )
        finally:
            connection.close()
        if count:
            raise KnowledgeBaseProcessingError("知识库正在处理中，请稍后重试")

    def _faiss_is_healthy(self, vector_ids: set[int]) -> bool:
        if not self.paths.faiss_index.exists():
            return not vector_ids
        try:
            manager = FaissIndexManager(
                self.paths.faiss_index,
                self.settings.models.embedding_dimensions,
            )
            return manager.ids == vector_ids
        except Exception:
            return False
