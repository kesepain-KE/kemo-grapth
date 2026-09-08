"""原始文档与 Markdown 文件映射存储。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import AppConfig, load_config
from ._utils import _path_key, _validated_path

FILE_MAP_VERSION = 1


class FileMapError(RuntimeError):
    """file_map.json 格式无效时抛出的错误。"""


@dataclass(frozen=True)
class FileMapping:
    """一条原始路径与 Markdown 相对路径的一对一映射。"""

    original_path: str
    markdown_path: str


class FileMapStore:
    """external/markdown/file_map.json 的 CRUD 存储。"""

    def __init__(self, file_path: Path | str):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            not self.file_path.exists()
            or not self.file_path.read_text(encoding="utf-8").strip()
        ):
            self._write([])

    @classmethod
    def from_config(cls, settings: AppConfig | None = None) -> "FileMapStore":
        active_settings = settings or load_config()
        return cls(active_settings.resolve_external_dir() / "file_map.json")

    def list(self) -> list[FileMapping]:
        """返回所有映射，保持文件中的顺序。"""

        payload = self._load_payload()
        mappings: list[FileMapping] = []
        for index, item in enumerate(payload["mappings"]):
            if not isinstance(item, dict):
                raise FileMapError(f"第 {index} 条映射必须是对象")
            original_path = item.get("original_path")
            markdown_path = item.get("markdown_path")
            if not isinstance(original_path, str) or not original_path.strip():
                raise FileMapError(f"第 {index} 条映射缺少有效 original_path")
            if not isinstance(markdown_path, str) or not markdown_path.strip():
                raise FileMapError(f"第 {index} 条映射缺少有效 markdown_path")
            mappings.append(
                FileMapping(
                    original_path=original_path,
                    markdown_path=markdown_path,
                )
            )
        return mappings

    def get_by_original(self, original_path: Path | str) -> FileMapping | None:
        lookup_key = _path_key(original_path)
        return next(
            (
                mapping
                for mapping in self.list()
                if _path_key(mapping.original_path) == lookup_key
            ),
            None,
        )

    def get_by_markdown(self, markdown_path: Path | str) -> FileMapping | None:
        lookup_key = _path_key(markdown_path)
        return next(
            (
                mapping
                for mapping in self.list()
                if _path_key(mapping.markdown_path) == lookup_key
            ),
            None,
        )

    def upsert(
        self, original_path: Path | str, markdown_path: Path | str
    ) -> FileMapping:
        """创建或更新映射，并保证两边路径都是一对一。"""

        mapping = FileMapping(
            original_path=_validated_path(original_path, "original_path"),
            markdown_path=_validated_path(markdown_path, "markdown_path"),
        )
        original_key = _path_key(mapping.original_path)
        markdown_key = _path_key(mapping.markdown_path)
        retained = [
            current
            for current in self.list()
            if _path_key(current.original_path) != original_key
            and _path_key(current.markdown_path) != markdown_key
        ]
        retained.append(mapping)
        self._write(retained)
        return mapping

    def delete_by_original(self, original_path: Path | str) -> bool:
        lookup_key = _path_key(original_path)
        current = self.list()
        retained = [
            mapping
            for mapping in current
            if _path_key(mapping.original_path) != lookup_key
        ]
        if len(retained) == len(current):
            return False
        self._write(retained)
        return True

    def delete_by_markdown(self, markdown_path: Path | str) -> bool:
        lookup_key = _path_key(markdown_path)
        current = self.list()
        retained = [
            mapping
            for mapping in current
            if _path_key(mapping.markdown_path) != lookup_key
        ]
        if len(retained) == len(current):
            return False
        self._write(retained)
        return True

    def _load_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FileMapError(f"映射表不是合法 JSON：{self.file_path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise FileMapError("映射表根节点必须是对象")
        if payload.get("version") != FILE_MAP_VERSION:
            raise FileMapError(f"不支持的映射表版本：{payload.get('version')!r}")
        if not isinstance(payload.get("mappings"), list):
            raise FileMapError("mappings 必须是数组")
        return payload

    def _write(self, mappings: list[FileMapping]) -> None:
        payload = {
            "version": FILE_MAP_VERSION,
            "mappings": [asdict(mapping) for mapping in mappings],
        }
        temporary_path = self.file_path.with_name(
            f".{self.file_path.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, self.file_path)
        finally:
            temporary_path.unlink(missing_ok=True)


__all__ = ["FILE_MAP_VERSION", "FileMapError", "FileMapping", "FileMapStore"]
