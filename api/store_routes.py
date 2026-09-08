"""Web 和外部 API 共用的路由定义。"""

from __future__ import annotations

import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from core.knowledge_base import (
    MAX_IMPORT_BYTES,
    SUPPORTED_IMPORT_SUFFIXES,
    DocumentIngestError,
    DocumentTooLargeError,
    KnowledgeBaseService,
    UnsupportedDocumentFormatError,
)

from core.jobs import MaintenanceJobManager
from update import ApplicationUpdater, UpdateBlockedError, UpdatePermissionError
from core.config import load_config
from core.portable_store import (
    create_store_service,
    describe_store,
    federated_query,
    initialize_store,
    load_store_manifest,
)

from .deps import RuntimeContext, get_context, get_job_manager, get_service, get_updater
from .errors import success_response
from .schemas import (
    APIResponse,
    AnswerQueryRequest,
    ConfigSaveRequest,
    DocumentBatchDeleteRequest,
    DocumentContentUpdateRequest,
    GlobalQueryRequest,
    GraphQueryRequest,
    HybridQueryRequest,
    IngestRequest,
    OrganizeGraphRequest,
    RAGQueryRequest,
    StoreAnswerQueryRequest,
    StoreCacheClearRequest,
    StoreCacheKeyRequest,
    StoreCacheListRequest,
    StoreCleanupRecycleRequest,
    StoreDocumentListRequest,
    StoreDocumentBatchDeleteRequest,
    StoreDocumentContentUpdateRequest,
    StoreDeleteAllDocumentsRequest,
    StoreEdgeRequest,
    StoreFederatedQueryRequest,
    StoreFullGraphRequest,
    StoreGlobalQueryRequest,
    StoreGraphQueryRequest,
    StoreHybridQueryRequest,
    StoreImportPathRequest,
    StoreIngestRequest,
    StoreInitializeRequest,
    StoreJobRequest,
    StoreJobsListRequest,
    StoreNeighborhoodRequest,
    StoreNodeRequest,
    StoreOrganizeGraphRequest,
    StoreRAGQueryRequest,
    StoreRootRequest,
    StoreSourceRequest,
    StoreSourceDeleteRequest,
    StoreSourceStatusRequest,
    StoreSourceSyncRequest,
    StoreUploadRequest,
    StoreVisualizationPageRequest,
    UpdateApplyRequest,
    UploadRequest,
)


router = APIRouter()
Context = Annotated[RuntimeContext, Depends(get_context)]


def _store_operation(
    store_root: str,
    context: RuntimeContext,
    operation: Callable[[KnowledgeBaseService], Any],
) -> dict[str, Any]:
    """创建严格绑定的 Store 服务，并让每次响应都携带稳定身份。"""

    settings = load_config(context.config_path)
    manifest = load_store_manifest(store_root, settings=settings)
    service = create_store_service(
        store_root,
        settings=settings,
        config_path=context.config_path,
    )
    return {
        "store": asdict(manifest),
        "result": operation(service),
    }


@router.post("/stores/sources/sync", response_model=APIResponse)
def post_store_sources_sync(
    payload: StoreSourceSyncRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.sync_sources(
                [record.model_dump() for record in payload.records],
                ingest_after_sync=payload.ingest_after_sync,
            ),
        )
    )


@router.post("/stores/sources/status", response_model=APIResponse)
def post_store_sources_status(
    payload: StoreSourceStatusRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.list_synced_sources(
                source_type=payload.source_type,
                include_deleted=payload.include_deleted,
                page=payload.page,
                page_size=payload.page_size,
            ),
        )
    )


@router.post("/stores/sources/delete", response_model=APIResponse)
def post_store_sources_delete(
    payload: StoreSourceDeleteRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.delete_synced_sources(payload.source_uris),
        )
    )


@router.post("/stores/ingest", response_model=APIResponse)
def post_store_ingest(payload: StoreIngestRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.ingest(paths=payload.paths, mode=payload.mode),
        )
    )


