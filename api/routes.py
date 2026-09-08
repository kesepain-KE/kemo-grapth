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
from .store_routes import _store_operation, router as store_router
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
Service = Annotated[KnowledgeBaseService, Depends(get_service)]
Jobs = Annotated[MaintenanceJobManager, Depends(get_job_manager)]
Updater = Annotated[ApplicationUpdater, Depends(get_updater)]
Context = Annotated[RuntimeContext, Depends(get_context)]


async def _stage_uploaded_document(
    file: UploadFile,
    temporary_dir: Path,
) -> tuple[str, Path]:
    """Validate and stream one multipart document into a short-lived staging file."""

    filename = _validated_upload_filename(file.filename)
    suffix = Path(filename).suffix.casefold()
    if suffix not in SUPPORTED_IMPORT_SUFFIXES:
        raise UnsupportedDocumentFormatError(
            f"不支持的文档格式：{suffix or '<无扩展名>'}"
        )

    staged = temporary_dir / filename
    total = 0
    try:
        with staged.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_IMPORT_BYTES:
                    raise DocumentTooLargeError(
                        f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MB 上限：{filename}"
                    )
                handle.write(chunk)
    finally:
        await file.close()
    return filename, staged


def _raise_import_failure(result: dict[str, Any]) -> None:
    if result.get("ingest_status") == "failed":
        raise DocumentIngestError(
            str(result.get("ingest_error") or "文档已转换，但知识库整理失败")
        )


@router.get("/status", response_model=APIResponse)
def get_status(service: Service) -> dict:
    return success_response(service.status())


@router.post("/ingest", response_model=APIResponse)
def post_ingest(payload: IngestRequest, service: Service) -> dict:
    return success_response(service.ingest(paths=payload.paths, mode=payload.mode))


@router.post("/query/graph", response_model=APIResponse)
def post_query_graph(
    payload: GraphQueryRequest,
    service: Service,
    force: Annotated[bool, Query(description="跳过缓存并刷新结果")] = False,
) -> dict:
    return success_response(
        service.query_graph(
            payload.query,
            depth=payload.depth,
            direction=payload.direction,
            confidence=payload.confidence,
            force=force,
        )
    )


@router.post("/query/rag", response_model=APIResponse)
def post_query_rag(
    payload: RAGQueryRequest,
    service: Service,
    force: Annotated[bool, Query(description="跳过缓存并刷新结果")] = False,
) -> dict:
    return success_response(
        service.query_rag(
            payload.query,
            top_k=payload.top_k,
            threshold=payload.threshold,
            force=force,
        )
    )


@router.post("/query/hybrid", response_model=APIResponse)
def post_query_hybrid(
    payload: HybridQueryRequest,
    service: Service,
    force: Annotated[bool, Query(description="跳过缓存并刷新结果")] = False,
) -> dict:
    return success_response(
        service.query_hybrid(
            payload.query,
            graph_depth=payload.graph_depth,
            rag_top_k=payload.rag_top_k,
            graph_confidence=payload.graph_confidence,
            rag_threshold=payload.rag_threshold,
            force=force,
        )
    )


@router.post("/query/answer", response_model=APIResponse)
def post_query_answer(
    payload: AnswerQueryRequest,
    service: Service,
    force: Annotated[bool, Query(description="跳过缓存并刷新结果")] = False,
) -> dict:
    return success_response(
        service.query_answer(
            payload.query,
            graph_depth=payload.graph_depth,
            rag_top_k=payload.rag_top_k,
            graph_confidence=payload.graph_confidence,
            rag_threshold=payload.rag_threshold,
            force=force,
        )
    )


@router.post("/query/global", response_model=APIResponse)
def post_query_global(
    payload: GlobalQueryRequest,
    service: Service,
    force: Annotated[bool, Query(description="跳过缓存并刷新结果")] = False,
) -> dict:
    return success_response(
        service.query_global(
            payload.query,
            top_k=payload.top_k,
            force=force,
        )
    )


# ── 搜索缓存与历史 ──


