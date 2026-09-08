"""kemo-graph 命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.config import DEFAULT_CONFIG_PATH, load_config
from core.graph_engine import GraphQueryError
from core.graph_organizer import GraphOrganizerError
from core.ingestor import DocumentNotFoundError, IngestError, RecycleConflictError
from core.knowledge_base import (
    DocumentImportError,
    DocumentImportPathError,
    DocumentImportConflictError,
    DocumentTooLargeError,
    KnowledgeBaseNotInitializedError,
    KnowledgeBaseProcessingError,
    KnowledgeBaseService,
    UnsupportedDocumentFormatError,
)
from core.rag_engine import RAGQueryError
from core.rebuilder import RebuildError
from core.jobs import JobNotFoundError
from core.portable_store import (
    PortableStoreAccessError,
    PortableStoreError,
    PortableStoreNotInitializedError,
    create_store_service,
    describe_store,
    federated_query,
    initialize_store,
)
from update import (
    ApplicationUpdater,
    UpdateBlockedError,
    UpdateError,
    UpdatePermissionError,
    UpdateSourceError,
    read_local_version,
)
from provider.tools.document_tools import DocumentConversionError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="kemo-graph 知识库命令行工具")
    parser.add_argument("--data-dir", help="知识库数据目录")
    parser.add_argument("--external-dir", help="Markdown 文档目录")
    parser.add_argument("--config", help="config.json 路径")
    parser.add_argument(
        "--store-root",
        help="绝对知识位置；数据固定写入其 kemo-graph-storage 目录",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version", help="查看当前应用版本")
    subparsers.add_parser("update-check", help="从 GitHub 检查应用更新")
    update_parser = subparsers.add_parser(
        "update", help="安全下载并安装 GitHub main 最新版本"
    )
    update_parser.add_argument(
        "--force",
        action="store_true",
        help="远端与本地同版本时也重新安装当前提交",
    )

    ingest_parser = subparsers.add_parser("ingest", help="整理 Markdown 文档")
    ingest_parser.add_argument("paths", nargs="*", help="要处理的 Markdown 路径")
    ingest_parser.add_argument(
        "--mode",
        choices=("graph", "rag", "both"),
        default="both",
    )

    import_parser = subparsers.add_parser("import", help="转换并导入本地文档")
    import_parser.add_argument(
        "path",
        help="受支持本地文档的绝对路径（PDF、Office、邮件、文本、表格或结构化数据）",
    )
    import_parser.add_argument(
        "--no-ingest",
        action="store_true",
        help="只转换和注册，不立即构建 Graph/RAG",
    )

    graph_parser = subparsers.add_parser("query-graph", help="执行图谱检索")
    graph_parser.add_argument("query")
    graph_parser.add_argument("--depth", type=int, default=3)
    graph_parser.add_argument(
        "--direction",
        choices=("forward", "backward", "both"),
        default="both",
    )
    graph_parser.add_argument("--confidence", type=float)
    graph_parser.add_argument(
        "--force", action="store_true", help="跳过缓存并刷新结果"
    )

    rag_parser = subparsers.add_parser("query-rag", help="执行向量检索")
    rag_parser.add_argument("query")
    rag_parser.add_argument("--top-k", type=int)
    rag_parser.add_argument("--threshold", type=float)
    rag_parser.add_argument(
        "--force", action="store_true", help="跳过缓存并刷新结果"
    )

    hybrid_parser = subparsers.add_parser("query-hybrid", help="执行混合检索")
    hybrid_parser.add_argument("query")
    hybrid_parser.add_argument("--graph-depth", type=int, default=3)
    hybrid_parser.add_argument("--rag-top-k", type=int)
    hybrid_parser.add_argument("--graph-confidence", type=float)
    hybrid_parser.add_argument("--rag-threshold", type=float)
    hybrid_parser.add_argument(
        "--direction",
        choices=("forward", "backward", "both"),
        default="both",
    )
    hybrid_parser.add_argument(
        "--force", action="store_true", help="跳过缓存并刷新结果"
    )

    answer_parser = subparsers.add_parser(
        "query-answer", help="使用混合知识上下文生成 LLM 回答"
    )
    answer_parser.add_argument("query")
    answer_parser.add_argument("--graph-depth", type=int, default=3)
    answer_parser.add_argument("--rag-top-k", type=int)
    answer_parser.add_argument("--graph-confidence", type=float)
    answer_parser.add_argument("--rag-threshold", type=float)
    answer_parser.add_argument(
        "--direction",
        choices=("forward", "backward", "both"),
        default="both",
    )
    answer_parser.add_argument("--force", action="store_true")

    global_parser = subparsers.add_parser(
        "query-global",
        help="基于节点群总结执行知识库全局检索",
    )
    global_parser.add_argument("query")
    global_parser.add_argument("--top-k", type=int, default=5)
    global_parser.add_argument(
        "--force", action="store_true", help="跳过缓存并刷新结果"
    )

    cache_list_parser = subparsers.add_parser(
        "cache-list", help="分页列出搜索缓存与历史"
    )
    cache_list_parser.add_argument("--page", type=int, default=1)
    cache_list_parser.add_argument("--page-size", type=int, default=20)
    cache_show_parser = subparsers.add_parser("cache-show", help="查看缓存详情")
    cache_show_parser.add_argument("cache_key")
    cache_clear_parser = subparsers.add_parser("cache-clear", help="清理搜索缓存")
    cache_clear_parser.add_argument(
        "--stale", action="store_true", help="只清理与当前知识库不匹配的记录"
    )

    subparsers.add_parser("status", help="查看知识库状态")
    delete_parser = subparsers.add_parser("delete-doc", help="删除文档")
    delete_parser.add_argument("source_id")
    list_docs_parser = subparsers.add_parser("list-docs", help="分页列出文档")
    list_docs_parser.add_argument("--page", type=int, default=1)
    list_docs_parser.add_argument("--page-size", type=int, default=20)
    list_docs_parser.add_argument(
        "--status", choices=("active", "pending", "all"), default="all"
    )
    content_parser = subparsers.add_parser("doc-content", help="查看文档内容")
    content_parser.add_argument("source_id")
    delete_node_parser = subparsers.add_parser("delete-node", help="删除图谱节点")
    delete_node_parser.add_argument("node_id")
    get_node_parser = subparsers.add_parser("get-node", help="查看节点与来源关系")
    get_node_parser.add_argument("node_id")
    get_relation_parser = subparsers.add_parser(
        "get-relation", help="查看关系及其证据来源"
    )
    get_relation_parser.add_argument("edge_id")
    delete_relation_parser = subparsers.add_parser(
        "delete-relation", help="删除关系及其全部证据"
    )
    delete_relation_parser.add_argument("edge_id")
    full_graph_parser = subparsers.add_parser("graph-full", help="读取完整或分页图谱")
    full_graph_parser.add_argument("--nodes-page", type=int)
    full_graph_parser.add_argument("--nodes-page-size", type=int, default=100)
    subparsers.add_parser("graph-meta", help="读取图谱可视化版本与数量")
    graph_nodes_parser = subparsers.add_parser(
        "graph-nodes", help="分页读取图谱可视化节点"
    )
    graph_nodes_parser.add_argument("--page", type=int, default=1)
    graph_nodes_parser.add_argument("--page-size", type=int, default=1000)
    graph_nodes_parser.add_argument("--expected-revision")
    graph_edges_parser = subparsers.add_parser(
        "graph-edges", help="分页读取图谱可视化关系"
    )
    graph_edges_parser.add_argument("--page", type=int, default=1)
    graph_edges_parser.add_argument("--page-size", type=int, default=2000)
    graph_edges_parser.add_argument("--expected-revision")
    neighborhood_parser = subparsers.add_parser(
        "graph-neighborhood", help="读取指定节点的局部图谱"
    )
    neighborhood_parser.add_argument("node_id")
    neighborhood_parser.add_argument("--depth", type=int, default=2)
    neighborhood_parser.add_argument(
        "--direction",
        choices=("forward", "backward", "both"),
        default="both",
    )
    neighborhood_parser.add_argument("--limit", type=int, default=2000)
    neighborhood_parser.add_argument("--edge-limit", type=int, default=10000)
    neighborhood_parser.add_argument("--expected-revision")
    subparsers.add_parser("summarize", help="生成节点群总结")
    cleanup_parser = subparsers.add_parser(
        "cleanup-recycle", help="清理过期回收站文件"
    )
    cleanup_parser.add_argument(
        "--force", action="store_true", help="永久清空全部回收站内容"
    )
    organize_parser = subparsers.add_parser("organize-graph", help="整理重叠节点与关系")
    organize_parser.add_argument(
        "--no-llm", action="store_true", help="仅执行完全术语重叠的确定性整理"
    )
    organize_parser.add_argument(
        "--no-summarize", action="store_true", help="整理后不生成节点群总结"
    )
    subparsers.add_parser("rebuild-knowledge-base", help="重建新增、变化和失败文档")
    subparsers.add_parser("rebuild-all", help="影子重建整个 Graph、RAG 与 FAISS 项目")
    jobs_parser = subparsers.add_parser("jobs", help="查看后台维护任务")
    jobs_parser.add_argument("--limit", type=int, default=100)
    job_parser = subparsers.add_parser("job-status", help="查看单个后台任务及事件")
    job_parser.add_argument("job_id")
    subparsers.add_parser("config-get", help="查看当前配置")
    config_set_parser = subparsers.add_parser("config-set", help="更新配置")
    config_set_parser.add_argument("key", help="配置键（如 rag.chunk_size）")
    config_set_parser.add_argument("value", help="配置值")

    store_init_parser = subparsers.add_parser(
        "store-init", help="在绝对知识位置初始化独立知识库"
    )
    store_init_parser.add_argument("--root", required=True, help="绝对知识位置")
    store_init_parser.add_argument(
        "--scope",
        required=True,
        choices=(
            "knowledge.global",
            "knowledge.shared",
            "knowledge.user",
            "memory.temporary",
            "memory.important",
            "memory.permanent",
            "memory.user",
        ),
    )
    store_init_parser.add_argument("--owner-id")
    store_init_parser.add_argument("--display-name")

    store_info_parser = subparsers.add_parser(
        "store-info", help="查看绝对知识位置的清单与状态"
    )
    store_info_parser.add_argument("--root", required=True, help="绝对知识位置")

    federated_parser = subparsers.add_parser(
        "query-federated", help="跨多个独立知识位置联合检索"
    )
    federated_parser.add_argument("query")
    federated_parser.add_argument(
        "--root",
        action="append",
        required=True,
        dest="roots",
        help="绝对知识位置；可重复指定",
    )
    federated_parser.add_argument(
        "--mode",
        choices=("graph", "rag", "hybrid", "global", "answer"),
        default="hybrid",
    )
    federated_parser.add_argument("--top-k", type=int)
    federated_parser.add_argument("--graph-depth", type=int, default=3)
    federated_parser.add_argument("--force", action="store_true")

    source_sync_parser = subparsers.add_parser(
        "source-sync", help="从 JSON 批量同步外部权威来源"
    )
    source_sync_parser.add_argument("json_file", help="来源数组或含 records 的 JSON 文件")
    source_sync_parser.add_argument(
        "--ingest", action="store_true", help="同步后立即构建 Graph/RAG"
    )
    source_status_parser = subparsers.add_parser(
        "source-status", help="分页查看外部来源同步状态"
    )
    source_status_parser.add_argument("--source-type")
    source_status_parser.add_argument("--include-deleted", action="store_true")
    source_status_parser.add_argument("--page", type=int, default=1)
    source_status_parser.add_argument("--page-size", type=int, default=100)
    source_delete_parser = subparsers.add_parser(
        "source-delete", help="按稳定 source_uri 删除外部派生数据"
    )
    source_delete_parser.add_argument("source_uris", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = (
        Path(args.config or os.getenv("KEMO_GRAPH_CONFIG", DEFAULT_CONFIG_PATH))
        .expanduser()
        .resolve()
    )
    try:
        if args.command == "version":
            data = {"version": read_local_version()}
            _print_json({"ok": True, "data": data, "error": None})
            return 0
        if args.command == "update-check":
            data = ApplicationUpdater().check()
            _print_json({"ok": True, "data": data, "error": None})
            return 0
        if args.command == "update":
            data = ApplicationUpdater().apply(force=bool(args.force))
            _print_json({"ok": True, "data": data, "error": None})
            return 0
        settings = load_config(config_path)
        if args.command == "store-init":
            data = initialize_store(
                args.root,
                scope=args.scope,
                owner_id=args.owner_id,
                display_name=args.display_name,
                settings=settings,
            )
        elif args.command == "store-info":
            data = describe_store(
                args.root,
                settings=settings,
                config_path=config_path,
            )
        elif args.command == "query-federated":
            data = federated_query(
                args.roots,
                args.query,
                mode=args.mode,
                settings=settings,
                config_path=config_path,
                force=args.force,
                top_k=args.top_k,
                graph_depth=args.graph_depth,
            )
        else:
            if (
                args.command in {"source-sync", "source-status", "source-delete"}
                and not args.store_root
            ):
                raise PortableStoreError(
                    f"{args.command} 必须通过全局 --store-root 指定绝对知识位置"
                )
            if args.store_root and (args.data_dir or args.external_dir):
                raise PortableStoreError(
                    "--store-root 不能与 --data-dir 或 --external-dir 同时使用"
                )
            if (
                args.store_root
                and args.command == "import"
                and not Path(args.path).expanduser().is_absolute()
            ):
                raise PortableStoreAccessError(
                    "使用 --store-root 时，import 文档路径必须是绝对路径"
                )
            service = (
                create_store_service(
                    args.store_root,
                    settings=settings,
                    config_path=config_path,
                )
                if args.store_root
                else KnowledgeBaseService(
                    settings=settings,
                    data_dir=args.data_dir,
                    external_dir=args.external_dir,
                    config_path=config_path,
                )
            )
            data = _dispatch(args, service)
        _print_json({"ok": True, "data": data, "error": None})
        return 0
    except Exception as exc:
        code, message, exit_code = _map_error(exc)
        _print_json(
            {
                "ok": False,
                "data": None,
                "error": {"code": code, "message": message},
            }
        )
        return exit_code


def _dispatch(
    args: argparse.Namespace, service: KnowledgeBaseService
) -> dict[str, Any]:
    if args.command == "source-sync":
        payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
        records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise ValueError("source-sync JSON 必须是数组或包含 records 数组的对象")
        return service.sync_sources(records, ingest_after_sync=args.ingest)
    if args.command == "source-status":
        return service.list_synced_sources(
            source_type=args.source_type,
            include_deleted=args.include_deleted,
            page=args.page,
            page_size=args.page_size,
        )
    if args.command == "source-delete":
        return service.delete_synced_sources(args.source_uris)
    if args.command == "ingest":
        return service.ingest(paths=args.paths or None, mode=args.mode)
    if args.command == "import":
        return service.import_document(
            args.path,
            ingest_after_import=not args.no_ingest,
        )
    if args.command == "query-graph":
        return service.query_graph(
            args.query,
            depth=args.depth,
            direction=args.direction,
            confidence=args.confidence,
            force=args.force,
        )
    if args.command == "query-rag":
        return service.query_rag(
            args.query,
            top_k=args.top_k,
            threshold=args.threshold,
            force=args.force,
        )
    if args.command == "query-hybrid":
        return service.query_hybrid(
            args.query,
            graph_depth=args.graph_depth,
            rag_top_k=args.rag_top_k,
            graph_confidence=args.graph_confidence,
            rag_threshold=args.rag_threshold,
            direction=args.direction,
            force=args.force,
        )
    if args.command == "query-answer":
        return service.query_answer(
            args.query,
            graph_depth=args.graph_depth,
            rag_top_k=args.rag_top_k,
            graph_confidence=args.graph_confidence,
            rag_threshold=args.rag_threshold,
            direction=args.direction,
            force=args.force,
        )
    if args.command == "query-global":
        return service.query_global(args.query, top_k=args.top_k, force=args.force)
    if args.command == "cache-list":
        return service.list_cached_queries(page=args.page, page_size=args.page_size)
    if args.command == "cache-show":
        return service.get_cached_query(args.cache_key)
    if args.command == "cache-clear":
        return service.clear_search_cache(stale_only=args.stale)
    if args.command == "status":
        return service.status()
    if args.command == "delete-doc":
        return service.delete_document(args.source_id)
    if args.command == "list-docs":
        return service.list_documents(
            status=None if args.status == "all" else args.status,
            page=args.page,
            page_size=args.page_size,
        )
    if args.command == "doc-content":
        return service.get_document_content(args.source_id)
    if args.command == "delete-node":
        return service.delete_node(args.node_id)
    if args.command == "get-node":
        return service.get_node(args.node_id)
    if args.command == "get-relation":
        return service.get_relation(args.edge_id)
    if args.command == "delete-relation":
        return service.delete_relation(args.edge_id)
    if args.command == "graph-full":
        return service.get_full_graph(
            nodes_page=args.nodes_page,
            nodes_page_size=args.nodes_page_size,
        )
    if args.command == "graph-meta":
        return service.get_graph_visualization_meta()
    if args.command == "graph-nodes":
        return service.list_graph_visualization_nodes(
            page=args.page,
            page_size=args.page_size,
            expected_revision=args.expected_revision,
        )
    if args.command == "graph-edges":
        return service.list_graph_visualization_edges(
            page=args.page,
            page_size=args.page_size,
            expected_revision=args.expected_revision,
        )
    if args.command == "graph-neighborhood":
        return service.get_graph_neighborhood(
            args.node_id,
            depth=args.depth,
            direction=args.direction,
            limit=args.limit,
            edge_limit=args.edge_limit,
            expected_revision=args.expected_revision,
        )
    if args.command == "summarize":
        return service.generate_group_summaries()
    if args.command == "cleanup-recycle":
        return service.cleanup_recycle(force=args.force)
    if args.command == "organize-graph":
        return service.organize_graph(
            use_llm=not args.no_llm,
            summarize=not args.no_summarize,
        )
    if args.command == "rebuild-knowledge-base":
        return service.rebuild_knowledge_base()
    if args.command == "rebuild-all":
        return service.rebuild_all()
    if args.command == "jobs":
        return {"jobs": service.list_jobs(limit=args.limit)}
    if args.command == "job-status":
        return service.get_job(args.job_id)
    if args.command == "config-get":
        return service.get_config()
    if args.command == "config-set":
        return _set_config(service, args.key, args.value)
    raise ValueError(f"未知命令：{args.command}")


def _set_config(service: KnowledgeBaseService, key: str, value: str) -> dict[str, Any]:
    import json as _json

    current = service.get_config()
    keys = key.split(".")
    target = current
    for part in keys[:-1]:
        if part not in target:
            raise ValueError(f"配置键不存在：{part}")
        target = target[part]
    last = keys[-1]
    if last not in target:
        raise ValueError(f"配置键不存在：{last}")
    try:
        parsed = _json.loads(value)
    except _json.JSONDecodeError:
        parsed = value
    target[last] = parsed
    return service.save_config(current)


def _map_error(exc: Exception) -> tuple[str, str, int]:
    if isinstance(exc, UpdatePermissionError):
        return "UPDATE_FORBIDDEN", str(exc), 3
    if isinstance(exc, UpdateBlockedError):
        return "UPDATE_BLOCKED", str(exc), 3
    if isinstance(exc, UpdateSourceError):
        return "UPDATE_SOURCE_FAILED", str(exc), 4
    if isinstance(exc, UpdateError):
        return "UPDATE_FAILED", str(exc), 1
    if isinstance(exc, PortableStoreAccessError):
        return "STORE_ACCESS_DENIED", str(exc), 2
    if isinstance(exc, PortableStoreNotInitializedError):
        return "STORE_NOT_INITIALIZED", str(exc), 2
    if isinstance(exc, PortableStoreError):
        return "STORE_INVALID", str(exc), 2
    if isinstance(exc, KnowledgeBaseNotInitializedError):
        return "NOT_INITIALIZED", str(exc), 2
    if isinstance(exc, KnowledgeBaseProcessingError):
        return "PROCESSING", str(exc), 3
    if isinstance(exc, DocumentNotFoundError):
        return "NOT_FOUND", str(exc), 2
    if isinstance(exc, JobNotFoundError):
        return "NOT_FOUND", str(exc), 2
    if isinstance(exc, UnsupportedDocumentFormatError):
        return "UNSUPPORTED_FORMAT", str(exc), 2
    if isinstance(exc, DocumentTooLargeError):
        return "FILE_TOO_LARGE", str(exc), 2
    if isinstance(exc, DocumentImportPathError):
        return "INVALID_PATH", str(exc), 2
    if isinstance(exc, DocumentImportConflictError):
        return "IMPORT_SOURCE_CHANGED", str(exc), 3
    if isinstance(exc, DocumentConversionError):
        return "CONVERSION_FAILED", str(exc), 2
    if isinstance(exc, DocumentImportError):
        return "IMPORT_FAILED", str(exc), 2
    if isinstance(
        exc,
        (
            GraphQueryError,
            RAGQueryError,
            RecycleConflictError,
            IngestError,
            ValueError,
            TypeError,
        ),
    ):
        return "INVALID_PARAM", str(exc), 2
    if isinstance(exc, GraphOrganizerError):
        return "ORGANIZE_FAILED", str(exc), 1
    if isinstance(exc, RebuildError):
        return "REBUILD_FAILED", str(exc), 1
    return "INTERNAL", "内部错误", 1


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
