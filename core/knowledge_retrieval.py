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
    _public_chat,
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


class KnowledgeRetrievalMixin:
    """Internal knowledge retrieval domain implementation."""

    def _query_graph_impl(
        self,
        query: str,
        *,
        depth: int = 3,
        direction: str = "both",
        confidence: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """旧私有入口兼容层；检索实现位于 :class:`RetrievalService`。"""
        return self.retrieval.query_graph(
            query,
            depth=depth,
            direction=direction,
            confidence=confidence,
            force=force,
        )

    def _query_rag_impl(
        self,
        query: str,
        *,
        top_k: int | None = None,
        threshold: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_rag(
            query,
            top_k=top_k,
            threshold=threshold,
            force=force,
        )

    def _query_hybrid_impl(
        self,
        query: str,
        *,
        graph_depth: int = 3,
        rag_top_k: int | None = None,
        graph_confidence: float | None = None,
        rag_threshold: float | None = None,
        direction: str = "both",
        force: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_hybrid(
            query,
            graph_depth=graph_depth,
            rag_top_k=rag_top_k,
            graph_confidence=graph_confidence,
            rag_threshold=rag_threshold,
            direction=direction,
            force=force,
        )

    def _query_answer_impl(
        self,
        query: str,
        *,
        graph_depth: int = 3,
        rag_top_k: int | None = None,
        graph_confidence: float | None = None,
        rag_threshold: float | None = None,
        direction: str = "both",
        force: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_answer(
            query,
            graph_depth=graph_depth,
            rag_top_k=rag_top_k,
            graph_confidence=graph_confidence,
            rag_threshold=rag_threshold,
            direction=direction,
            force=force,
        )

    def _query_answer_uncached(
        self,
        normalized_query: str,
        *,
        graph_depth: int,
        rag_top_k: int | None,
        graph_confidence: float | None,
        rag_threshold: float | None,
        direction: str,
    ) -> dict[str, Any]:
        return self.retrieval.query_answer_uncached(
            normalized_query,
            graph_depth=graph_depth,
            rag_top_k=rag_top_k,
            graph_confidence=graph_confidence,
            rag_threshold=rag_threshold,
            direction=direction,
        )

    def _query_global_impl(
        self,
        query: str,
        top_k: int = 5,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_global(query, top_k=top_k, force=force)

    def _query_global_uncached(
        self,
        normalized_query: str,
        top_k: int,
    ) -> dict[str, Any]:
        return self.retrieval.query_global_uncached(normalized_query, top_k)

    def _cached_query(
        self,
        query_mode: str,
        query: str,
        params: dict[str, Any],
        execute: Callable[[], dict[str, Any]],
        *,
        force: bool,
    ) -> dict[str, Any]:
        """兼容私有入口；缓存实现已迁移到 RetrievalService。"""

        return self.retrieval.cached_query(
            query_mode,
            query,
            params,
            execute,
            force=force,
        )

    def _list_cached_queries_impl(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self.retrieval.list_cached_queries(page, page_size)

    def _get_cached_query_impl(self, cache_key: str) -> dict[str, Any]:
        return self.retrieval.get_cached_query(cache_key)

    def _clear_search_cache_impl(self, stale_only: bool = False) -> dict[str, Any]:
        return self.retrieval.clear_search_cache(stale_only)

    def _new_rag_engine(self) -> RAGEngine:
        from . import knowledge_base

        return knowledge_base.RAGEngine(self.data_dir, settings=self.settings)

    def _new_hybrid_engine(self) -> HybridEngine:
        from . import knowledge_base

        return knowledge_base.HybridEngine(self.data_dir, settings=self.settings)

    def _chat(self, system: str, user: str) -> str:
        return _public_chat(system, user, settings=self.settings)