@router.get("/search/cache", response_model=APIResponse)
def get_search_cache(
    service: Service,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    return success_response(service.list_cached_queries(page, page_size))


@router.get("/search/cache/{cache_key}", response_model=APIResponse)
def get_search_cache_detail(cache_key: str, service: Service) -> dict:
    return success_response(service.get_cached_query(cache_key))


@router.delete("/search/cache", response_model=APIResponse)
def delete_search_cache(
    service: Service,
    stale_only: Annotated[bool, Query(description="只删除已过期记录")] = False,
) -> dict:
    return success_response(service.clear_search_cache(stale_only))


# ── 文档管理 ──


@router.get("/documents", response_model=APIResponse)
def get_documents(
    service: Service,
    status: Annotated[str | None, Query(description="active / pending / all")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    return success_response(
        service.list_documents(status=status, page=page, page_size=page_size)
    )


@router.get("/documents/{source_id}/content", response_model=APIResponse)
def get_document_content(source_id: str, service: Service) -> dict:
    return success_response(service.get_document_content(source_id))


@router.put("/documents/{source_id}/content", response_model=APIResponse)
def put_document_content(
    source_id: str,
    payload: DocumentContentUpdateRequest,
    service: Service,
) -> dict:
    return success_response(
        service.update_document_content(
            source_id,
            payload.content,
            expected_content_hash=payload.expected_content_hash,
        )
    )


@router.post("/documents/delete-batch", response_model=APIResponse)
def post_delete_documents(
    payload: DocumentBatchDeleteRequest,
    service: Service,
) -> dict:
    return success_response(service.delete_documents(payload.source_ids))


@router.delete("/documents", response_model=APIResponse)
def delete_all_documents(
    service: Service,
    confirm: Annotated[
        Literal["delete-all"],
        Query(description="破坏性操作确认值，必须为 delete-all"),
    ],
) -> dict:
    if confirm != "delete-all":  # Literal 已校验；保留显式防线。
        raise ValueError("清空文档必须显式确认 delete-all")
    return success_response(service.delete_all_documents())


@router.delete("/documents/{source_id}", response_model=APIResponse)
def delete_document(source_id: str, service: Service) -> dict:
    return success_response(service.delete_document(source_id))


@router.post("/upload", response_model=APIResponse)
def post_upload(payload: UploadRequest, service: Service) -> dict:
    return success_response(service.upload_file(payload.content, payload.filename))


@router.post("/import", response_model=APIResponse)
async def post_import(
    service: Service,
    file: Annotated[UploadFile, File(description="要转换并导入的本地文档")],
    ingest_after_import: Annotated[
        bool,
        Query(alias="ingest", description="转换后是否立即整理图谱与 RAG"),
    ] = True,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="kemo-graph-import-") as temporary_dir:
        filename, staged = await _stage_uploaded_document(file, Path(temporary_dir))
        result = service.import_document(
            staged,
            ingest_after_import=ingest_after_import,
            _original_identity=f"upload://{filename}",
        )
        _raise_import_failure(result)
    return success_response(result)


# ── 节点管理 ──


@router.delete("/nodes/{node_id}", response_model=APIResponse)
def delete_node(node_id: str, service: Service) -> dict:
    return success_response(service.delete_node(node_id))


@router.get("/nodes/{node_id}", response_model=APIResponse)
def get_node(node_id: str, service: Service) -> dict:
    return success_response(service.get_node(node_id))


@router.get("/relations/{edge_id}", response_model=APIResponse)
def get_relation(edge_id: str, service: Service) -> dict:
    return success_response(service.get_relation(edge_id))


@router.delete("/relations/{edge_id}", response_model=APIResponse)
def delete_relation(edge_id: str, service: Service) -> dict:
    return success_response(service.delete_relation(edge_id))


# ── 图谱全量 ──


@router.get("/graph", response_model=APIResponse)
@router.get("/graph/full", response_model=APIResponse, include_in_schema=False)
def get_full_graph(
    service: Service,
    nodes_page: Annotated[int | None, Query(ge=1)] = None,
    nodes_page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict:
    return success_response(
        service.get_full_graph(
            nodes_page=nodes_page,
            nodes_page_size=nodes_page_size,
        )
    )


@router.get("/graph/visualization/meta", response_model=APIResponse)
def get_graph_visualization_meta(service: Service) -> dict:
    return success_response(service.get_graph_visualization_meta())


@router.get("/graph/visualization/nodes", response_model=APIResponse)
def get_graph_visualization_nodes(
    service: Service,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=5000)] = 1000,
    expected_revision: Annotated[str | None, Query(min_length=64, max_length=64)] = None,
) -> dict:
    return success_response(
        service.list_graph_visualization_nodes(
            page=page,
            page_size=page_size,
            expected_revision=expected_revision,
        )
    )


@router.get("/graph/visualization/edges", response_model=APIResponse)
def get_graph_visualization_edges(
    service: Service,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=10000)] = 2000,
    expected_revision: Annotated[str | None, Query(min_length=64, max_length=64)] = None,
) -> dict:
    return success_response(
        service.list_graph_visualization_edges(
            page=page,
            page_size=page_size,
            expected_revision=expected_revision,
        )
    )


