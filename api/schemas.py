"""外部 API 和 Web 入口共用的 Pydantic 请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestRequest(StrictRequest):
    paths: list[str] | None = None
    mode: Literal["graph", "rag", "both"] = "both"

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if any(not path.strip() for path in value):
            raise ValueError("paths 中不能包含空路径")
        return [path.strip() for path in value]


class GraphQueryRequest(StrictRequest):
    query: str
    depth: int = Field(default=3, ge=1, le=10)
    direction: Literal["forward", "backward", "both"] = "both"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空")
        return value.strip()


class RAGQueryRequest(StrictRequest):
    query: str
    top_k: int | None = Field(default=None, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空")
        return value.strip()


class HybridQueryRequest(StrictRequest):
    query: str
    graph_depth: int = Field(default=3, ge=1, le=10)
    rag_top_k: int | None = Field(default=None, ge=1, le=100)
    graph_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rag_threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空")
        return value.strip()


class AnswerQueryRequest(HybridQueryRequest):
    """使用混合检索上下文生成 LLM 回答。"""


class GlobalQueryRequest(StrictRequest):
    query: str
    top_k: int = Field(default=5, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query 不能为空")
        return value.strip()


class CachedQueryItem(BaseModel):
    cache_key: str
    query_mode: Literal["answer", "graph", "rag", "hybrid", "global"] | str
    query: str
    normalized_query: str
    params: dict[str, Any]
    params_json: str
    result_size: int = Field(ge=0)
    state_hash: str
    is_stale: bool
    hit_count: int = Field(ge=0)
    created_at: str
    updated_at: str
    last_hit_at: str | None = None


class CachedQueryListResponse(BaseModel):
    items: list[CachedQueryItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


class ErrorDetail(BaseModel):
    code: str
    message: str


class UploadRequest(StrictRequest):
    content: str
    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("filename 不能为空")
        if any(c in value for c in ("/", "\\", "..")):
            raise ValueError("filename 不能包含路径分隔符")
        return value.strip()


class DocumentContentUpdateRequest(StrictRequest):
    content: str
    expected_content_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("expected_content_hash")
    @classmethod
    def validate_expected_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("expected_content_hash 必须是 SHA-256 十六进制字符串")
        return normalized


class DocumentBatchDeleteRequest(StrictRequest):
    source_ids: list[str] = Field(min_length=1, max_length=1000)

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        normalized = [source_id.strip() for source_id in value]
        if any(not source_id for source_id in normalized):
            raise ValueError("source_ids 中不能包含空值")
        return list(dict.fromkeys(normalized))


class ImportResponseData(BaseModel):
    source_id: str
    original_filename: str
    detected_format: str
    markdown_relative_path: str
    conversion_status: Literal["completed"]
    ingest_status: Literal["pending", "completed", "failed"]
    size: int = Field(ge=0)
    origin_hash: str | None = None
    content_hash: str | None = None
    origin_modified_at: str | None = None
    ingest: dict[str, Any] | None = None
    ingest_error: str | None = None


class ConfigSaveRequest(StrictRequest):
    """接受完整配置 JSON 的请求体，字段由 AppConfig 模型校验。"""
    model_config = ConfigDict(extra="allow")


class OrganizeGraphRequest(StrictRequest):
    use_llm: bool = True
    summarize: bool = True


class RestartRequest(StrictRequest):
    confirm: Literal["restart"]


class UpdateApplyRequest(StrictRequest):
    """应用更新请求；``force`` 允许同版本重新安装当前远端提交。"""

    force: bool = False


StoreScope = Literal[
    "knowledge.global",
    "knowledge.shared",
    "knowledge.user",
    "memory.temporary",
    "memory.important",
    "memory.permanent",
    "memory.user",
]


class StoreRootRequest(StrictRequest):
    """所有分布式 Store 操作的显式绝对位置。"""

    store_root: str = Field(min_length=1)

    @field_validator("store_root")
    @classmethod
    def validate_store_root(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("store_root 不能为空")
        return normalized


class StoreInitializeRequest(StoreRootRequest):
    scope: StoreScope
    owner_id: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)


class StoreImportPathRequest(StoreRootRequest):
    path: str = Field(min_length=1)
    ingest_after_import: bool = True
    expected_origin_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("path 不能为空")
        return normalized

    @field_validator("expected_origin_hash")
    @classmethod
    def validate_expected_origin_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("expected_origin_hash 必须是 SHA-256 十六进制字符串")
        return normalized


class StoreUploadRequest(StoreRootRequest):
    content: str
    filename: str = Field(min_length=1, max_length=255)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(part in normalized for part in ("/", "\\", "..")):
            raise ValueError("filename 必须是不含路径的文件名")
        return normalized


class ExternalSourceRecord(StrictRequest):
    """kemo-agent 等上游权威存储推送的一条稳定来源。"""

    source_uri: str = Field(min_length=1, max_length=2048)
    source_type: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)
    content: str = ""
    content_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    revision: str = Field(min_length=1, max_length=255)
    updated_at: str = Field(min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    deleted: bool = False
    ingest_mode: Literal["graph", "rag", "both"] = "both"


class StoreSourceSyncRequest(StoreRootRequest):
    records: list[ExternalSourceRecord] = Field(min_length=1, max_length=1000)
    ingest_after_sync: bool = False


class StoreSourceStatusRequest(StoreRootRequest):
    source_type: str | None = Field(default=None, min_length=1, max_length=128)
    include_deleted: bool = False
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=1000)


class StoreSourceDeleteRequest(StoreRootRequest):
    source_uris: list[str] = Field(min_length=1, max_length=1000)

    @field_validator("source_uris")
    @classmethod
    def validate_source_uris(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("source_uris 中不能包含空值")
        return list(dict.fromkeys(normalized))


class StoreIngestRequest(StoreRootRequest):
    paths: list[str] | None = None
    mode: Literal["graph", "rag", "both"] = "both"

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("paths 中不能包含空路径")
        return normalized


class StoreGraphQueryRequest(StoreRootRequest):
    query: str
    depth: int = Field(default=3, ge=1, le=10)
    direction: Literal["forward", "backward", "both"] = "both"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    force: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized


class StoreRAGQueryRequest(StoreRootRequest):
    query: str
    top_k: int | None = Field(default=None, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    force: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized


class StoreHybridQueryRequest(StoreRootRequest):
    query: str
    graph_depth: int = Field(default=3, ge=1, le=10)
    rag_top_k: int | None = Field(default=None, ge=1, le=100)
    graph_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rag_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    direction: Literal["forward", "backward", "both"] = "both"
    force: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized


class StoreAnswerQueryRequest(StoreHybridQueryRequest):
    pass


class StoreGlobalQueryRequest(StoreRootRequest):
    query: str
    top_k: int = Field(default=5, ge=1, le=100)
    force: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized


class StoreFederatedQueryRequest(StrictRequest):
    store_roots: list[str] = Field(min_length=1, max_length=100)
    query: str
    mode: Literal["graph", "rag", "hybrid", "global", "answer"] = "hybrid"
    force: bool = False
    top_k: int | None = Field(default=None, ge=1, le=1000)
    graph_depth: int = Field(default=3, ge=1, le=10)

    @field_validator("store_roots")
    @classmethod
    def validate_store_roots(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("store_roots 中不能包含空路径")
        return normalized

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized


class StoreDocumentListRequest(StoreRootRequest):
    status: Literal["active", "pending", "all"] | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class StoreSourceRequest(StoreRootRequest):
    source_id: str = Field(min_length=1)


class StoreDocumentContentUpdateRequest(StoreSourceRequest):
    content: str
    expected_content_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("expected_content_hash")
    @classmethod
    def validate_expected_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().casefold()
        if any(character not in "0123456789abcdef" for character in normalized):
            raise ValueError("expected_content_hash 必须是 SHA-256 十六进制字符串")
        return normalized


class StoreDocumentBatchDeleteRequest(StoreRootRequest):
    source_ids: list[str] = Field(min_length=1, max_length=1000)

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        normalized = [source_id.strip() for source_id in value]
        if any(not source_id for source_id in normalized):
            raise ValueError("source_ids 中不能包含空值")
        return list(dict.fromkeys(normalized))


class StoreDeleteAllDocumentsRequest(StoreRootRequest):
    confirm: Literal["delete-all"]


class StoreNodeRequest(StoreRootRequest):
    node_id: str = Field(min_length=1)


class StoreEdgeRequest(StoreRootRequest):
    edge_id: str = Field(min_length=1)


class StoreFullGraphRequest(StoreRootRequest):
    nodes_page: int | None = Field(default=None, ge=1)
    nodes_page_size: int = Field(default=100, ge=1, le=1000)


class StoreVisualizationPageRequest(StoreRootRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=1000, ge=1, le=10000)
    expected_revision: str | None = Field(default=None, min_length=64, max_length=64)


class StoreNeighborhoodRequest(StoreRootRequest):
    node_id: str = Field(min_length=1)
    depth: int = Field(default=2, ge=1, le=10)
    direction: Literal["forward", "backward", "both"] = "both"
    limit: int = Field(default=2000, ge=1, le=10000)
    edge_limit: int = Field(default=10000, ge=1, le=50000)
    expected_revision: str | None = Field(default=None, min_length=64, max_length=64)


class StoreCacheListRequest(StoreRootRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class StoreCacheKeyRequest(StoreRootRequest):
    cache_key: str = Field(min_length=1)


class StoreCacheClearRequest(StoreRootRequest):
    stale_only: bool = False


class StoreOrganizeGraphRequest(StoreRootRequest):
    use_llm: bool = True
    summarize: bool = True


class StoreCleanupRecycleRequest(StoreRootRequest):
    force: bool = False


class StoreJobsListRequest(StoreRootRequest):
    limit: int = Field(default=100, ge=1, le=1000)


class StoreJobRequest(StoreRootRequest):
    job_id: str = Field(min_length=1)


class APIResponse(BaseModel):
    ok: bool
    data: Any | None = None
    error: ErrorDetail | None = None
