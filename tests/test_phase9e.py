"""Phase 9E 全格式导入、入口集成与日志验收测试。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import start
from api import create_app
from api.deps import get_service
from core.config import AppConfig, load_config
from core.ingestor import IngestError
from core.knowledge_base import (
    DocumentImportConflictError,
    DocumentImportError,
    DocumentImportPathError,
    KnowledgeBaseService,
    UnsupportedDocumentFormatError,
)
from core.logger import DailyTSVLogger
from provider.tools.document_tools import DocumentConversionError


def _settings(root: Path) -> AppConfig:
    return AppConfig(
        chunk_size=128,
        chunk_overlap=16,
        log_dir=str(root / "log"),
        entity_extraction={"method": "rule", "max_entities": 10},
        models={
            "embedding": "siliconflow-Qwen-Qwen3-VL-Embedding-8B",
            "embedding_dimensions": 3,
            "rerank": "siliconflow-Qwen-Qwen3-VL-Reranker-8B",
        },
    )


def _service(root: Path) -> KnowledgeBaseService:
    return KnowledgeBaseService(
        settings=_settings(root),
        data_dir=root / "data",
        external_dir=root / "external" / "markdown",
        config_path=root / "config.json",
    )


class ConfigurationTests(unittest.TestCase):
    def test_public_siliconflow_model_ids_do_not_contain_slashes(self) -> None:
        settings = load_config(Path(__file__).resolve().parents[1] / "config" / "config.json")
        self.assertEqual(
            settings.models.embedding,
            "siliconflow-Qwen-Qwen3-VL-Embedding-8B",
        )
        self.assertEqual(
            settings.models.rerank,
            "siliconflow-Qwen-Qwen3-VL-Reranker-8B",
        )
        self.assertNotIn("/", settings.models.embedding)
        self.assertNotIn("/", settings.models.rerank)
        self.assertNotEqual(settings.models.llm, settings.models.embedding)


class DocumentImportTests(unittest.TestCase):
    def test_txt_markdown_and_csv_import_and_file_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source_dir = root / "sources"
            source_dir.mkdir()
            files = {
                "notes.txt": "line one\nline two",
                "guide.markdown": "# Guide\n\nBody",
                "table.csv": "name,value\nalpha,1\n",
                "reference.rst": "RST Guide\n=========\n\nConverted body.",
            }
            service = _service(root)
            results = []
            for name, content in files.items():
                source = source_dir / name
                source.write_text(content, encoding="utf-8")
                results.append(
                    service.import_document(source, ingest_after_import=False)
                )

            markdown_dir = root / "external" / "markdown"
            self.assertEqual({item["ingest_status"] for item in results}, {"pending"})
            self.assertTrue(all(item["source_id"] for item in results))
            self.assertEqual(
                (markdown_dir / results[0]["markdown_relative_path"]).read_text(encoding="utf-8"),
                "line one\nline two\n",
            )
            self.assertEqual(
                (markdown_dir / results[1]["markdown_relative_path"]).read_text(encoding="utf-8"),
                files["guide.markdown"] + "\n",
            )
            self.assertIn("| name | value |", (markdown_dir / results[2]["markdown_relative_path"]).read_text(encoding="utf-8"))
            self.assertIn(
                "# RST Guide",
                (markdown_dir / results[3]["markdown_relative_path"]).read_text(encoding="utf-8"),
            )
            mapping = json.loads((markdown_dir / "file_map.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mapping["mappings"]), 4)

            repeated = service.import_document(
                source_dir / "notes.txt",
                ingest_after_import=False,
            )
            self.assertEqual(
                repeated["markdown_relative_path"],
                results[0]["markdown_relative_path"],
            )
            self.assertEqual(repeated["source_id"], results[0]["source_id"])
            mapping = json.loads((markdown_dir / "file_map.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mapping["mappings"]), 4)

    def test_import_converts_the_same_private_snapshot_that_was_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "中文 文档.txt"
            original = b"captured content"
            source.write_bytes(original)
            expected_hash = hashlib.sha256(original).hexdigest()
            service = _service(root)
            captured_snapshot: Path | None = None

            def replace_source_during_conversion(
                snapshot_path,
                external_dir,
                *,
                destination_name,
            ):
                nonlocal captured_snapshot
                captured_snapshot = Path(snapshot_path)
                source.write_text("replacement content", encoding="utf-8")
                destination = Path(external_dir) / destination_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(captured_snapshot.read_bytes())
                return {"format": "txt"}

            with patch(
                "core.knowledge_base.convert_document",
                side_effect=replace_source_during_conversion,
            ):
                imported = service.import_document(
                    source,
                    ingest_after_import=False,
                    expected_origin_hash=expected_hash,
                )

            self.assertEqual(imported["origin_hash"], expected_hash)
            destination = root / "external" / "markdown" / imported["markdown_relative_path"]
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(source.read_text(encoding="utf-8"), "replacement content")
            self.assertIsNotNone(captured_snapshot)
            assert captured_snapshot is not None
            self.assertFalse(captured_snapshot.exists())
            self.assertFalse(captured_snapshot.parent.exists())
            mapping = json.loads(
                (root / "external" / "markdown" / "file_map.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(mapping["mappings"][0]["original_path"], str(source.resolve()))

    def test_subjectless_eml_keeps_original_filename_as_fallback_title(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "quarterly-report.eml"
            source.write_bytes(
                b"From: sender@example.com\r\n"
                b"To: receiver@example.com\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"\r\nQuarterly report body.\r\n"
            )
            service = _service(root)

            imported = service.import_document(
                source,
                ingest_after_import=False,
            )

            markdown = (
                root
                / "external"
                / "markdown"
                / imported["markdown_relative_path"]
            ).read_text(encoding="utf-8")
            self.assertIn("# quarterly report", markdown)
            self.assertNotIn("# source", markdown)

    def test_expected_hash_conflict_has_no_document_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "document.txt"
            source.write_text("current content", encoding="utf-8")
            service = _service(root)

            with patch("core.knowledge_base.convert_document") as convert_mock:
                with self.assertRaises(DocumentImportConflictError):
                    service.import_document(
                        source,
                        ingest_after_import=False,
                        expected_origin_hash="0" * 64,
                    )

            convert_mock.assert_not_called()
            markdown_dir = root / "external" / "markdown"
            self.assertEqual(list(markdown_dir.glob("*.md")), [])
            mapping = json.loads(
                (markdown_dir / "file_map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(mapping["mappings"], [])

    def test_source_change_during_capture_is_rejected_before_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "document.txt"
            source.write_text("original", encoding="utf-8")
            service = _service(root)
            real_fstat = os.fstat
            fstat_calls = 0

            def change_source_before_second_fstat(descriptor):
                nonlocal fstat_calls
                fstat_calls += 1
                if fstat_calls == 2:
                    source.write_text("changed while capturing", encoding="utf-8")
                return real_fstat(descriptor)

            with (
                patch(
                    "core.knowledge_base.os.fstat",
                    side_effect=change_source_before_second_fstat,
                ),
                patch("core.knowledge_base.convert_document") as convert_mock,
            ):
                with self.assertRaises(DocumentImportConflictError):
                    service.import_document(source, ingest_after_import=False)

            convert_mock.assert_not_called()
            markdown_dir = root / "external" / "markdown"
            self.assertEqual(list(markdown_dir.glob("*.md")), [])

    def test_scan_failure_rolls_back_markdown_and_file_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "document.txt"
            source.write_text("content", encoding="utf-8")
            service = _service(root)

            with patch(
                "core.knowledge_base.Ingestor.scan_sources",
                side_effect=IngestError("scan failed"),
            ):
                with self.assertRaises(DocumentImportError):
                    service.import_document(source, ingest_after_import=False)

            markdown_dir = root / "external" / "markdown"
            self.assertEqual(list(markdown_dir.glob("*.md")), [])
            mapping = json.loads(
                (markdown_dir / "file_map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(mapping["mappings"], [])

    def test_parallel_first_imports_do_not_lose_file_map_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            sources = []
            for index in range(6):
                source = root / f"document-{index}.txt"
                source.write_text(f"content {index}", encoding="utf-8")
                sources.append(source)
            service = _service(root)

            with ThreadPoolExecutor(max_workers=len(sources)) as executor:
                results = list(
                    executor.map(
                        lambda path: service.import_document(
                            path,
                            ingest_after_import=False,
                        ),
                        sources,
                    )
                )

            self.assertEqual(len({item["source_id"] for item in results}), len(sources))
            mapping = json.loads(
                (
                    root / "external" / "markdown" / "file_map.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(mapping["mappings"]), len(sources))

    def test_parallel_imports_of_same_source_keep_one_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "shared.txt"
            source.write_text("shared content", encoding="utf-8")
            service = _service(root)

            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(
                    executor.map(
                        lambda _: service.import_document(
                            source,
                            ingest_after_import=False,
                        ),
                        range(6),
                    )
                )

            self.assertEqual(len({item["source_id"] for item in results}), 1)
            self.assertEqual(
                len({item["markdown_relative_path"] for item in results}),
                1,
            )
            mapping = json.loads(
                (
                    root / "external" / "markdown" / "file_map.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(mapping["mappings"]), 1)

    def test_snapshot_tampering_rolls_back_markdown_and_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "document.txt"
            source.write_text("captured content", encoding="utf-8")
            service = _service(root)
            captured_snapshot: Path | None = None

            def tamper_with_snapshot(snapshot_path, external_dir, *, destination_name):
                nonlocal captured_snapshot
                captured_snapshot = Path(snapshot_path)
                destination = Path(external_dir) / destination_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text("converted content", encoding="utf-8")
                captured_snapshot.write_text("tampered", encoding="utf-8")
                return {"format": "txt"}

            with patch(
                "core.knowledge_base.convert_document",
                side_effect=tamper_with_snapshot,
            ):
                with self.assertRaises(DocumentImportConflictError):
                    service.import_document(source, ingest_after_import=False)

            markdown_dir = root / "external" / "markdown"
            self.assertEqual(list(markdown_dir.glob("*.md")), [])
            mapping = json.loads(
                (markdown_dir / "file_map.json").read_text(encoding="utf-8")
            )
            self.assertEqual(mapping["mappings"], [])
            self.assertIsNotNone(captured_snapshot)
            assert captured_snapshot is not None
            self.assertFalse(captured_snapshot.exists())
            self.assertFalse(captured_snapshot.parent.exists())
            self.assertEqual(
                list(markdown_dir.glob(".*.tmp"))
                + list(markdown_dir.glob(".*.restore")),
                [],
            )

    def test_invalid_inputs_and_conversion_failure_leave_no_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source_dir = root / "sources"
            source_dir.mkdir()
            unsupported = source_dir / "payload.exe"
            unsupported.write_bytes(b"MZ")
            service = _service(root)

            with self.assertRaises(UnsupportedDocumentFormatError):
                service.import_document(unsupported, ingest_after_import=False)
            traversal = str(source_dir / "nested" / ".." / "document.txt")
            with self.assertRaises(DocumentImportPathError):
                service.import_document(traversal, ingest_after_import=False)

            source = source_dir / "broken.txt"
            source.write_text("broken", encoding="utf-8")
            with patch(
                "core.knowledge_base.convert_document",
                side_effect=DocumentConversionError("conversion failed"),
            ):
                with self.assertRaises(DocumentConversionError):
                    service.import_document(source, ingest_after_import=False)
            markdown_dir = root / "external" / "markdown"
            self.assertEqual(list(markdown_dir.glob("*.md")), [])

    def test_no_ingest_skips_models_and_default_import_runs_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "document.md"
            source.write_text("# Document", encoding="utf-8")
            service = _service(root)
            ingest_result = {
                "processed": 1,
                "graph_updated": 1,
                "rag_updated": 1,
                "skipped": 0,
                "failed": 0,
                "details": [],
            }

            with patch("core.knowledge_base.Ingestor.ingest") as ingest_mock:
                pending = service.import_document(source, ingest_after_import=False)
                ingest_mock.assert_not_called()
            self.assertEqual(pending["ingest_status"], "pending")

            source.write_text("# Document updated", encoding="utf-8")
            with patch(
                "core.knowledge_base.Ingestor.ingest",
                return_value=ingest_result,
            ) as ingest_mock:
                completed = service.import_document(source)
                ingest_mock.assert_called_once()
            self.assertEqual(completed["ingest_status"], "completed")
            self.assertEqual(completed["ingest"], ingest_result)

    def test_import_graph_tool_and_delete_actions_are_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "document.md"
            source.write_text("Alpha", encoding="utf-8")
            service = _service(root)
            imported = service.import_document(source, ingest_after_import=False)

            def finish_graph(system, user, tools, tool_handler, **kwargs):
                del system, user, tools, kwargs
                tool_handler("finish", {})
                return "done"

            with patch("core.ingestor.chat_with_tools", side_effect=finish_graph):
                service.ingest(paths=[imported["markdown_relative_path"]], mode="graph")
            service.delete_document(imported["source_id"])

            log_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (root / "log").glob("*.tsv")
            )
            self.assertIn("document_import_start", log_text)
            self.assertIn("graph_tool_call", log_text)
            self.assertIn("delete_document", log_text)


class _ImportAPIService:
    def import_document(self, source_path, **kwargs):
        source = Path(source_path)
        if source.name == "broken.txt":
            raise DocumentConversionError("无法转换测试文件")
        if source.name == "ingest-fail.txt":
            return {
                "source_id": "source-2",
                "original_filename": source.name,
                "detected_format": "txt",
                "markdown_relative_path": "ingest-fail-stable.md",
                "conversion_status": "completed",
                "ingest_status": "failed",
                "ingest_error": "模拟整理失败",
                "size": source.stat().st_size,
            }
        return {
            "source_id": "source-1",
            "original_filename": source.name,
            "detected_format": source.suffix.removeprefix("."),
            "markdown_relative_path": "document-stable.md",
            "conversion_status": "completed",
            "ingest_status": "completed" if kwargs["ingest_after_import"] else "pending",
            "size": source.stat().st_size,
        }


class EntryPointTests(unittest.TestCase):
    def test_api_import_success_and_format_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            config_path = root / "config.json"
            config_path.write_text(
                _settings(root).model_dump_json(indent=2),
                encoding="utf-8",
            )
            app = create_app(
                config_path=config_path,
                data_dir=root / "data",
                external_dir=root / "markdown",
            )
            app.dependency_overrides[get_service] = lambda: _ImportAPIService()
            client = TestClient(app)

            success = client.post(
                "/api/v1/import?ingest=false",
                files={"file": ("notes.txt", b"hello", "text/plain")},
            )
            self.assertEqual(success.status_code, 200)
            self.assertEqual(success.json()["data"]["ingest_status"], "pending")

            unsupported = client.post(
                "/api/v1/import",
                files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
            )
            self.assertEqual(unsupported.status_code, 415)
            self.assertEqual(unsupported.json()["error"]["code"], "UNSUPPORTED_FORMAT")

            conversion = client.post(
                "/api/v1/import",
                files={"file": ("broken.txt", b"broken", "text/plain")},
            )
            self.assertEqual(conversion.status_code, 422)
            self.assertEqual(conversion.json()["error"]["code"], "CONVERSION_FAILED")

            ingest_failure = client.post(
                "/api/v1/import",
                files={"file": ("ingest-fail.txt", b"text", "text/plain")},
            )
            self.assertEqual(ingest_failure.status_code, 502)
            self.assertEqual(ingest_failure.json()["error"]["code"], "INGEST_FAILED")

    def test_cli_import_outputs_structured_json(self) -> None:
        class FakeService:
            def import_document(self, path, *, ingest_after_import=True):
                return {
                    "source_id": "source-1",
                    "original_filename": Path(path).name,
                    "detected_format": "txt",
                    "markdown_relative_path": "notes-stable.md",
                    "conversion_status": "completed",
                    "ingest_status": "completed" if ingest_after_import else "pending",
                }

        output = io.StringIO()
        with (
            patch.object(start, "load_config", return_value=AppConfig()),
            patch.object(start, "KnowledgeBaseService", return_value=FakeService()),
            redirect_stdout(output),
        ):
            exit_code = start.main(["import", "notes.txt", "--no-ingest"])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["ingest_status"], "pending")


class LoggerTests(unittest.TestCase):
    def test_daily_tsv_has_header_and_redacts_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            secret = "phase9e-super-secret"
            logger = DailyTSVLogger(Path(temporary_dir), "INFO")
            path = logger.log(
                "test",
                "llm_request",
                f"Authorization: Bearer {secret}; api_key={secret}",
                12,
            )
            self.assertIsNotNone(path)
            content = path.read_text(encoding="utf-8")
            self.assertEqual(
                content.splitlines()[0],
                "time\tlevel\tmodule\taction\tdetail\telapsed_ms",
            )
            self.assertNotIn(secret, content)
            self.assertIn("[REDACTED]", content)


if __name__ == "__main__":
    unittest.main()