@router.post("/stores/query/graph", response_model=APIResponse)
def post_store_query_graph(
    payload: StoreGraphQueryRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.query_graph(
                payload.query,
                depth=payload.depth,
                direction=payload.direction,
                confidence=payload.confidence,
                force=payload.force,
            ),
        )
    )


@router.post("/stores/query/rag", response_model=APIResponse)
def post_store_query_rag(payload: StoreRAGQueryRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.query_rag(
                payload.query,
                top_k=payload.top_k,
                threshold=payload.threshold,
                force=payload.force,
            ),
        )
    )


@router.post("/stores/query/hybrid", response_model=APIResponse)
def post_store_query_hybrid(
    payload: StoreHybridQueryRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.query_hybrid(
                payload.query,
                graph_depth=payload.graph_depth,
                rag_top_k=payload.rag_top_k,
                graph_confidence=payload.graph_confidence,
                rag_threshold=payload.rag_threshold,
                direction=payload.direction,
                force=payload.force,
            ),
        )
    )


@router.post("/stores/query/answer", response_model=APIResponse)
def post_store_query_answer(
    payload: StoreAnswerQueryRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.query_answer(
                payload.query,
                graph_depth=payload.graph_depth,
                rag_top_k=payload.rag_top_k,
                graph_confidence=payload.graph_confidence,
                rag_threshold=payload.rag_threshold,
                direction=payload.direction,
                force=payload.force,
            ),
        )
    )


@router.post("/stores/query/global", response_model=APIResponse)
def post_store_query_global(
    payload: StoreGlobalQueryRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.query_global(
                payload.query,
                top_k=payload.top_k,
                force=payload.force,
            ),
        )
    )


@router.post("/stores/query/federated", response_model=APIResponse)
def post_store_query_federated(
    payload: StoreFederatedQueryRequest,
    context: Context,
) -> dict:
    return success_response(
        federated_query(
            payload.store_roots,
            payload.query,
            mode=payload.mode,
            settings=load_config(context.config_path),
            config_path=context.config_path,
            force=payload.force,
            top_k=payload.top_k,
            graph_depth=payload.graph_depth,
        )
    )


@router.post("/stores/documents/list", response_model=APIResponse)
def post_store_documents_list(
    payload: StoreDocumentListRequest,
    context: Context,
) -> dict:
    status = None if payload.status == "all" else payload.status
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.list_documents(
                status=status,
                page=payload.page,
                page_size=payload.page_size,
            ),
        )
    )


@router.post("/stores/documents/content", response_model=APIResponse)
def post_store_document_content(
    payload: StoreSourceRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_document_content(payload.source_id),
        )
    )


@router.post("/stores/documents/update", response_model=APIResponse)
def post_store_document_update(
    payload: StoreDocumentContentUpdateRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.update_document_content(
                payload.source_id,
                payload.content,
                expected_content_hash=payload.expected_content_hash,
            ),
        )
    )


@router.post("/stores/documents/delete-batch", response_model=APIResponse)
def post_store_documents_delete_batch(
    payload: StoreDocumentBatchDeleteRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.delete_documents(payload.source_ids),
        )
    )


@router.post("/stores/documents/delete-all", response_model=APIResponse)
def post_store_documents_delete_all(
    payload: StoreDeleteAllDocumentsRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.delete_all_documents(),
        )
    )


@router.post("/stores/documents/delete", response_model=APIResponse)
def post_store_document_delete(
    payload: StoreSourceRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.delete_document(payload.source_id),
        )
    )


@router.post("/stores/nodes/delete", response_model=APIResponse)
def post_store_node_delete(payload: StoreNodeRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.delete_node(payload.node_id),
        )
    )


@router.post("/stores/nodes/get", response_model=APIResponse)
def post_store_node_get(payload: StoreNodeRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_node(payload.node_id),
        )
    )