@router.get("/graph/neighborhood/{node_id}", response_model=APIResponse)
def get_graph_neighborhood(
    node_id: str,
    service: Service,
    depth: Annotated[int, Query(ge=1, le=10)] = 2,
    direction: Annotated[
        Literal["forward", "backward", "both"],
        Query(),
    ] = "both",
    limit: Annotated[int, Query(ge=1, le=10000)] = 2000,
    edge_limit: Annotated[int, Query(ge=1, le=50000)] = 10000,
    expected_revision: Annotated[str | None, Query(min_length=64, max_length=64)] = None,
) -> dict:
    return success_response(
        service.get_graph_neighborhood(
            node_id,
            depth=depth,
            direction=direction,
            limit=limit,
            edge_limit=edge_limit,
            expected_revision=expected_revision,
        )
    )


# ── 配置管理 ──


@router.get("/config", response_model=APIResponse)
def get_config(service: Service) -> dict:
    return success_response(service.get_config())


@router.put("/config", response_model=APIResponse)
def put_config(payload: ConfigSaveRequest, service: Service) -> dict:
    return success_response(service.save_config(payload.model_dump()))


# ── 运维 ──


@router.get("/update/status", response_model=APIResponse)
def get_update_status(updater: Updater) -> dict:
    return success_response(updater.status())


@router.post("/update/check", response_model=APIResponse)
def post_update_check(updater: Updater) -> dict:
    return success_response(updater.check())


@router.post("/update/apply", response_model=APIResponse)
def post_update_apply(
    request: Request,
    updater: Updater,
    jobs: Jobs,
    payload: UpdateApplyRequest | None = None,
    force: Annotated[bool, Query(description="允许同版本强制重新安装")] = False,
) -> dict:
    host = request.client.host if request.client is not None else ""
    if not _is_loopback_host(host):
        raise UpdatePermissionError("应用更新只允许从本机访问")
    force_requested = bool(force or (payload and payload.force))
    status = updater.status()
    same_version_force = bool(force_requested and status.get("force_update_available"))
    if not status.get("update_available") and not same_version_force:
        raise UpdateBlockedError("请先检查更新；当前没有可安装的新版本")
    can_apply = bool(status.get("can_apply")) or bool(
        same_version_force and status.get("can_force_apply")
    )
    if not can_apply:
        reasons = "；".join(status.get("blocking_reasons") or [])
        raise UpdateBlockedError(reasons or "当前安装状态不允许更新")
    if force_requested:
        return success_response(jobs.submit("update", force=True))
    # Preserve the original no-options call shape for existing API adapters
    # and test doubles; MaintenanceJobManager defaults force to False.
    return success_response(jobs.submit("update"))


def _is_loopback_host(host: str) -> bool:
    import ipaddress

    normalized = host.strip().casefold()
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@router.post("/jobs/ingest", response_model=APIResponse)
def post_ingest_job(payload: IngestRequest, jobs: Jobs) -> dict:
    return success_response(
        jobs.submit("ingest", paths=payload.paths, mode=payload.mode)
    )


@router.post("/jobs/summarize", response_model=APIResponse)
def post_summarize_job(jobs: Jobs) -> dict:
    """在后台生成节点群摘要，供 Web 端持续追踪执行状态。"""

    return success_response(jobs.submit("summarize"))


@router.post("/maintenance/organize-graph", response_model=APIResponse)
def post_organize_graph(payload: OrganizeGraphRequest, jobs: Jobs) -> dict:
    return success_response(
        jobs.submit(
            "organize_graph",
            use_llm=payload.use_llm,
            summarize=payload.summarize,
        )
    )


@router.post("/maintenance/rebuild-knowledge-base", response_model=APIResponse)
def post_rebuild_knowledge_base(jobs: Jobs) -> dict:
    return success_response(jobs.submit("rebuild_knowledge_base"))


