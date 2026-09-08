"""Public errors and immutable import metadata for the knowledge service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from provider.tools.document_tools import SUPPORTED_DOCUMENT_SUFFIXES

class KnowledgeBaseNotInitializedError(RuntimeError):
    """知识库的 sources.db 尚不存在。"""


class KnowledgeBaseProcessingError(RuntimeError):
    """所请求的数据当前正在整理。"""


class DocumentImportError(RuntimeError):
    """文档导入入口错误基类。"""


class UnsupportedDocumentFormatError(DocumentImportError):
    """用户上传了不受支持的文件格式。"""


class DocumentImportPathError(DocumentImportError):
    """导入路径不安全、缺失或不是普通文件。"""


class DocumentTooLargeError(DocumentImportError):
    """导入文件超过服务端大小上限。"""


class DocumentIngestError(DocumentImportError):
    """转换已完成，但后续知识库整理无法启动。"""


class DocumentContentConflictError(DocumentImportError):
    """文档在编辑期间已变化，拒绝覆盖较新的内容。"""


class DocumentImportConflictError(DocumentImportError):
    """源文件在调用方确认后发生变化，拒绝导入不同内容。"""


SUPPORTED_IMPORT_SUFFIXES = SUPPORTED_DOCUMENT_SUFFIXES
MAX_IMPORT_BYTES = 50 * 1024 * 1024
CONFIG_API_KEY_MASK = "************"


@dataclass(frozen=True)
class _ImportSnapshot:
    """一次导入使用的私有不可变文件快照及其来源元数据。"""

    path: Path
    size: int
    sha256: str
    modified_at: str