@router.post("/stores/relations/get", response_model=APIResponse)
def post_store_relation_get(payload: StoreEdgeRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_relation(payload.edge_id),
        )
    )


@router.post("/stores/relations/delete", response_model=APIResponse)
def post_store_relation_delete(payload: StoreEdgeRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.delete_relation(payload.edge_id),
        )
    )


@router.post("/stores/graph/full", response_model=APIResponse)
def post_store_graph_full(payload: StoreFullGraphRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_full_graph(
                nodes_page=payload.nodes_page,
                nodes_page_size=payload.nodes_page_size,
            ),
        )
    )


@router.post("/stores/graph/visualization/meta", response_model=APIResponse)
def post_store_graph_visualization_meta(
    payload: StoreRootRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_graph_visualization_meta(),
        )
    )


@router.post("/stores/graph/visualization/nodes", response_model=APIResponse)
def post_store_graph_visualization_nodes(
    payload: StoreVisualizationPageRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.list_graph_visualization_nodes(
                page=payload.page,
                page_size=payload.page_size,
                expected_revision=payload.expected_revision,
            ),
        )
    )


@router.post("/stores/graph/visualization/edges", response_model=APIResponse)
def post_store_graph_visualization_edges(
    payload: StoreVisualizationPageRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.list_graph_visualization_edges(
                page=payload.page,
                page_size=payload.page_size,
                expected_revision=payload.expected_revision,
            ),
        )
    )


@router.post("/stores/graph/neighborhood", response_model=APIResponse)
def post_store_graph_neighborhood(
    payload: StoreNeighborhoodRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_graph_neighborhood(
                payload.node_id,
                depth=payload.depth,
                direction=payload.direction,
                limit=payload.limit,
                edge_limit=payload.edge_limit,
                expected_revision=payload.expected_revision,
            ),
        )
    )


@router.post("/stores/cache/list", response_model=APIResponse)
def post_store_cache_list(payload: StoreCacheListRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.list_cached_queries(
                page=payload.page,
                page_size=payload.page_size,
            ),
        )
    )


@router.post("/stores/cache/show", response_model=APIResponse)
def post_store_cache_show(payload: StoreCacheKeyRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_cached_query(payload.cache_key),
        )
    )


@router.post("/stores/cache/clear", response_model=APIResponse)
def post_store_cache_clear(
    payload: StoreCacheClearRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.clear_search_cache(payload.stale_only),
        )
    )


@router.post("/stores/maintenance/organize-graph", response_model=APIResponse)
def post_store_organize_graph(
    payload: StoreOrganizeGraphRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.organize_graph(
                use_llm=payload.use_llm,
                summarize=payload.summarize,
            ),
        )
    )


@router.post(
    "/stores/maintenance/rebuild-knowledge-base",
    response_model=APIResponse,
)
def post_store_rebuild_knowledge_base(
    payload: StoreRootRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.rebuild_knowledge_base(),
        )
    )


@router.post("/stores/maintenance/rebuild-all", response_model=APIResponse)
def post_store_rebuild_all(payload: StoreRootRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.rebuild_all(),
        )
    )


@router.post("/stores/maintenance/summarize", response_model=APIResponse)
def post_store_summarize(payload: StoreRootRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.generate_group_summaries(),
        )
    )


@router.post("/stores/maintenance/cleanup-recycle", response_model=APIResponse)
def post_store_cleanup_recycle(
    payload: StoreCleanupRecycleRequest,
    context: Context,
) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.cleanup_recycle(force=payload.force),
        )
    )


@router.post("/stores/jobs/list", response_model=APIResponse)
def post_store_jobs_list(payload: StoreJobsListRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: {"jobs": service.list_jobs(limit=payload.limit)},
        )
    )


@router.post("/stores/jobs/get", response_model=APIResponse)
def post_store_job_get(payload: StoreJobRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.get_job(payload.job_id),
        )
    )