@router.post("/maintenance/rebuild-all", response_model=APIResponse)
def post_rebuild_all(jobs: Jobs) -> dict:
    return success_response(jobs.submit("rebuild_all"))


@router.get("/jobs", response_model=APIResponse)
def get_jobs(
    jobs: Jobs,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    status: Annotated[str | None, Query()] = None,
) -> dict:
    return success_response({"jobs": jobs.list(limit=limit, status=status)})


@router.get("/jobs/{job_id}", response_model=APIResponse)
def get_job(job_id: str, jobs: Jobs) -> dict:
    return success_response(jobs.get(job_id))


@router.post("/maintenance/summarize", response_model=APIResponse)
def post_summarize(service: Service) -> dict:
    return success_response(service.generate_group_summaries())


@router.post("/maintenance/cleanup-recycle", response_model=APIResponse)
def post_cleanup_recycle(service: Service) -> dict:
    return success_response(service.cleanup_recycle())


@router.delete("/maintenance/recycle", response_model=APIResponse)
def delete_recycle(service: Service) -> dict:
    """永久清空回收站；调用方应在执行前向用户二次确认。"""

    return success_response(service.cleanup_recycle(force=True))


# ── 绝对路径分布式 Store（组织级调用面） ──
#
# store_root 一律放在 JSON 请求体内，避免绝对路径进入 URL、代理访问日志或路由编码。
# 原有单知识库端点完整保留；本组端点显式选择独立 kemo-graph-storage。


@router.post("/stores/initialize", response_model=APIResponse)
def post_store_initialize(
    payload: StoreInitializeRequest,
    context: Context,
) -> dict:
    settings = load_config(context.config_path)
    return success_response(
        initialize_store(
            payload.store_root,
            scope=payload.scope,
            owner_id=payload.owner_id,
            display_name=payload.display_name,
            settings=settings,
        )
    )


@router.post("/stores/info", response_model=APIResponse)
def post_store_info(payload: StoreRootRequest, context: Context) -> dict:
    return success_response(
        describe_store(
            payload.store_root,
            settings=load_config(context.config_path),
            config_path=context.config_path,
        )
    )


@router.post("/stores/status", response_model=APIResponse)
def post_store_status(payload: StoreRootRequest, context: Context) -> dict:
    return success_response(
        _store_operation(payload.store_root, context, lambda service: service.status())
    )


@router.post("/stores/import-path", response_model=APIResponse)
def post_store_import_path(
    payload: StoreImportPathRequest,
    context: Context,
) -> dict:
    if not Path(payload.path).expanduser().is_absolute():
        raise ValueError("Store import-path 只接受绝对文件路径")
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.import_document(
                payload.path,
                ingest_after_import=payload.ingest_after_import,
                expected_origin_hash=payload.expected_origin_hash,
            ),
        )
    )


@router.post("/stores/import", response_model=APIResponse)
async def post_store_import(
    context: Context,
    file: Annotated[UploadFile, File(description="要转换并导入的本地文档")],
    store_root: Annotated[
        str,
        Form(description="portable Store 绝对路径；使用表单字段避免进入 URL 日志"),
    ],
    ingest_after_import: Annotated[
        bool,
        Query(alias="ingest", description="转换后是否立即整理图谱与 RAG"),
    ] = False,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="kemo-graph-store-import-") as temporary_dir:
        filename, staged = await _stage_uploaded_document(file, Path(temporary_dir))
        data = _store_operation(
            store_root,
            context,
            lambda service: service.import_document(
                staged,
                ingest_after_import=ingest_after_import,
                _original_identity=f"upload://{filename}",
            ),
        )
        result = data.get("result")
        if isinstance(result, dict):
            _raise_import_failure(result)
    return success_response(data)


@router.post("/stores/upload", response_model=APIResponse)
def post_store_upload(payload: StoreUploadRequest, context: Context) -> dict:
    return success_response(
        _store_operation(
            payload.store_root,
            context,
            lambda service: service.upload_file(payload.content, payload.filename),
        )
    )




def _validated_upload_filename(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("上传文件名不能为空")
    filename = value.strip()
    if filename != Path(filename).name or any(
        part in filename for part in ("/", "\\", "..")
    ):
        raise ValueError("上传文件名不能包含路径")
    return filename

router.include_router(store_router)
