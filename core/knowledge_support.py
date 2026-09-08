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
    MAX_IMPORT_BYTES,
    SUPPORTED_IMPORT_SUFFIXES,
    DocumentImportConflictError,
    DocumentImportPathError,
    DocumentTooLargeError,
    _ImportSnapshot,
)


def _public_chat(*args: Any, **kwargs: Any) -> Any:
    """Resolve the facade symbol lazily so existing patch/injection points remain valid."""

    from . import knowledge_base

    return knowledge_base.chat(*args, **kwargs)


def _public_convert_document(*args: Any, **kwargs: Any) -> Any:
    """Resolve document conversion through the public facade patch point."""

    from . import knowledge_base

    return knowledge_base.convert_document(*args, **kwargs)

def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise ValueError("图谱中的 JSON 数组字段格式错误")
    return parsed


def _relation_row(row: Any) -> dict[str, Any]:
    source_keyword = str(row["source_keyword"])
    relation = str(row["relation"])
    target_keyword = str(row["target_keyword"])
    return {
        "edge_id": row["edge_id"],
        "source_node_id": row["source_node_id"],
        "source_keyword": source_keyword,
        "relation": relation,
        "target_node_id": row["target_node_id"],
        "target_keyword": target_keyword,
        "path": f"{source_keyword}->[{relation}]->{target_keyword}",
        "weight": float(row["weight"] or 0.0),
        "support_count": int(row["support_count"] or 0),
        "created_at": row["created_at"],
    }


def _clear_directory_contents(root: Path) -> int:
    """删除目录中的全部内容且不跟随符号链接，返回永久删除的文件数。"""

    deleted = 0
    for child in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        junction_check = getattr(child, "is_junction", None)
        is_junction = bool(junction_check and junction_check())
        if child.is_symlink() or is_junction:
            if not child.name.endswith(".meta.json"):
                deleted += 1
            if is_junction and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
            continue
        if child.is_dir():
            deleted += _clear_directory_contents(child)
            child.rmdir()
            continue
        child.unlink()
        if not child.name.endswith(".meta.json"):
            deleted += 1
    return deleted


def _resolve_import_source(value: Path | str) -> Path:
    if not isinstance(value, (str, Path)):
        raise DocumentImportPathError("source_path 必须是路径字符串")
    raw = os.fspath(value).strip()
    if not raw:
        raise DocumentImportPathError("source_path 不能为空")
    candidate = Path(raw).expanduser()
    if ".." in candidate.parts:
        raise DocumentImportPathError("导入路径不能包含 '..'")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve(strict=False)
    if not resolved.exists():
        raise DocumentImportPathError(f"文件不存在：{resolved.name}")
    if not resolved.is_file():
        raise DocumentImportPathError(f"路径不是普通文件：{resolved.name}")
    try:
        with resolved.open("rb") as stream:
            stream.read(1)
    except OSError as exc:
        raise DocumentImportPathError(f"文件不可读：{resolved.name}") from exc
    return resolved


def _stable_markdown_name(filename: str, identity: str) -> str:
    stem = Path(filename).stem
    safe_stem = (
        "".join(
            "_" if character in '<>:"/\\|?*' or ord(character) < 32 else character
            for character in stem
        ).strip(" .")
        or "document"
    )
    digest = hashlib.sha256(
        os.path.normcase(os.path.normpath(identity)).encode("utf-8")
    ).hexdigest()[:10]
    return f"{safe_stem}-{digest}.md"


