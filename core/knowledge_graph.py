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


class KnowledgeGraphMixin:
    """Internal knowledge graph domain implementation."""

    def _get_node_impl(self, node_id: str) -> dict[str, Any]:
        """旧私有入口兼容层；读取实现位于 GraphService。"""
        return self.graph.get_node(node_id)

    def _get_relation_impl(self, edge_id: str) -> dict[str, Any]:
        """旧私有入口兼容层；读取实现位于 GraphService。"""
        return self.graph.get_relation(edge_id)

    def _delete_relation_impl(self, edge_id: str) -> dict[str, Any]:
        """删除关系及其全部证据，并使群组总结和搜索缓存失效。"""

        self._require_available("graph")
        graph_connection = connect_graph(self.paths)
        try:
            graph_connection.execute("BEGIN IMMEDIATE")
            edge = graph_connection.execute(
                """
                SELECT e.edge_id, e.source_node_id, sn.keyword AS source_keyword,
                       e.relation, e.target_node_id, tn.keyword AS target_keyword,
                       e.weight, e.support_count, e.created_at
                FROM edges e
                JOIN nodes sn ON sn.node_id = e.source_node_id
                JOIN nodes tn ON tn.node_id = e.target_node_id
                WHERE e.edge_id = ?
                """,
                (edge_id,),
            ).fetchone()
            if edge is None:
                raise DocumentNotFoundError(f"关系不存在：{edge_id}")
            evidence_count = int(
                graph_connection.execute(
                    "SELECT COUNT(*) FROM edge_sources WHERE edge_id = ?",
                    (edge_id,),
                ).fetchone()[0]
            )
            mention_rows = graph_connection.execute(
                """
                SELECT rm.mention_id
                FROM relation_mentions rm
                JOIN mention_nodes sm ON sm.mention_id = rm.source_mention_id
                JOIN mention_nodes tm ON tm.mention_id = rm.target_mention_id
                WHERE sm.node_id = ? AND rm.relation = ? AND tm.node_id = ?
                """,
                (
                    edge["source_node_id"],
                    edge["relation"],
                    edge["target_node_id"],
                ),
            ).fetchall()
            if mention_rows:
                placeholders = ",".join("?" for _ in mention_rows)
                graph_connection.execute(
                    f"DELETE FROM relation_mentions WHERE mention_id IN ({placeholders})",
                    tuple(row["mention_id"] for row in mention_rows),
                )
            graph_connection.execute(
                "DELETE FROM edge_sources WHERE edge_id = ?", (edge_id,)
            )
            graph_connection.execute("DELETE FROM edges WHERE edge_id = ?", (edge_id,))
            graph_connection.execute("DELETE FROM group_nodes")
            graph_connection.execute("DELETE FROM groups")
            graph_connection.commit()
        except Exception:
            graph_connection.rollback()
            raise
        finally:
            graph_connection.close()

        self._refresh_graph_meta(changed=True)
        RAGEngine(self.data_dir, settings=self.settings).refresh_auxiliary_consistency()
        try:
            cache_deleted = self.clear_search_cache()["deleted"]
        except Exception:
            cache_deleted = 0
        result = {
            "deleted": True,
            "edge": _relation_row(edge),
            "deleted_evidence_count": evidence_count,
            "deleted_mention_count": len(mention_rows),
            "groups_invalidated": True,
            "search_cache_deleted": cache_deleted,
        }
        self._log_event(
            "delete_relation",
            f"edge_id={edge_id}, evidence={evidence_count}",
        )
        return result

    def _delete_node_impl(self, node_id: str) -> dict[str, Any]:
        """删除指定节点，按 06 文档规则执行级联操作。"""
        self._require_available("graph")
        with Ingestor(
            data_dir=self.data_dir,
            external_dir=self.external_dir,
            settings=self.settings,
        )._write_lock:
            result = self._execute_node_deletion(node_id)
        try:
            result["search_cache_deleted"] = self.clear_search_cache()["deleted"]
        except Exception:
            result["search_cache_deleted"] = 0
        self._log_event(
            "delete_node",
            f"node_id={node_id}, edges={result['cascade_deleted_edges']}",
        )
        return result

    def _execute_node_deletion(self, node_id: str) -> dict[str, Any]:
        graph_connection = connect_graph(self.paths)
        try:
            node_row = graph_connection.execute(
                "SELECT node_id, keyword FROM nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if node_row is None:
                raise DocumentNotFoundError(f"节点不存在：{node_id}")
            bindings = graph_connection.execute(
                """
                SELECT ns.source_id,
                       (SELECT COUNT(*) FROM node_sources other
                        WHERE other.source_id = ns.source_id
                          AND other.node_id != ns.node_id) AS other_node_count
                FROM node_sources ns WHERE ns.node_id = ?
                ORDER BY ns.source_id
                """,
                (node_id,),
            ).fetchall()
        finally:
            graph_connection.close()

        source_ids = [str(binding["source_id"]) for binding in bindings]
        source_by_id: dict[str, Any] = {}
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            source_connection = connect_sources(self.paths)
            try:
                source_by_id = {
                    str(row["source_id"]): row
                    for row in source_connection.execute(
                        f"""
                        SELECT source_id, relative_path, exists_status
                        FROM sources WHERE source_id IN ({placeholders})
                        """,
                        tuple(source_ids),
                    ).fetchall()
                }
            finally:
                source_connection.close()

        isolated_source_ids = [
            str(binding["source_id"])
            for binding in bindings
            if int(binding["other_node_count"] or 0) == 0
            and str(binding["source_id"]) in source_by_id
            and source_by_id[str(binding["source_id"])]["exists_status"] == "active"
        ]
        unlinked_files = [
            str(source_by_id[str(binding["source_id"])]["relative_path"])
            for binding in bindings
            if int(binding["other_node_count"] or 0) > 0
            and str(binding["source_id"]) in source_by_id
        ]

        ingestor = Ingestor(
            data_dir=self.data_dir,
            external_dir=self.external_dir,
            settings=self.settings,
        )
        recycled_files: list[str] = []
        recycled_paths: list[str] = []
        for source_id in isolated_source_ids:
            deletion = ingestor.delete_document(source_id)
            recycled_files.append(str(deletion["relative_path"]))
            if deletion.get("recycled_path"):
                recycled_paths.append(str(deletion["recycled_path"]))

        graph_connection = connect_graph(self.paths)
        try:
            graph_connection.execute("BEGIN IMMEDIATE")
            edge_ids = {
                row["edge_id"]
                for row in graph_connection.execute(
                    """
                    SELECT edge_id FROM edges
                    WHERE source_node_id = ? OR target_node_id = ?
                    """,
                    (node_id, node_id),
                ).fetchall()
            }
            mention_ids = [
                str(row["mention_id"])
                for row in graph_connection.execute(
                    "SELECT mention_id FROM mention_nodes WHERE node_id = ?",
                    (node_id,),
                ).fetchall()
            ]
            deleted_relation_mentions = 0
            if mention_ids:
                placeholders = ",".join("?" for _ in mention_ids)
                deleted_relation_mentions = int(
                    graph_connection.execute(
                        f"""
                        SELECT COUNT(*) FROM relation_mentions
                        WHERE source_mention_id IN ({placeholders})
                           OR target_mention_id IN ({placeholders})
                        """,
                        tuple(mention_ids + mention_ids),
                    ).fetchone()[0]
                )
                graph_connection.execute(
                    f"""
                    DELETE FROM relation_mentions
                    WHERE source_mention_id IN ({placeholders})
                       OR target_mention_id IN ({placeholders})
                    """,
                    tuple(mention_ids + mention_ids),
                )
                graph_connection.execute(
                    f"DELETE FROM entity_mentions WHERE mention_id IN ({placeholders})",
                    tuple(mention_ids),
                )
            if edge_ids:
                placeholders = ",".join("?" for _ in edge_ids)
                parameters = tuple(sorted(edge_ids))
                graph_connection.execute(
                    f"DELETE FROM edge_sources WHERE edge_id IN ({placeholders})",
                    parameters,
                )
                graph_connection.execute(
                    f"DELETE FROM edges WHERE edge_id IN ({placeholders})",
                    parameters,
                )

            graph_connection.execute(
                "DELETE FROM node_sources WHERE node_id = ?", (node_id,)
            )
            graph_connection.execute(
                "DELETE FROM mention_nodes WHERE node_id = ?", (node_id,)
            )
            graph_connection.execute("DELETE FROM group_nodes")
            graph_connection.execute("DELETE FROM groups")
            graph_connection.execute("DELETE FROM nodes WHERE node_id = ?", (node_id,))
            graph_connection.commit()
        except Exception:
            graph_connection.rollback()
            raise
        finally:
            graph_connection.close()

        # 清理 chunk_nodes 中的孤立引用
        rag_connection = connect_rag(self.paths)
        try:
            rag_connection.execute(
                "DELETE FROM chunk_nodes WHERE node_id = ?", (node_id,)
            )
            rag_connection.commit()
        finally:
            rag_connection.close()

        self._refresh_graph_meta(changed=True)
        RAGEngine(self.data_dir, settings=self.settings).refresh_auxiliary_consistency()
        return {
            "deleted_node_id": node_id,
            "deleted_keyword": node_row["keyword"],
            "cascade_deleted_edges": len(edge_ids),
            "deleted_mention_count": len(mention_ids),
            "deleted_relation_mention_count": deleted_relation_mentions,
            "deleted_source_ids": isolated_source_ids,
            "recycled_files": recycled_files,
            "recycled_paths": recycled_paths,
            "unlinked_files": unlinked_files,
        }

    def _generate_group_summaries_impl(self, *, force: bool = False) -> dict[str, Any]:
        """按 06 文档规则执行一次节点群总结。"""
        started_at = time.perf_counter()
        self._log_event("group_summary_start", "status=started")
        self._require_available("graph")
        meta = read_graph_meta(self.paths)
        changed = int(meta.get("changed_since_summary", 0))
        if not force and changed < self.settings.summary_trigger_file_count:
            result = {
                "generated": 0,
                "changed_files": changed,
                "threshold": self.settings.summary_trigger_file_count,
                "skipped_reason": "改动数量未达阈值",
            }
            self._log_event(
                "group_summary_done",
                f"generated=0, changed={changed}, skipped=threshold",
                _elapsed_ms(started_at),
            )
            return result

        components = self._find_connected_components()
        qualified = [
            comp
            for comp in components
            if len(comp) >= 3 and self._component_depth(comp) >= 3
        ]
        if not qualified:
            graph_connection = connect_graph(self.paths)
            try:
                graph_connection.execute("BEGIN IMMEDIATE")
                graph_connection.execute("DELETE FROM group_nodes")
                graph_connection.execute("DELETE FROM groups")
                graph_connection.commit()
            except Exception:
                graph_connection.rollback()
                raise
            finally:
                graph_connection.close()
            RAGEngine(self.data_dir, settings=self.settings).refresh_auxiliary_consistency()
            write_graph_meta(self.paths, {**meta, "changed_since_summary": 0})
            result = {
                "generated": 0,
                "qualified_components": 0,
                "total_components": len(components),
                "changed_files": changed,
            }
            self._log_event(
                "group_summary_done",
                f"generated=0, changed={changed}, skipped=no_component",
                _elapsed_ms(started_at),
            )
            return result

        community_records: list[tuple[str, str]] = []
        graph_connection = connect_graph(self.paths)
        try:
            graph_connection.execute("BEGIN IMMEDIATE")
            graph_connection.execute("DELETE FROM group_nodes")
            graph_connection.execute("DELETE FROM groups")
            generated = 0
            for component in qualified:
                summary = self._summarize_component(component)
                placeholders = ",".join("?" for _ in component)
                edge_count = int(
                    graph_connection.execute(
                        f"""
                        SELECT COUNT(*) FROM edges
                        WHERE source_node_id IN ({placeholders})
                          AND target_node_id IN ({placeholders})
                        """,
                        tuple(sorted(component)) + tuple(sorted(component)),
                    ).fetchone()[0]
                )
                group_id = str(uuid4())
                now_str = _now_iso()
                graph_connection.execute(
                    """
                    INSERT INTO groups (group_id, summary, node_count, edge_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (group_id, summary, len(component), edge_count, now_str, now_str),
                )
                graph_connection.executemany(
                    "INSERT INTO group_nodes (group_id, node_id) VALUES (?, ?)",
                    [(group_id, node_id) for node_id in sorted(component)],
                )
                community_records.append((group_id, summary))
                generated += 1
            graph_connection.commit()
        except Exception:
            graph_connection.rollback()
            raise
        finally:
            graph_connection.close()

        community_vectors = RAGEngine(
            self.data_dir,
            settings=self.settings,
        ).sync_community_vectors(community_records)

        write_graph_meta(
            self.paths,
            {**meta, "changed_since_summary": 0, "last_summary_at": _now_iso()},
        )
        self._refresh_graph_meta(changed=False)
        result = {
            "generated": generated,
            "qualified_components": len(qualified),
            "total_components": len(components),
            "changed_files": changed,
            "community_vectors": community_vectors,
        }
        self._log_event(
            "group_summary_done",
            f"generated={generated}, changed={changed}",
            _elapsed_ms(started_at),
        )
        return result

    def _find_connected_components(self) -> list[set[str]]:
        """从 graph.db 中查找所有连通分量。"""
        connection = connect_graph(self.paths)
        try:
            node_ids = {
                row["node_id"]
                for row in connection.execute("SELECT node_id FROM nodes").fetchall()
            }
            adjacency: dict[str, set[str]] = {nid: set() for nid in node_ids}
            for row in connection.execute(
                "SELECT source_node_id, target_node_id FROM edges"
            ).fetchall():
                a, b = row["source_node_id"], row["target_node_id"]
                if a in adjacency and b in adjacency:
                    adjacency[a].add(b)
                    adjacency[b].add(a)
        finally:
            connection.close()

        visited: set[str] = set()
        components: list[set[str]] = []
        for node_id in sorted(node_ids):
            if node_id in visited:
                continue
            component: set[str] = set()
            frontier = {node_id}
            while frontier:
                current = frontier.pop()
                if current in visited:
                    continue
                visited.add(current)
                component.add(current)
                frontier.update(adjacency.get(current, set()) - visited)
            components.append(component)
        return components

    def _component_depth(self, component: set[str]) -> int:
        """计算连通分量中任意节点能到达的最大 BFS 深度。"""
        connection = connect_graph(self.paths)
        try:
            adjacency: dict[str, set[str]] = {nid: set() for nid in component}
            for row in connection.execute(
                """
                SELECT source_node_id, target_node_id FROM edges
                WHERE source_node_id IN ({}) AND target_node_id IN ({})
                """.format(
                    ",".join("?" * len(component)),
                    ",".join("?" * len(component)),
                ),
                tuple(sorted(component)) + tuple(sorted(component)),
            ).fetchall():
                a, b = row["source_node_id"], row["target_node_id"]
                adjacency.setdefault(a, set()).add(b)
                adjacency.setdefault(b, set()).add(a)
        finally:
            connection.close()

        max_depth = 0
        for start in component:
            visited = {start}
            frontier = {start}
            depth = 0
            while frontier and depth <= 5:
                next_frontier: set[str] = set()
                for nid in frontier:
                    for neighbor in adjacency.get(nid, set()):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            next_frontier.add(neighbor)
                if next_frontier:
                    depth += 1
                frontier = next_frontier
            max_depth = max(max_depth, depth)
        return max_depth

    def _summarize_component(self, component: set[str]) -> str:
        """用 LLM 为一个节点群生成总结。"""
        connection = connect_graph(self.paths)
        try:
            nodes_info: list[str] = []
            placeholders = ",".join("?" for _ in component)
            for row in connection.execute(
                f"SELECT keyword, summary FROM nodes WHERE node_id IN ({placeholders})",
                tuple(sorted(component)),
            ).fetchall():
                nodes_info.append(f"- {row['keyword']}: {row['summary']}")
            edges_info: list[str] = []
            for row in connection.execute(
                f"""
                SELECT n1.keyword AS src_kw, e.relation, n2.keyword AS tgt_kw
                FROM edges e
                JOIN nodes n1 ON n1.node_id = e.source_node_id
                JOIN nodes n2 ON n2.node_id = e.target_node_id
                WHERE e.source_node_id IN ({placeholders})
                  AND e.target_node_id IN ({placeholders})
                """,
                tuple(sorted(component)) + tuple(sorted(component)),
            ).fetchall():
                edges_info.append(
                    f"- {row['src_kw']} -[{row['relation']}]-> {row['tgt_kw']}"
                )
        finally:
            connection.close()

        system_prompt = (
            "你是知识图谱群组总结助手。根据给出的节点摘要和关系，"
            "用 2~3 句话总结该群组的核心主题和关键关系。"
            "要求：简洁、专业，只输出一段中文总结，不要额外解释。"
        )
        user_prompt = (
            f"节点（{len(nodes_info)} 个）：\n"
            + "\n".join(nodes_info[:20])
            + "\n\n关系（{len(edges_info)} 条）：\n"
            + "\n".join(edges_info[:30])
            + "\n\n请为该群组生成一段总结。"
        )

        started_at = time.perf_counter()
        try:
            summary = _public_chat(
                system_prompt,
                user_prompt,
                settings=self.settings,
            ).strip()
            self._log_event(
                "llm_request",
                f"purpose=group_summary, model={self.settings.models.llm}",
                _elapsed_ms(started_at),
            )
            return summary
        except Exception:
            self._log_event(
                "llm_request",
                f"purpose=group_summary, model={self.settings.models.llm}, status=failed",
                _elapsed_ms(started_at),
                level="ERROR",
            )
            raise

    def _refresh_graph_meta(self, *, changed: bool) -> None:
        """刷新 graph_meta.json，并在有变更时递增 changed_since_summary。"""
        connection = connect_graph(self.paths)
        try:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM nodes) AS total_nodes,
                    (SELECT COUNT(*) FROM edges) AS total_edges,
                    (SELECT COUNT(*) FROM groups) AS total_groups
                """
            ).fetchone()
        finally:
            connection.close()
        meta = read_graph_meta(self.paths)
        meta.update(
            {
                "total_nodes": int(counts["total_nodes"]),
                "total_edges": int(counts["total_edges"]),
                "total_groups": int(counts["total_groups"]),
            }
        )
        if changed:
            meta["changed_since_summary"] = (
                int(meta.get("changed_since_summary", 0)) + 1
            )
        write_graph_meta(self.paths, meta)

    def _organize_graph_impl(
        self,
        *,
        use_llm: bool = True,
        summarize: bool = True,
    ) -> dict[str, Any]:
        """只整理既有图谱投影，不重读文档、不调用 Embedding。"""

        self._require_available("graph", "rag")
        result = GraphOrganizer(
            self.data_dir,
            settings=self.settings,
        ).organize(use_llm=use_llm)
        summary: dict[str, Any] | None = None
        if summarize and result.get("groups_invalidated"):
            summary = self.generate_group_summaries(force=True)
        return {**result, "group_summary": summary}

    def _rebuild_knowledge_base_impl(
        self,
        *,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """重读新增、变化、删除和失败文档；未变化文档保持跳过。"""

        return KnowledgeBaseRebuilder(
            self.data_dir,
            self.external_dir,
            settings=self.settings,
        ).rebuild_changed(progress=progress)

    def _rebuild_all_impl(
        self,
        *,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """在影子目录重建 Graph、RAG、FAISS，验证后切换正式目录。"""

        return KnowledgeBaseRebuilder(
            self.data_dir,
            self.external_dir,
            settings=self.settings,
        ).rebuild_all(progress=progress)

    def _list_jobs_impl(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        from .jobs import list_jobs

        return list_jobs(self.data_dir, settings=self.settings, limit=limit)

    def _get_job_impl(self, job_id: str) -> dict[str, Any]:
        from .jobs import get_job

        return get_job(job_id, self.data_dir, settings=self.settings)

    def _get_full_graph_impl(
        self,
        nodes_page: int | None = None,
        nodes_page_size: int = 100,
    ) -> dict[str, Any]:
        """旧私有入口兼容层；读取实现位于 GraphService。"""
        return self.graph.get_full_graph(nodes_page, nodes_page_size)

    def _get_graph_visualization_meta_impl(self) -> dict[str, Any]:
        """返回 GPU 图谱分页加载所需的稳定 revision 与数量。"""

        self._require_available("graph")
        return get_visualization_meta(self.paths)

    def _list_graph_visualization_nodes_impl(
        self,
        *,
        page: int = 1,
        page_size: int = 1000,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """独立分页返回可视化节点及来源、群组绑定。"""

        self._require_available("graph")
        return list_visualization_nodes(
            self.paths,
            page=page,
            page_size=page_size,
            expected_revision=expected_revision,
        )

    def _list_graph_visualization_edges_impl(
        self,
        *,
        page: int = 1,
        page_size: int = 2000,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """独立分页返回可视化关系，不随节点页重复传输。"""

        self._require_available("graph")
        return list_visualization_edges(
            self.paths,
            page=page,
            page_size=page_size,
            expected_revision=expected_revision,
        )

    def _get_graph_neighborhood_impl(
        self,
        node_id: str,
        *,
        depth: int = 2,
        direction: GraphDirection = "both",
        limit: int = 2000,
        edge_limit: int = 10000,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """按节点 ID 返回确定性局部子图，不触发任何模型调用。"""

        self._require_available("graph")
        return get_neighborhood(
            self.paths,
            node_id,
            depth=depth,
            direction=direction,
            limit=limit,
            edge_limit=edge_limit,
            expected_revision=expected_revision,
        )

    def _new_graph_engine(self) -> GraphEngine:
        from . import knowledge_base

        return knowledge_base.GraphEngine(self.data_dir, settings=self.settings)
