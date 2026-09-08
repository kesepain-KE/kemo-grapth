from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import start
from api import create_app
from core.config import AppConfig
from core.db import initialize_databases
from core.portable_store import (
    STORAGE_DIRECTORY_NAME,
    PortableStoreAccessError,
    PortableStoreError,
    PortableStoreNotInitializedError,
    create_store_service,
    federated_query,
    initialize_store,
    load_store_manifest,
    resolve_store_paths,
)


def _settings(root: Path, *, allowed_roots: list[str] | None = None) -> AppConfig:
    return AppConfig(
        log_dir=str(root / "log"),
        portable_stores={
            "enabled": True,
            "allowed_roots": allowed_roots or [],
        },
    )


def _config_file(root: Path, settings: AppConfig) -> Path:
    path = root / "config.json"
    path.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_store_requires_absolute_root_and_uses_visible_fixed_layout(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(PortableStoreAccessError):
        resolve_store_paths("relative/store", settings=settings)
    with pytest.raises(PortableStoreAccessError):
        resolve_store_paths(
            tmp_path / STORAGE_DIRECTORY_NAME,
            settings=settings,
        )

    store_root = tmp_path / "memory" / "permanent"
    first = initialize_store(
        store_root,
        scope="memory.permanent",
        owner_id="user-001",
        display_name="永久记忆",
        settings=settings,
    )
    second = initialize_store(
        store_root,
        scope="memory.permanent",
        owner_id="user-001",
        settings=settings,
    )
    paths = resolve_store_paths(store_root, settings=settings, require_initialized=True)

    assert first["manifest"]["store_id"] == second["manifest"]["store_id"]
    assert first["initialized_now"] is True
    assert second["initialized_now"] is False
    assert paths.storage_root == store_root / STORAGE_DIRECTORY_NAME
    assert paths.manifest_path.is_file()
    assert paths.databases.sources_db.is_file()
    assert paths.databases.search_cache_db.is_file()
    assert paths.databases.graph_db.is_file()
    assert paths.databases.rag_db.is_file()
    assert paths.databases.vector_index_dir.is_dir()
    assert paths.external_dir == paths.storage_root / "content" / "markdown"
    assert paths.recycle_dir == paths.storage_root / "content" / "recycle"

    with pytest.raises(PortableStoreError):
        initialize_store(store_root, scope="knowledge.user", settings=settings)

    paths.databases.rag_db.unlink()
    with pytest.raises(PortableStoreNotInitializedError, match="结构不完整"):
        create_store_service(store_root, settings=settings)
    repaired = initialize_store(
        store_root,
        scope="memory.permanent",
        owner_id="user-001",
        settings=settings,
    )
    assert repaired["manifest"]["store_id"] == first["manifest"]["store_id"]
    assert paths.databases.rag_db.is_file()


def test_sources_origin_columns_migrate_in_place_without_losing_rows(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "legacy-data"
    data_dir.mkdir()
    sources_db = data_dir / "sources.db"
    with sqlite3.connect(sources_db) as connection:
        connection.execute(
            """
            CREATE TABLE sources (
                source_id TEXT PRIMARY KEY,
                original_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                path_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                graph_hash TEXT,
                rag_hash TEXT,
                graph_status TEXT DEFAULT 'pending',
                rag_status TEXT DEFAULT 'pending',
                exists_status TEXT DEFAULT 'active',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO sources (
                source_id, original_path, relative_path, path_hash, content_hash
            ) VALUES ('legacy', 'D:/legacy.txt', 'legacy.md', 'p', 'c')
            """
        )
        connection.commit()

    initialize_databases(data_dir, _settings(tmp_path))
    with sqlite3.connect(sources_db) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(sources)")
        }
        row = connection.execute(
            "SELECT source_id, origin_hash, origin_size, origin_modified_at FROM sources"
        ).fetchone()
    assert {"origin_hash", "origin_size", "origin_modified_at"} <= columns
    assert row == ("legacy", None, None, None)


def test_allowed_roots_and_nonempty_manifest_guard(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    settings = _settings(tmp_path, allowed_roots=[str(allowed)])
    initialize_store(
        allowed / "knowledge",
        scope="knowledge.user",
        settings=settings,
    )
    with pytest.raises(PortableStoreAccessError):
        initialize_store(denied / "knowledge", scope="knowledge.user", settings=settings)

    broken_root = allowed / "broken"
    storage = broken_root / STORAGE_DIRECTORY_NAME
    storage.mkdir(parents=True)
    (storage / "unknown.txt").write_text("occupied", encoding="utf-8")
    with pytest.raises(PortableStoreError, match="非空"):
        initialize_store(broken_root, scope="knowledge.user", settings=settings)


def test_absolute_import_records_both_hashes_and_keeps_stores_isolated(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    source = tmp_path / "outside" / "notes.txt"
    source.parent.mkdir()
    source.write_text("portable knowledge", encoding="utf-8")
    origin_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    roots = [tmp_path / "store-a", tmp_path / "store-b"]
    for index, root in enumerate(roots):
        initialize_store(
            root,
            scope="knowledge.user",
            owner_id=f"user-{index}",
            settings=settings,
        )
    first_service = create_store_service(
        roots[0], settings=settings, config_path=config_path
    )
    second_service = create_store_service(
        roots[1], settings=settings, config_path=config_path
    )

    first = first_service.import_document(source, ingest_after_import=False)
    second = second_service.import_document(source, ingest_after_import=False)
    assert first["origin_hash"] == origin_hash
    assert first["content_hash"] != origin_hash
    assert first_service.data_dir == roots[0] / STORAGE_DIRECTORY_NAME
    assert second_service.data_dir == roots[1] / STORAGE_DIRECTORY_NAME

    first_paths = resolve_store_paths(roots[0], settings=settings)
    with sqlite3.connect(first_paths.databases.sources_db) as connection:
        row = connection.execute(
            """
            SELECT original_path, origin_hash, origin_size, origin_modified_at,
                   content_hash, graph_status, rag_status, exists_status
            FROM sources WHERE source_id = ?
            """,
            (first["source_id"],),
        ).fetchone()
    assert row is not None
    assert row[0] == str(source.resolve())
    assert row[1] == origin_hash
    assert row[2] == source.stat().st_size
    assert row[3]
    assert row[4] == first["content_hash"]
    assert row[5:] == ("pending", "pending", "active")

    first_service.delete_document(first["source_id"])
    assert first_service.list_documents(status="active")["pagination"]["total"] == 0
    assert second_service.list_documents(status="active")["pagination"]["total"] == 1
    assert second_service.get_document_content(second["source_id"])["content"]


def test_federated_query_isolates_failures_and_labels_results(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    valid = tmp_path / "valid"
    missing = tmp_path / "missing"
    initialized = initialize_store(
        valid,
        scope="knowledge.shared",
        settings=settings,
    )

    with patch(
        "core.portable_store._query_store",
        return_value={"query": "q", "results": [{"chunk_id": 1, "score": 0.8}]},
    ):
        result = federated_query(
            [valid, missing],
            "q",
            mode="rag",
            settings=settings,
            config_path=config_path,
        )

    assert result["stores_requested"] == 2
    assert result["stores_succeeded"] == 1
    assert len(result["stores_failed"]) == 1
    assert result["merged_results"][0]["federated_score"] == 0.8
    assert (
        result["merged_results"][0]["store"]["store_id"]
        == initialized["manifest"]["store_id"]
    )


def test_node_and_relation_read_delete_contract_uses_readable_path(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    root = tmp_path / "relations"
    initialize_store(root, scope="knowledge.shared", settings=settings)
    source = tmp_path / "relation-source.md"
    source.write_text("A relates to B", encoding="utf-8")
    service = create_store_service(root, settings=settings, config_path=config_path)
    imported = service.import_document(source, ingest_after_import=False)

    paths = resolve_store_paths(root, settings=settings)
    with sqlite3.connect(paths.databases.graph_db) as connection:
        connection.execute(
            """
            INSERT INTO nodes (
                node_id, keyword, summary, aliases, tags, weight, ref_count,
                created_at, updated_at
            ) VALUES ('node-a', 'A', 'source', '[]', '[]', 1.2, 1, 'now', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO nodes (
                node_id, keyword, summary, aliases, tags, weight, ref_count,
                created_at, updated_at
            ) VALUES ('node-b', 'B', 'target', '[]', '[]', 1.1, 1, 'now', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO node_sources (
                node_id, source_id, content_hash, evidence_weight, evidence
            ) VALUES ('node-a', ?, ?, 0.9, 'A evidence')
            """,
            (imported["source_id"], imported["content_hash"]),
        )
        for mention_id, local_id, keyword, node_id in (
            ("mention-a", "local-a", "A", "node-a"),
            ("mention-b", "local-b", "B", "node-b"),
        ):
            connection.execute(
                """
                INSERT INTO entity_mentions (
                    mention_id, source_id, content_hash, local_id, keyword,
                    summary, aliases, tags, evidence_weight, evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', 1, 'evidence', 'now')
                """,
                (
                    mention_id,
                    imported["source_id"],
                    imported["content_hash"],
                    local_id,
                    keyword,
                    keyword,
                ),
            )
            connection.execute(
                "INSERT INTO mention_nodes (mention_id, node_id) VALUES (?, ?)",
                (mention_id, node_id),
            )
        connection.execute(
            """
            INSERT INTO relation_mentions (
                mention_id, source_id, content_hash, source_mention_id, relation,
                target_mention_id, evidence_weight, evidence, created_at
            ) VALUES (
                'relation-mention', ?, ?, 'mention-a', '关联', 'mention-b',
                0.8, 'A relates B', 'now'
            )
            """,
            (imported["source_id"], imported["content_hash"]),
        )
        connection.execute(
            """
            INSERT INTO edges (
                edge_id, source_node_id, relation, target_node_id,
                weight, support_count, created_at
            ) VALUES ('edge-1', 'node-a', '关联', 'node-b', 0.8, 1, 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO edge_sources (
                edge_id, source_id, content_hash, evidence_weight
            ) VALUES ('edge-1', ?, ?, 0.8)
            """,
            (imported["source_id"], imported["content_hash"]),
        )
        connection.commit()

    relation = service.get_relation("edge-1")
    assert relation["path"] == "A->[关联]->B"
    assert relation["sources"][0]["origin_hash"] == imported["origin_hash"]
    node = service.get_node("node-a")
    assert node["weight"] == 1.2
    assert node["relations"][0]["path"] == "A->[关联]->B"
    full = service.get_full_graph()
    assert full["edges"][0]["path"] == "A->[关联]->B"

    app = create_app(
        config_path=config_path,
        data_dir=tmp_path / "default-api-data",
        external_dir=tmp_path / "default-api-markdown",
    )
    with TestClient(app) as client:
        api_relation = client.post(
            "/api/v1/stores/relations/get",
            json={"store_root": str(root), "edge_id": "edge-1"},
        )
        assert api_relation.status_code == 200
        assert api_relation.json()["data"]["result"]["path"] == "A->[关联]->B"
        api_deleted = client.post(
            "/api/v1/stores/relations/delete",
            json={"store_root": str(root), "edge_id": "edge-1"},
        )
        assert api_deleted.status_code == 200
        deleted = api_deleted.json()["data"]["result"]
    assert deleted["deleted"] is True
    assert deleted["deleted_evidence_count"] == 1
    assert deleted["deleted_mention_count"] == 1
    with sqlite3.connect(paths.databases.graph_db) as connection:
        mention_count = connection.execute(
            "SELECT COUNT(*) FROM relation_mentions WHERE mention_id = 'relation-mention'"
        ).fetchone()[0]
    assert mention_count == 0
    with pytest.raises(Exception, match="关系不存在"):
        service.get_relation("edge-1")


def test_portable_full_rebuild_preserves_manifest_content_and_origin_metadata(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    root = tmp_path / "rebuild-store"
    initialize_store(
        root,
        scope="memory.permanent",
        owner_id="user-1",
        settings=settings,
    )
    source = tmp_path / "rebuild-source.txt"
    source.write_text("preserve me", encoding="utf-8")
    service = create_store_service(root, settings=settings, config_path=config_path)
    imported = service.import_document(source, ingest_after_import=False)
    paths = resolve_store_paths(root, settings=settings)
    manifest_before = paths.manifest_path.read_bytes()
    markdown_path = paths.external_dir / imported["markdown_relative_path"]
    markdown_before = markdown_path.read_bytes()

    with (
        patch(
            "core.rebuilder.Ingestor.ingest",
            return_value={"failed": 0, "processed": 1, "details": []},
        ),
        patch(
            "core.rebuilder._validate_shadow",
            return_value={"active_sources": 1, "faiss_healthy": True},
        ),
    ):
        result = service.rebuild_all()

    assert paths.manifest_path.read_bytes() == manifest_before
    assert markdown_path.read_bytes() == markdown_before
    assert load_store_manifest(root, settings=settings).owner_id == "user-1"
    backup = Path(result["backup_path"])
    assert backup.is_dir()
    assert (backup / "sources.db").is_file()
    assert (backup / "Graph").is_dir()
    assert (backup / "RAG").is_dir()
    assert not (backup / "manifest.json").exists()
    assert not (backup / "content").exists()
    with sqlite3.connect(paths.databases.sources_db) as connection:
        row = connection.execute(
            """
            SELECT source_id, origin_hash, origin_size, origin_modified_at
            FROM sources WHERE source_id = ?
            """,
            (imported["source_id"],),
        ).fetchone()
    assert row == (
        imported["source_id"],
        imported["origin_hash"],
        source.stat().st_size,
        imported["origin_modified_at"],
    )


def test_portable_node_delete_cascades_source_mentions_and_recycle(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    root = tmp_path / "delete-node-store"
    initialize_store(root, scope="knowledge.user", settings=settings)
    source = tmp_path / "node-source.md"
    source.write_text("Only node", encoding="utf-8")
    service = create_store_service(root, settings=settings, config_path=config_path)
    imported = service.import_document(source, ingest_after_import=False)
    paths = resolve_store_paths(root, settings=settings)
    with sqlite3.connect(paths.databases.graph_db) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO nodes (
                node_id, keyword, summary, aliases, tags, weight, ref_count,
                created_at, updated_at
            ) VALUES ('only-node', 'Only', 'only', '[]', '[]', 1, 1, 'now', 'now')
            """
        )
        connection.execute(
            """
            INSERT INTO node_sources (
                node_id, source_id, content_hash, evidence_weight, evidence
            ) VALUES ('only-node', ?, ?, 1, 'Only node')
            """,
            (imported["source_id"], imported["content_hash"]),
        )
        connection.execute(
            """
            INSERT INTO entity_mentions (
                mention_id, source_id, content_hash, local_id, keyword, summary,
                aliases, tags, evidence_weight, evidence, created_at
            ) VALUES (
                'mention-only', ?, ?, 'local-only', 'Only', 'only',
                '[]', '[]', 1, 'Only node', 'now'
            )
            """,
            (imported["source_id"], imported["content_hash"]),
        )
        connection.execute(
            "INSERT INTO mention_nodes (mention_id, node_id) VALUES ('mention-only', 'only-node')"
        )
        connection.commit()

    result = service.delete_node("only-node")
    assert result["deleted_source_ids"] == [imported["source_id"]]
    assert result["recycled_files"] == [imported["markdown_relative_path"]]
    assert not (paths.external_dir / imported["markdown_relative_path"]).exists()
    assert (paths.recycle_dir / imported["markdown_relative_path"]).is_file()
    with sqlite3.connect(paths.databases.sources_db) as connection:
        status = connection.execute(
            "SELECT exists_status FROM sources WHERE source_id = ?",
            (imported["source_id"],),
        ).fetchone()[0]
    with sqlite3.connect(paths.databases.graph_db) as connection:
        counts = connection.execute(
            """
            SELECT (SELECT COUNT(*) FROM nodes WHERE node_id = 'only-node'),
                   (SELECT COUNT(*) FROM entity_mentions WHERE mention_id = 'mention-only'),
                   (SELECT COUNT(*) FROM mention_nodes WHERE mention_id = 'mention-only')
            """
        ).fetchone()
    assert status == "deleted"
    assert counts == (0, 0, 0)


def test_store_api_is_explicit_and_covers_lifecycle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    app = create_app(
        config_path=config_path,
        data_dir=tmp_path / "default-data",
        external_dir=tmp_path / "default-markdown",
    )
    store_root = tmp_path / "api-store"
    source = tmp_path / "source.txt"
    source.write_text("API portable knowledge", encoding="utf-8")

    with TestClient(app) as client:
        initialized = client.post(
            "/api/v1/stores/initialize",
            json={
                "store_root": str(store_root),
                "scope": "knowledge.global",
                "display_name": "组织全局知识",
            },
        )
        assert initialized.status_code == 200
        changed = client.post(
            "/api/v1/stores/import-path",
            json={
                "store_root": str(store_root),
                "path": str(source),
                "ingest_after_import": False,
                "expected_origin_hash": "0" * 64,
            },
        )
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "IMPORT_SOURCE_CHANGED"
        after_conflict = client.post(
            "/api/v1/stores/documents/list",
            json={"store_root": str(store_root), "page": 1, "page_size": 5},
        )
        assert after_conflict.status_code == 200
        assert after_conflict.json()["data"]["result"]["pagination"]["total"] == 0

        expected_origin_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        imported = client.post(
            "/api/v1/stores/import-path",
            json={
                "store_root": str(store_root),
                "path": str(source),
                "ingest_after_import": False,
                "expected_origin_hash": expected_origin_hash.upper(),
            },
        )
        assert imported.status_code == 200
        imported_data = imported.json()["data"]
        assert imported_data["store"]["scope"] == "knowledge.global"
        assert imported_data["result"]["origin_hash"] == expected_origin_hash

        documents = client.post(
            "/api/v1/stores/documents/list",
            json={"store_root": str(store_root), "page": 1, "page_size": 5},
        )
        assert documents.status_code == 200
        assert documents.json()["data"]["result"]["pagination"]["total"] == 1
        status = client.post(
            "/api/v1/stores/status",
            json={"store_root": str(store_root)},
        )
        assert status.json()["data"]["result"]["sources"]["active"] == 1

        relative = client.post(
            "/api/v1/stores/initialize",
            json={"store_root": "relative", "scope": "knowledge.user"},
        )
        assert relative.status_code == 403
        assert relative.json()["error"]["code"] == "STORE_ACCESS_DENIED"
        missing = client.post(
            "/api/v1/stores/status",
            json={"store_root": str(tmp_path / "not-initialized")},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "STORE_NOT_INITIALIZED"

        paths = set(app.openapi()["paths"])
        required = {
            "/api/v1/stores/initialize",
            "/api/v1/stores/import-path",
            "/api/v1/stores/import",
            "/api/v1/stores/ingest",
            "/api/v1/stores/sources/sync",
            "/api/v1/stores/sources/status",
            "/api/v1/stores/sources/delete",
            "/api/v1/stores/query/graph",
            "/api/v1/stores/query/rag",
            "/api/v1/stores/query/hybrid",
            "/api/v1/stores/query/answer",
            "/api/v1/stores/query/global",
            "/api/v1/stores/query/federated",
            "/api/v1/stores/documents/delete",
            "/api/v1/stores/documents/update",
            "/api/v1/stores/documents/delete-batch",
            "/api/v1/stores/documents/delete-all",
            "/api/v1/stores/nodes/delete",
            "/api/v1/stores/nodes/get",
            "/api/v1/stores/relations/get",
            "/api/v1/stores/relations/delete",
            "/api/v1/stores/graph/full",
            "/api/v1/stores/graph/neighborhood",
            "/api/v1/stores/maintenance/organize-graph",
            "/api/v1/stores/maintenance/rebuild-knowledge-base",
            "/api/v1/stores/maintenance/rebuild-all",
        }
        assert required <= paths


def test_store_api_accepts_multipart_document_upload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    app = create_app(
        config_path=config_path,
        data_dir=tmp_path / "default-data",
        external_dir=tmp_path / "default-markdown",
    )
    store_root = tmp_path / "multipart-store"

    with TestClient(app) as client:
        initialized = client.post(
            "/api/v1/stores/initialize",
            json={
                "store_root": str(store_root),
                "scope": "knowledge.global",
                "display_name": "multipart portable store",
            },
        )
        assert initialized.status_code == 200

        imported = client.post(
            "/api/v1/stores/import?ingest=false",
            data={"store_root": str(store_root)},
            files={"file": ("notes.txt", b"portable upload", "text/plain")},
        )
        assert imported.status_code == 200
        payload = imported.json()["data"]
        assert payload["store"]["scope"] == "knowledge.global"
        assert payload["result"]["ingest_status"] == "pending"
        assert payload["result"]["original_filename"] == "notes.txt"

        unsupported = client.post(
            "/api/v1/stores/import?ingest=false",
            data={"store_root": str(store_root)},
            files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
        )
        assert unsupported.status_code == 415
        assert unsupported.json()["error"]["code"] == "UNSUPPORTED_FORMAT"

        with patch("api.routes.MAX_IMPORT_BYTES", 4):
            too_large = client.post(
                "/api/v1/stores/import?ingest=false",
                data={"store_root": str(store_root)},
                files={"file": ("notes.txt", b"12345", "text/plain")},
            )
        assert too_large.status_code == 413
        assert too_large.json()["error"]["code"] == "FILE_TOO_LARGE"

        missing_store = client.post(
            "/api/v1/stores/import?ingest=false",
            files={"file": ("notes.txt", b"text", "text/plain")},
        )
        assert missing_store.status_code == 422
        assert missing_store.json()["error"]["code"] == "INVALID_PARAM"

    operation = app.openapi()["paths"]["/api/v1/stores/import"]["post"]
    parameter_names = {item["name"] for item in operation.get("parameters", [])}
    assert "store_root" not in parameter_names
    assert operation["requestBody"]["content"].get("multipart/form-data") is not None


def test_cli_store_root_never_falls_back_to_default_data(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    config_path = _config_file(tmp_path, settings)
    store_root = tmp_path / "cli-store"

    output = io.StringIO()
    with redirect_stdout(output):
        code = start.main(
            [
                "--config",
                str(config_path),
                "store-init",
                "--root",
                str(store_root),
                "--scope",
                "memory.important",
            ]
        )
    assert code == 0
    initialized = json.loads(output.getvalue())
    assert initialized["data"]["storage_root"] == str(
        store_root / STORAGE_DIRECTORY_NAME
    )

    output = io.StringIO()
    with redirect_stdout(output):
        code = start.main(
            [
                "--config",
                str(config_path),
                "--store-root",
                str(store_root),
                "status",
            ]
        )
    assert code == 0
    assert json.loads(output.getvalue())["data"]["initialized"] is True
    assert not (tmp_path / "default-data").exists()

    output = io.StringIO()
    with redirect_stdout(output):
        code = start.main(
            [
                "--config",
                str(config_path),
                "--store-root",
                str(store_root),
                "import",
                "relative.md",
                "--no-ingest",
            ]
        )
    assert code == 2
    assert json.loads(output.getvalue())["error"]["code"] == "STORE_ACCESS_DENIED"


def test_manifest_root_is_bound_to_its_physical_location(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = tmp_path / "bound"
    initialize_store(root, scope="memory.temporary", settings=settings)
    manifest = load_store_manifest(root, settings=settings)
    assert manifest.root_path == str(root.resolve())