def _safe_markdown_destination(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise DocumentImportPathError("Markdown 映射路径不安全")
    destination = (root / relative).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError as exc:
        raise DocumentImportPathError("Markdown 映射路径超出目标目录") from exc
    if destination.suffix.casefold() != ".md":
        raise DocumentImportPathError("Markdown 映射路径必须以 .md 结尾")
    return destination


def _restore_import_destination(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.restore")
    try:
        temporary.write_bytes(previous)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _source_record_for_relative_path(
    paths: Any,
    relative_path: str,
) -> dict[str, Any]:
    connection = connect_sources(paths)
    try:
        row = connection.execute(
            """
            SELECT source_id, content_hash FROM sources
            WHERE relative_path = ? AND exists_status = 'active'
            """,
            (relative_path,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise DocumentImportError(f"导入后未注册来源：{relative_path}")
    return {
        "source_id": str(row["source_id"]),
        "content_hash": str(row["content_hash"]),
    }


def _source_id_for_relative_path(paths: Any, relative_path: str) -> str:
    """兼容内部旧调用；新代码应读取完整来源记录。"""

    return str(_source_record_for_relative_path(paths, relative_path)["source_id"])


def _normalize_expected_origin_hash(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expected_origin_hash 必须是 SHA-256 字符串")
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("expected_origin_hash 必须是 64 位 SHA-256 十六进制字符串")
    return normalized


def _sha256_snapshot(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise DocumentImportConflictError("无法复核导入快照") from exc
    return digest.hexdigest()


def _same_import_source_state(
    before: os.stat_result,
    after: os.stat_result,
    *,
    compare_change_time: bool = False,
) -> bool:
    try:
        same_object = os.path.samestat(before, after)
    except (AttributeError, OSError):
        same_object = (
            getattr(before, "st_dev", None) == getattr(after, "st_dev", None)
            and getattr(before, "st_ino", None) == getattr(after, "st_ino", None)
        )
    unchanged = bool(
        same_object
        and stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
    )
    if compare_change_time:
        unchanged = bool(
            unchanged
            and getattr(before, "st_ctime_ns", None)
            == getattr(after, "st_ctime_ns", None)
        )
    return unchanged


@contextmanager
def _stable_import_snapshot(
    source: Path,
    *,
    expected_origin_hash: str | None = None,
) -> Iterator[_ImportSnapshot]:
    """复制并锁定一次导入内容，使哈希与转换消费同一份字节。"""

    expected_hash = _normalize_expected_origin_hash(expected_origin_hash)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(source, flags)
        stream = os.fdopen(descriptor, "rb", closefd=True)
        descriptor = None
    except (OSError, ValueError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise DocumentImportPathError(f"无法读取文件内容：{source.name}") from exc

    with stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise DocumentImportPathError(f"路径不是普通文件：{source.name}")
        if before.st_size > MAX_IMPORT_BYTES:
            raise DocumentTooLargeError(
                f"文件超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MB 上限：{source.name}"
            )

        with tempfile.TemporaryDirectory(
            prefix="kemo-graph-import-snapshot-"
        ) as temporary_dir:
            snapshot_path = Path(temporary_dir) / _snapshot_filename(source)
            digest = hashlib.sha256()
            total = 0
            snapshot_descriptor: int | None = None
            try:
                snapshot_descriptor = os.open(
                    snapshot_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                snapshot_stream = os.fdopen(
                    snapshot_descriptor,
                    "wb",
                    closefd=True,
                )
                snapshot_descriptor = None
                with snapshot_stream:
                    while block := stream.read(1024 * 1024):
                        total += len(block)
                        if total > MAX_IMPORT_BYTES:
                            raise DocumentTooLargeError(
                                "文件在读取期间超过 "
                                f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB 上限："
                                f"{source.name}"
                            )
                        digest.update(block)
                        snapshot_stream.write(block)
                after = os.fstat(stream.fileno())
            except (DocumentImportError, OSError, ValueError):
                if snapshot_descriptor is not None:
                    os.close(snapshot_descriptor)
                raise
            try:
                current = source.stat()
            except OSError as exc:
                raise DocumentImportConflictError(
                    f"源文件在读取期间不可用，请重新扫描后重试：{source.name}"
                ) from exc

            if (
                total != before.st_size
                or not _same_import_source_state(
                    before,
                    after,
                    compare_change_time=True,
                )
                or not _same_import_source_state(before, current)
            ):
                raise DocumentImportConflictError(
                    f"源文件在读取期间发生变化，请重新扫描后重试：{source.name}"
                )

            origin_hash = digest.hexdigest()
            if expected_hash is not None and origin_hash != expected_hash:
                raise DocumentImportConflictError(
                    f"源文件内容与调用方确认的哈希不一致，请重新扫描后重试：{source.name}"
                )

            yield _ImportSnapshot(
                path=snapshot_path,
                size=total,
                sha256=origin_hash,
                modified_at=datetime.fromtimestamp(
                    before.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
            )


def _snapshot_filename(source: Path) -> str:
    """保留转换所需的原始 basename，同时生成跨平台安全的快照名。"""

    sanitized = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", source.name).rstrip(" .")
    suffix = source.suffix
    if not sanitized or sanitized in {".", ".."}:
        sanitized = f"source{suffix.casefold()}"
    stem = Path(sanitized).stem
    if stem.casefold() in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }:
        sanitized = f"_{sanitized}"
    if len(sanitized) > 180:
        safe_suffix = Path(sanitized).suffix
        stem_limit = max(1, 180 - len(safe_suffix))
        sanitized = f"{Path(sanitized).stem[:stem_limit]}{safe_suffix}"
    return sanitized


def _detected_format(suffix: str) -> str:
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix in {".html", ".htm"}:
        return "html"
    return suffix.removeprefix(".")


def _ingest_error_summary(result: dict[str, Any]) -> str:
    messages: list[str] = []
    for detail in result.get("details", []):
        if not isinstance(detail, dict):
            continue
        for key in ("error", "graph_error", "rag_error"):
            value = detail.get(key)
            if value:
                messages.append(str(value)[:160])
    return "; ".join(messages[:3]) or "文档整理返回失败状态"


def _build_answer_context(retrieval: Any) -> dict[str, Any]:
    """将混合检索结果压缩为可控、可读且不丢失来源的 LLM 上下文。"""

    if not isinstance(retrieval, dict):
        retrieval = {}
    graph = retrieval.get("graph")
    rag = retrieval.get("rag")
    graph = graph if isinstance(graph, dict) else {}
    rag = rag if isinstance(rag, dict) else {}

    raw_nodes = _dict_items(graph.get("hit_nodes")) + _dict_items(
        graph.get("expanded_nodes")
    )
    nodes: list[dict[str, Any]] = []
    node_names: dict[str, str] = {}
    seen_node_ids: set[str] = set()
    for item in raw_nodes:
        node_id = str(item.get("node_id") or "").strip()
        keyword = str(item.get("keyword") or node_id).strip()
        identity = node_id or keyword.casefold()
        if not identity or identity in seen_node_ids:
            continue
        seen_node_ids.add(identity)
        if node_id:
            node_names[node_id] = keyword or node_id
        nodes.append(
            {
                "node_id": node_id,
                "keyword": keyword,
                "summary": _clip_context_text(item.get("summary"), 800),
                "aliases": item.get("aliases") if isinstance(item.get("aliases"), list) else [],
                "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
                "match_score": item.get("match_score"),
                "depth": item.get("depth"),
            }
        )
        if len(nodes) >= 30:
            break

    relationships: list[dict[str, Any]] = []
    for edge in _dict_items(graph.get("edges"))[:40]:
        source_id = str(edge.get("source_node_id") or "")
        target_id = str(edge.get("target_node_id") or "")
        source = node_names.get(source_id, source_id)
        target = node_names.get(target_id, target_id)
        relation = _clip_context_text(edge.get("relation"), 160)
        relationships.append(
            {
                "text": f"{source} →[{relation}]→ {target}",
                "weight": edge.get("weight"),
            }
        )

    relationship_paths = [
        _clip_context_text(path.get("text"), 700)
        for path in _dict_items(graph.get("paths"))[:20]
        if str(path.get("text") or "").strip()
    ]
    groups = [
        {
            "group_id": str(item.get("group_id") or ""),
            "summary": _clip_context_text(item.get("summary"), 1200),
            "node_ids": item.get("node_ids") if isinstance(item.get("node_ids"), list) else [],
        }
        for item in _dict_items(graph.get("groups"))[:8]
    ]

    rag_passages: list[dict[str, Any]] = []
    for item in _dict_items(rag.get("results"))[:12]:
        source = item.get("source")
        source = source if isinstance(source, dict) else {}
        parent = item.get("context")
        parent = parent if isinstance(parent, dict) else {}
        matched_content = _clip_context_text(item.get("content"), 1600)
        parent_content = _clip_context_text(parent.get("content"), 3200)
        rag_passages.append(
            {
                "chunk_id": str(item.get("chunk_id") or ""),
                "content": parent_content or matched_content,
                "matched_content": (
                    matched_content
                    if parent_content and matched_content != parent_content
                    else ""
                ),
                "score": item.get("score"),
                "granularity": item.get("granularity"),
                "context_granularity": parent.get("granularity"),
                "source": str(
                    source.get("relative_path") or source.get("source_id") or "未知来源"
                ),
            }
        )

    semantic_entities = [
        {
            "node_id": str(item.get("node_id") or ""),
            "keyword": str(item.get("keyword") or ""),
            "summary": _clip_context_text(item.get("summary"), 800),
            "score": item.get("score"),
        }
        for item in _dict_items(retrieval.get("entities"))[:10]
    ]
    semantic_communities = [
        {
            "group_id": str(item.get("group_id") or ""),
            "summary": _clip_context_text(item.get("summary"), 1200),
            "score": item.get("score"),
        }
        for item in _dict_items(retrieval.get("communities"))[:6]
    ]
    return {
        "graph_nodes": nodes,
        "relationships": relationships,
        "relationship_paths": relationship_paths,
        "groups": groups,
        "rag_passages": rag_passages,
        "semantic_entities": semantic_entities,
        "semantic_communities": semantic_communities,
    }


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _load_source_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clip_context_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit].rstrip()}…"


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
