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
    DocumentImportError,
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
    _public_convert_document,
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


class KnowledgeDocumentsMixin:
    """Internal knowledge documents domain implementation."""

    def _list_documents_impl(
        self,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页返回文档基本信息，并支持按活动或待处理状态筛选。"""

        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page 必须是大于等于 1 的整数")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 100
        ):
            raise ValueError("page_size 必须是 1 到 100 之间的整数")

        pagination = {
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 0,
        }
        if not self.paths.sources_db.exists():
            return {"documents": [], "pagination": pagination}

        where_sql = ""
        order_sql = "ORDER BY exists_status, relative_path"
        if status == "pending":
            where_sql = (
                "WHERE exists_status = 'active' "
                "AND (graph_status = 'pending' OR rag_status = 'pending')"
            )
            order_sql = "ORDER BY relative_path"
        elif status == "active":
            where_sql = "WHERE exists_status = 'active'"
            order_sql = "ORDER BY relative_path"

        connection = connect_sources(self.paths)
        try:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM sources {where_sql}"
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT source_id, relative_path, original_path,
                       content_hash, graph_hash, rag_hash,
                       origin_hash, origin_size, origin_modified_at,
                       source_uri, source_type, source_revision,
                       source_updated_at, source_metadata_json,
                       external_content_hash, last_synced_at,
                       graph_status, rag_status, exists_status,
                       created_at, updated_at
                FROM sources
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?
                """,
                (page_size, (page - 1) * page_size),
            ).fetchall()
        finally:
            connection.close()

        pagination["total"] = total
        pagination["total_pages"] = (total + page_size - 1) // page_size
        return {
            "documents": [
                {
                    "source_id": row["source_id"],
                    "relative_path": row["relative_path"],
                    "original_path": row["original_path"],
                    "content_hash": row["content_hash"],
                    "graph_hash": row["graph_hash"],
                    "rag_hash": row["rag_hash"],
                    "origin_hash": row["origin_hash"],
                    "origin_size": row["origin_size"],
                    "origin_modified_at": row["origin_modified_at"],
                    "source_uri": row["source_uri"],
                    "source_type": row["source_type"],
                    "source_revision": row["source_revision"],
                    "source_updated_at": row["source_updated_at"],
                    "source_metadata": _load_source_metadata(
                        row["source_metadata_json"]
                    ),
                    "external_content_hash": row["external_content_hash"],
                    "last_synced_at": row["last_synced_at"],
                    "graph_status": row["graph_status"],
                    "rag_status": row["rag_status"],
                    "exists_status": row["exists_status"],
                    "updated_at": row["updated_at"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
            "pagination": pagination,
        }

    def _get_document_content_impl(self, source_id: str) -> dict[str, Any]:
        """返回指定文档的 Markdown 文本内容。"""
        self._require_initialized()
        connection = connect_sources(self.paths)
        try:
            row = connection.execute(
                """
                SELECT source_id, original_path, relative_path, content_hash,
                       graph_hash, rag_hash, origin_hash, origin_size,
                       origin_modified_at, source_uri, source_type,
                       source_revision, source_updated_at, source_metadata_json,
                       external_content_hash, last_synced_at,
                       graph_status, rag_status, exists_status
                FROM sources WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise DocumentNotFoundError(f"文档不存在：{source_id}")
        if row["exists_status"] != "active":
            raise DocumentNotFoundError(f"文档已删除：{source_id}")

        file_path = self.external_dir / row["relative_path"]
        if not file_path.exists():
            raise DocumentNotFoundError(f"文档文件缺失：{row['relative_path']}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise DocumentNotFoundError(
                f"无法读取文档：{row['relative_path']}: {exc}"
            ) from exc

        return {
            "source_id": row["source_id"],
            "original_path": row["original_path"],
            "relative_path": row["relative_path"],
            "content_hash": row["content_hash"],
            "graph_hash": row["graph_hash"],
            "rag_hash": row["rag_hash"],
            "origin_hash": row["origin_hash"],
            "origin_size": row["origin_size"],
            "origin_modified_at": row["origin_modified_at"],
            "source_uri": row["source_uri"],
            "source_type": row["source_type"],
            "source_revision": row["source_revision"],
            "source_updated_at": row["source_updated_at"],
            "source_metadata": _load_source_metadata(row["source_metadata_json"]),
            "external_content_hash": row["external_content_hash"],
            "last_synced_at": row["last_synced_at"],
            "graph_status": row["graph_status"],
            "rag_status": row["rag_status"],
            "exists_status": row["exists_status"],
            "content": content,
        }

    def _update_document_content_impl(
        self,
        source_id: str,
        content: str,
        *,
        expected_content_hash: str | None = None,
    ) -> dict[str, Any]:
        """原子保存 Markdown，并仅将派生 Graph/RAG 标记为待重建。"""

        self._require_initialized()
        normalized_source_id = str(source_id).strip()
        if not normalized_source_id:
            raise ValueError("source_id 必须是非空字符串")
        if not isinstance(content, str):
            raise TypeError("content 必须是字符串")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_IMPORT_BYTES:
            raise DocumentTooLargeError(
                f"Markdown 内容超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MB 上限"
            )
        expected_hash = (
            str(expected_content_hash).strip().casefold()
            if expected_content_hash is not None
            else None
        )
        if expected_hash is not None and (
            len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            raise ValueError("expected_content_hash 必须是 64 位 SHA-256")

        ingestor = Ingestor(
            data_dir=self.data_dir,
            external_dir=self.external_dir,
            settings=self.settings,
        )
        with ingestor._write_lock:
            connection = connect_sources(self.paths)
            try:
                row = connection.execute(
                    """
                    SELECT source_id, relative_path, content_hash,
                           graph_hash, rag_hash, graph_status, rag_status,
                           exists_status
                    FROM sources WHERE source_id = ?
                    """,
                    (normalized_source_id,),
                ).fetchone()
            finally:
                connection.close()
            if row is None or row["exists_status"] != "active":
                raise DocumentNotFoundError(
                    f"活动文档不存在：{normalized_source_id}"
                )

            current_hash = str(row["content_hash"])
            if expected_hash is not None and expected_hash != current_hash.casefold():
                raise DocumentContentConflictError(
                    "文档已被其他操作更新，请重新加载后再编辑"
                )
            destination = _safe_markdown_destination(
                self.external_dir,
                str(row["relative_path"]),
            )
            if not destination.is_file():
                raise DocumentNotFoundError(
                    f"文档文件缺失：{row['relative_path']}"
                )
            try:
                previous_bytes = destination.read_bytes()
            except OSError as exc:
                raise DocumentNotFoundError(
                    f"无法读取文档：{row['relative_path']}"
                ) from exc
            disk_hash = hashlib.sha256(previous_bytes).hexdigest()
            if disk_hash != current_hash:
                raise DocumentContentConflictError(
                    "磁盘文档已在数据库外发生变化，请刷新文档列表后再编辑"
                )

            new_hash = hashlib.sha256(encoded).hexdigest()
            if new_hash == current_hash:
                return {
                    "source_id": normalized_source_id,
                    "relative_path": str(row["relative_path"]),
                    "changed": False,
                    "previous_content_hash": current_hash,
                    "content_hash": current_hash,
                    "graph_status": str(row["graph_status"]),
                    "rag_status": str(row["rag_status"]),
                }

            _write_bytes_atomic(destination, encoded)
            now = _now_iso()
            connection = connect_sources(self.paths)
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE sources
                    SET content_hash = ?, graph_status = 'pending',
                        rag_status = 'pending', updated_at = ?
                    WHERE source_id = ? AND exists_status = 'active'
                      AND content_hash = ?
                    """,
                    (new_hash, now, normalized_source_id, current_hash),
                )
                if cursor.rowcount != 1:
                    raise DocumentContentConflictError(
                        "文档状态已变化，请重新加载后再编辑"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                _write_bytes_atomic(destination, previous_bytes)
                raise
            finally:
                connection.close()

        self._log_event(
            "document_content_update",
            f"source_id={normalized_source_id}, path={row['relative_path']}",
        )
        return {
            "source_id": normalized_source_id,
            "relative_path": str(row["relative_path"]),
            "changed": True,
            "previous_content_hash": current_hash,
            "content_hash": new_hash,
            "graph_status": "pending",
            "rag_status": "pending",
            "updated_at": now,
        }

    def _move_source_to_recycle(self, relative_path: str) -> str | None:
        """将 Markdown 文件移入回收站。回滚保证原子性。"""
        source_path = (self.external_dir / relative_path).resolve()
        try:
            source_path.relative_to(self.external_dir)
        except ValueError as exc:
            raise IngestError(f"非法 Markdown 相对路径：{relative_path}") from exc
        if not source_path.exists():
            return None

        recycle_root = (self.external_dir.parent / "recycle").resolve()
        destination = (recycle_root / relative_path).resolve()
        try:
            destination.relative_to(recycle_root)
        except ValueError as exc:
            raise IngestError(f"非法回收站相对路径：{relative_path}") from exc
        meta_path = destination.with_name(destination.name + ".meta.json")
        if destination.exists() or meta_path.exists():
            return str(destination.relative_to(recycle_root.parent))

        destination.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        metadata = {
            "original_path": relative_path,
            "recycled_at": now.isoformat(),
            "expires_at": (
                now + timedelta(days=self.settings.recycle_life_days)
            ).isoformat(),
        }
        shutil.move(str(source_path), str(destination))
        try:
            _write_json_atomic(meta_path, metadata)
        except Exception:
            source_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source_path))
            raise
        return destination.relative_to(recycle_root.parent).as_posix()

    def _cleanup_recycle_impl(self, *, force: bool = False) -> dict[str, Any]:
        """清理回收站文件；``force`` 为真时永久清空全部内容。"""
        started_at = time.perf_counter()
        external_parent = self.external_dir.parent.resolve()
        recycle_root = (external_parent / "recycle").resolve()
        if recycle_root.parent != external_parent or recycle_root.name != "recycle":
            raise IngestError("回收站目录不安全，拒绝执行清理")

        if not recycle_root.exists():
            if force:
                recycle_root.mkdir(parents=True, exist_ok=True)
            result = {"deleted": 0, "forced": force}
            self._log_event(
                "recycle_cleanup",
                f"force={str(force).lower()}, deleted=0",
                _elapsed_ms(started_at),
            )
            return result

        if not recycle_root.is_dir():
            raise IngestError("回收站路径不是目录，拒绝执行清理")

        if force:
            deleted = _clear_directory_contents(recycle_root)
            result = {"deleted": deleted, "forced": True}
            self._log_event(
                "recycle_cleanup",
                f"force=true, deleted={deleted}",
                _elapsed_ms(started_at),
            )
            return result

        now = datetime.now(timezone.utc)
        deleted = 0
        for meta_path in sorted(recycle_root.rglob("*.meta.json")):
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(payload["expires_at"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

            if expires_at > now:
                continue

            base = meta_path.with_name(meta_path.name.replace(".meta.json", ""))
            meta_path.unlink(missing_ok=True)
            if base.exists():
                base.unlink()
                deleted += 1

        result = {"deleted": deleted, "forced": False}
        self._log_event(
            "recycle_cleanup",
            f"force=false, deleted={deleted}",
            _elapsed_ms(started_at),
        )
        return result

    def _import_document_impl(
        self,
        source_path: Path | str,
        *,
        ingest_after_import: bool = True,
        expected_origin_hash: str | None = None,
        _original_identity: str | None = None,
    ) -> dict[str, Any]:
        """安全转换任意受支持文件、注册来源，并可立即执行整理。"""

        started_at = time.perf_counter()
        try:
            source = _resolve_import_source(source_path)
        except DocumentImportPathError as exc:
            self._log_event(
                "document_import_failed",
                f"error={type(exc).__name__}",
                _elapsed_ms(started_at),
                level="ERROR",
            )
            raise
        suffix = source.suffix.casefold()
        if suffix not in SUPPORTED_IMPORT_SUFFIXES:
            self._log_event(
                "document_import_failed",
                f"file={source.name}, error=UnsupportedDocumentFormatError",
                _elapsed_ms(started_at),
                level="ERROR",
            )
            raise UnsupportedDocumentFormatError(
                f"不支持的文档格式：{suffix or '<无扩展名>'}"
            )
        identity = _original_identity or str(source)

        with get_knowledge_base_lock(self.data_dir):
            ingestor = Ingestor(
                data_dir=self.data_dir,
                external_dir=self.external_dir,
                settings=self.settings,
            )
            try:
                existing_mapping = ingestor.file_map.get_by_original(identity)
                markdown_relative_path = (
                    existing_mapping.markdown_path
                    if existing_mapping is not None
                    else _stable_markdown_name(source.name, identity)
                )
                destination = _safe_markdown_destination(
                    self.external_dir,
                    markdown_relative_path,
                )
                previous_bytes = (
                    destination.read_bytes() if destination.exists() else None
                )
                file_map_previous = ingestor.file_map.file_path.read_bytes()
                try:
                    with _stable_import_snapshot(
                        source,
                        expected_origin_hash=expected_origin_hash,
                    ) as snapshot:
                        size = snapshot.size
                        origin_hash = snapshot.sha256
                        origin_modified_at = snapshot.modified_at
                        self._log_event(
                            "document_import_start",
                            (
                                f"file={source.name}, "
                                f"format={suffix.removeprefix('.')}, size={size}"
                            ),
                        )
                        conversion = _public_convert_document(
                            snapshot.path,
                            self.external_dir,
                            destination_name=markdown_relative_path,
                        )
                        if _sha256_snapshot(snapshot.path) != origin_hash:
                            raise DocumentImportConflictError(
                                "导入快照在转换期间发生变化，已拒绝提交："
                                f"{source.name}"
                            )
                    self._log_event(
                        "document_convert",
                        f"file={source.name}, format={conversion['format']}",
                        _elapsed_ms(started_at),
                    )
                    ingestor.file_map.upsert(identity, markdown_relative_path)
                    ingestor.scan_sources()
                except Exception:
                    _restore_import_destination(destination, previous_bytes)
                    _write_bytes_atomic(
                        ingestor.file_map.file_path,
                        file_map_previous,
                    )
                    raise
            except (DocumentImportError, DocumentConversionError, FileMapError) as exc:
                self._log_event(
                    "document_import_failed",
                    f"file={source.name}, error={type(exc).__name__}",
                    _elapsed_ms(started_at),
                    level="ERROR",
                )
                raise
            except Exception as exc:
                self._log_event(
                    "document_import_failed",
                    f"file={source.name}, error={type(exc).__name__}",
                    _elapsed_ms(started_at),
                    level="ERROR",
                )
                raise DocumentImportError(
                    f"文档注册失败：{source.name}: {type(exc).__name__}"
                ) from exc

            source_record = _source_record_for_relative_path(
                self.paths,
                markdown_relative_path,
            )
            source_id = str(source_record["source_id"])
            content_hash = str(source_record["content_hash"])
            source_connection = connect_sources(self.paths)
            try:
                source_connection.execute(
                    """
                    UPDATE sources
                    SET origin_hash = ?, origin_size = ?, origin_modified_at = ?,
                        updated_at = ?
                    WHERE source_id = ?
                    """,
                    (
                        origin_hash,
                        size,
                        origin_modified_at,
                        _now_iso(),
                        source_id,
                    ),
                )
                source_connection.commit()
            finally:
                source_connection.close()
            ingest_status = "pending"
            ingest_result: dict[str, Any] | None = None
            ingest_error: str | None = None
            if ingest_after_import:
                try:
                    ingest_result = ingestor.ingest(
                        paths=[markdown_relative_path],
                        mode="both",
                    )
                    ingest_status = (
                        "failed" if int(ingest_result.get("failed", 0)) else "completed"
                    )
                    if ingest_status == "failed":
                        ingest_error = _ingest_error_summary(ingest_result)
                except Exception as exc:
                    ingest_status = "failed"
                    ingest_error = f"{type(exc).__name__}: {str(exc)[:240]}"

        result: dict[str, Any] = {
            "source_id": source_id,
            "original_filename": source.name,
            "detected_format": _detected_format(suffix),
            "markdown_relative_path": markdown_relative_path,
            "conversion_status": "completed",
            "ingest_status": ingest_status,
            "size": size,
            "origin_hash": origin_hash,
            "content_hash": content_hash,
            "origin_modified_at": origin_modified_at,
        }
        if ingest_result is not None:
            result["ingest"] = ingest_result
        if ingest_error:
            result["ingest_error"] = ingest_error
        self._log_event(
            "document_import_done",
            f"file={source.name}, source_id={source_id}, ingest={ingest_status}",
            _elapsed_ms(started_at),
            level="ERROR" if ingest_status == "failed" else "INFO",
        )
        return result

    def _upload_file_impl(self, content: str, filename: str) -> dict[str, Any]:
        """将文本内容保存为 Markdown 文件，并注册到 sources。"""
        safe_filename = filename.strip()
        if not safe_filename.endswith(".md"):
            safe_filename += ".md"
        safe_filename = safe_filename.replace("\\", "/").split("/")[-1]

        ingestor = Ingestor(
            data_dir=self.data_dir,
            external_dir=self.external_dir,
            settings=self.settings,
        )
        dest_path = self.external_dir / safe_filename
        with ingestor._write_lock:
            if dest_path.exists():
                raise RecycleConflictError(f"文件已存在：{safe_filename}")
            dest_path.write_text(content, encoding="utf-8")
            try:
                ingestor.scan_sources()
            except Exception:
                dest_path.unlink(missing_ok=True)
                raise

        connection = connect_sources(self.paths)
        try:
            row = connection.execute(
                "SELECT source_id, content_hash FROM sources WHERE relative_path = ?",
                (safe_filename,),
            ).fetchone()
            if row is not None:
                encoded = content.encode("utf-8")
                origin_hash = hashlib.sha256(encoded).hexdigest()
                origin_modified_at = _now_iso()
                connection.execute(
                    """
                    UPDATE sources
                    SET origin_hash = ?, origin_size = ?, origin_modified_at = ?,
                        updated_at = ?
                    WHERE source_id = ?
                    """,
                    (
                        origin_hash,
                        len(encoded),
                        origin_modified_at,
                        origin_modified_at,
                        row["source_id"],
                    ),
                )
                connection.commit()
            else:
                origin_hash = None
                origin_modified_at = None
        finally:
            connection.close()
        result = {
            "source_id": row["source_id"] if row is not None else None,
            "filename": safe_filename,
            "path": dest_path.relative_to(self.external_dir).as_posix(),
            "size": len(content),
            "origin_hash": origin_hash,
            "content_hash": row["content_hash"] if row is not None else None,
            "origin_modified_at": origin_modified_at,
        }
        self._log_event(
            "document_import_done",
            f"file={safe_filename}, source_id={result['source_id']}, ingest=pending",
        )
        return result

    def ingest(
        self,
        paths: Sequence[Path | str] | None = None,
        mode: str = "both",
    ) -> dict[str, Any]:
        return Ingestor(
            data_dir=self.data_dir,
            external_dir=self.external_dir,
            settings=self.settings,
        ).ingest(paths=paths, mode=mode)

    def _sync_sources_impl(
        self,
        records: Sequence[dict[str, Any]],
        *,
        ingest_after_sync: bool = False,
    ) -> dict[str, Any]:
        """同步上游表记录；kemo-graph 只维护派生 Markdown、Graph 与 RAG。"""

        from .source_sync import sync_external_sources

        return sync_external_sources(
            self,
            records,
            ingest_after_sync=ingest_after_sync,
        )

    def _list_synced_sources_impl(
        self,
        *,
        source_type: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """分页读取由外部权威数据源同步而来的记录。"""

        from .source_sync import list_external_sources

        return list_external_sources(
            self,
            source_type=source_type,
            include_deleted=include_deleted,
            page=page,
            page_size=page_size,
        )

    def _delete_synced_sources_impl(self, source_uris: Sequence[str]) -> dict[str, Any]:
        """按稳定 URI 删除外部派生数据，不把派生 Markdown 放入回收站。"""

        from .source_sync import delete_external_sources

        return delete_external_sources(self, source_uris)

    def _delete_document_impl(self, source_id: str) -> dict[str, Any]:
        self._require_initialized()
        result = Ingestor(
            data_dir=self.data_dir,
            external_dir=self.external_dir,
            settings=self.settings,
        ).delete_document(source_id)
        # 删除属于隐私边界：旧缓存可能包含已删除正文，因此不保留结果历史。
        try:
            result["search_cache_deleted"] = self.clear_search_cache()["deleted"]
        except Exception:
            result["search_cache_deleted"] = 0
        return result

    def _delete_documents_impl(self, source_ids: Sequence[str]) -> dict[str, Any]:
        """逐一精确删除活动文档，并以结构化结果报告部分失败。"""

        self._require_initialized()
        if isinstance(source_ids, (str, bytes)):
            raise TypeError("source_ids 必须是字符串数组")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in source_ids:
            source_id = str(value).strip()
            if not source_id:
                raise ValueError("source_ids 中不能包含空值")
            if source_id not in seen:
                seen.add(source_id)
                normalized.append(source_id)
        if not normalized:
            raise ValueError("至少选择一篇文档")
        if len(normalized) > 1000:
            raise ValueError("单次最多删除 1000 篇文档")

        ingestor = Ingestor(
            data_dir=self.data_dir,
            external_dir=self.external_dir,
            settings=self.settings,
        )
        deleted: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        with ingestor._write_lock:
            for source_id in normalized:
                try:
                    deleted.append(ingestor.delete_document(source_id))
                except Exception as exc:
                    failures.append(
                        {
                            "source_id": source_id,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
        cache_deleted = 0
        if deleted:
            try:
                cache_deleted = int(self.clear_search_cache()["deleted"])
            except Exception:
                cache_deleted = 0
        self._log_event(
            "delete_documents",
            f"requested={len(normalized)}, deleted={len(deleted)}, failed={len(failures)}",
            level="WARNING" if failures else "INFO",
        )
        return {
            "requested": len(normalized),
            "deleted": len(deleted),
            "failed": len(failures),
            "documents": deleted,
            "failures": failures,
            "search_cache_deleted": cache_deleted,
        }

    def _delete_all_documents_impl(self) -> dict[str, Any]:
        """删除当前知识库内的全部活动文档，不跨越 Store 边界。"""

        self._require_initialized()
        connection = connect_sources(self.paths)
        try:
            source_ids = [
                str(row["source_id"])
                for row in connection.execute(
                    """
                    SELECT source_id FROM sources
                    WHERE exists_status = 'active'
                    ORDER BY relative_path, source_id
                    """
                ).fetchall()
            ]
        finally:
            connection.close()
        if not source_ids:
            return {
                "requested": 0,
                "deleted": 0,
                "failed": 0,
                "documents": [],
                "failures": [],
                "search_cache_deleted": 0,
            }
        return self.delete_documents(source_ids)
