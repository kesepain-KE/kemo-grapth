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
from .knowledge_documents import KnowledgeDocumentsMixin
from .knowledge_graph import KnowledgeGraphMixin
from .knowledge_models import (
    CONFIG_API_KEY_MASK,
    MAX_IMPORT_BYTES,
    SUPPORTED_IMPORT_SUFFIXES,
    DocumentContentConflictError,
    DocumentImportConflictError,
    DocumentImportError,
    DocumentImportPathError,
    DocumentIngestError,
    DocumentTooLargeError,
    KnowledgeBaseNotInitializedError,
    KnowledgeBaseProcessingError,
    UnsupportedDocumentFormatError,
    _ImportSnapshot,
)
from .knowledge_retrieval import KnowledgeRetrievalMixin
from .knowledge_status import KnowledgeStatusMixin




class KnowledgeBaseService(KnowledgeDocumentsMixin, KnowledgeGraphMixin, KnowledgeRetrievalMixin, KnowledgeStatusMixin):
    """将入口层请求转换为稳定的 core 调用。"""

    def __init__(
        self,
        *,
        settings: AppConfig | None = None,
        data_dir: Path | str | None = None,
        external_dir: Path | str | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        self.settings = settings or load_config()
        self.config_path = (
            Path(config_path or DEFAULT_CONFIG_PATH).expanduser().resolve()
        )
        self._data_dir_override = (
            Path(data_dir).expanduser().resolve() if data_dir is not None else None
        )
        self._external_dir_override = (
            Path(external_dir).expanduser().resolve()
            if external_dir is not None
            else None
        )
        self.data_dir = self._data_dir_override or self.settings.resolve_data_dir()
        self.external_dir = (
            self._external_dir_override or self.settings.resolve_external_dir()
        )
        self.paths = get_database_paths(self.data_dir)
        self._logger = DailyTSVLogger(
            self.settings.resolve_log_dir(),
            self.settings.log_level,
        )
        # 对外仍由本类提供稳定门面；领域服务只持有 owner 引用，
        # 因而 save_config() 更新配置/路径后无需重建服务对象。
        self.documents = DocumentService(self)
        self.graph = GraphService(self)
        self.retrieval = RetrievalService(self)
        self.maintenance = MaintenanceService(self)

    # ------------------------------------------------------------------
    # 兼容门面：公开方法签名保持不变，具体入口按领域委托到 services。
    # ------------------------------------------------------------------
    def list_documents(
        self,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return self.documents.list_documents(status, page, page_size)

    def get_document_content(self, source_id: str) -> dict[str, Any]:
        return self.documents.get_document_content(source_id)

    def update_document_content(
        self,
        source_id: str,
        content: str,
        *,
        expected_content_hash: str | None = None,
    ) -> dict[str, Any]:
        return self.documents.update_document_content(
            source_id,
            content,
            expected_content_hash=expected_content_hash,
        )

    def import_document(
        self,
        source_path: Path | str,
        *,
        ingest_after_import: bool = True,
        expected_origin_hash: str | None = None,
        _original_identity: str | None = None,
    ) -> dict[str, Any]:
        return self.documents.import_document(
            source_path,
            ingest_after_import=ingest_after_import,
            expected_origin_hash=expected_origin_hash,
            _original_identity=_original_identity,
        )

    def upload_file(self, content: str, filename: str) -> dict[str, Any]:
        return self.documents.upload_file(content, filename)

    def sync_sources(
        self,
        records: Sequence[dict[str, Any]],
        *,
        ingest_after_sync: bool = False,
    ) -> dict[str, Any]:
        return self.documents.sync_sources(
            records,
            ingest_after_sync=ingest_after_sync,
        )

    def list_synced_sources(
        self,
        *,
        source_type: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        return self.documents.list_synced_sources(
            source_type=source_type,
            include_deleted=include_deleted,
            page=page,
            page_size=page_size,
        )

    def delete_synced_sources(self, source_uris: Sequence[str]) -> dict[str, Any]:
        return self.documents.delete_synced_sources(source_uris)

    def get_node(self, node_id: str) -> dict[str, Any]:
        return self.graph.get_node(node_id)

    def get_relation(self, edge_id: str) -> dict[str, Any]:
        return self.graph.get_relation(edge_id)

    def delete_relation(self, edge_id: str) -> dict[str, Any]:
        return self.graph.delete_relation(edge_id)

    def delete_node(self, node_id: str) -> dict[str, Any]:
        return self.graph.delete_node(node_id)

    def get_full_graph(
        self,
        nodes_page: int | None = None,
        nodes_page_size: int = 100,
    ) -> dict[str, Any]:
        return self.graph.get_full_graph(nodes_page, nodes_page_size)

    def get_graph_visualization_meta(self) -> dict[str, Any]:
        return self.graph.get_visualization_meta()

    def list_graph_visualization_nodes(
        self,
        *,
        page: int = 1,
        page_size: int = 1000,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        return self.graph.list_visualization_nodes(
            page=page,
            page_size=page_size,
            expected_revision=expected_revision,
        )

    def list_graph_visualization_edges(
        self,
        *,
        page: int = 1,
        page_size: int = 2000,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        return self.graph.list_visualization_edges(
            page=page,
            page_size=page_size,
            expected_revision=expected_revision,
        )

    def get_graph_neighborhood(
        self,
        node_id: str,
        *,
        depth: int = 2,
        direction: GraphDirection = "both",
        limit: int = 2000,
        edge_limit: int = 10000,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        return self.graph.get_neighborhood(
            node_id,
            depth=depth,
            direction=direction,
            limit=limit,
            edge_limit=edge_limit,
            expected_revision=expected_revision,
        )

    def organize_graph(
        self,
        *,
        use_llm: bool = True,
        summarize: bool = True,
    ) -> dict[str, Any]:
        return self.maintenance.organize_graph(
            use_llm=use_llm,
            summarize=summarize,
        )

    def rebuild_knowledge_base(
        self,
        *,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        return self.maintenance.rebuild_knowledge_base(progress=progress)

    def rebuild_all(
        self,
        *,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        return self.maintenance.rebuild_all(progress=progress)

    def list_jobs(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return self.maintenance.list_jobs(limit=limit)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.maintenance.get_job(job_id)

    def cleanup_recycle(self, *, force: bool = False) -> dict[str, Any]:
        return self.maintenance.cleanup_recycle(force=force)

    def generate_group_summaries(self, *, force: bool = False) -> dict[str, Any]:
        return self.maintenance.generate_group_summaries(force=force)

    def query_graph(
        self,
        query: str,
        *,
        depth: int = 3,
        direction: str = "both",
        confidence: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_graph(
            query,
            depth=depth,
            direction=direction,
            confidence=confidence,
            force=force,
        )

    def query_rag(
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

    def query_hybrid(
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

    def query_answer(
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

    def query_global(
        self,
        query: str,
        top_k: int = 5,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.retrieval.query_global(query, top_k=top_k, force=force)

    def list_cached_queries(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return self.retrieval.list_cached_queries(page, page_size)

    def get_cached_query(self, cache_key: str) -> dict[str, Any]:
        return self.retrieval.get_cached_query(cache_key)

    def clear_search_cache(self, stale_only: bool = False) -> dict[str, Any]:
        return self.retrieval.clear_search_cache(stale_only)

    def delete_document(self, source_id: str) -> dict[str, Any]:
        return self.documents.delete_document(source_id)

    def delete_documents(self, source_ids: Sequence[str]) -> dict[str, Any]:
        return self.documents.delete_documents(source_ids)

    def delete_all_documents(self) -> dict[str, Any]:
        return self.documents.delete_all_documents()

    def status(self) -> dict[str, Any]:
        return self.maintenance.status()

    def get_config(self) -> dict[str, Any]:
        return self.maintenance.get_config()

    def save_config(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.maintenance.save_config(data)
















































    # Provider 工厂是领域服务的最小可替换上下文。它们保留旧的
    # ``patch("core.knowledge_base.*Engine")`` 测试/集成注入点，同时不把
    # provider 依赖重新扩散到 API、CLI 或 Web。
