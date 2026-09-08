"""API 统一响应封装与异常映射。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.graph_engine import GraphQueryError
from core.graph_visualization import (
    GraphNodeNotFoundError,
    GraphRevisionChangedError,
)
from core.ingestor import (
    DocumentNotFoundError,
    IngestError,
    RecycleConflictError,
)
from core.knowledge_base import (
    DocumentImportError,
    DocumentContentConflictError,
    DocumentImportConflictError,
    DocumentImportPathError,
    DocumentIngestError,
    DocumentTooLargeError,
    KnowledgeBaseNotInitializedError,
    KnowledgeBaseProcessingError,
    UnsupportedDocumentFormatError,
)
from core.logger import DailyTSVLogger
from core.jobs import JobNotFoundError
from core.rag_engine import RAGQueryError
from core.portable_store import (
    PortableStoreAccessError,
    PortableStoreError,
    PortableStoreNotInitializedError,
)
from provider.tools.document_tools import DocumentConversionError
from update import (
    UpdateBlockedError,
    UpdateError,
    UpdatePermissionError,
    UpdateSourceError,
)
from restart import RestartError, RestartPermissionError, RestartUnavailableError


LOGGER = logging.getLogger(__name__)


def success_response(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message},
    }


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(HTTPException, _http_error_handler)
    for exception_type in (
        KnowledgeBaseNotInitializedError,
        KnowledgeBaseProcessingError,
        DocumentNotFoundError,
        GraphNodeNotFoundError,
        GraphRevisionChangedError,
        JobNotFoundError,
        RecycleConflictError,
        GraphQueryError,
        RAGQueryError,
        IngestError,
        PortableStoreError,
        DocumentImportError,
        DocumentConversionError,
        ValueError,
        TypeError,
        UpdateError,
        RestartError,
    ):
        app.add_exception_handler(exception_type, _application_error_handler)
    app.add_exception_handler(Exception, _application_error_handler)


async def _validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    messages = [
        f"{'.'.join(str(item) for item in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    ]
    _log_api_error(request, 422, "INVALID_PARAM", exc)
    return JSONResponse(
        status_code=422,
        content=error_payload("INVALID_PARAM", "; ".join(messages)),
    )


async def _http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "INVALID_PARAM"
    _log_api_error(request, exc.status_code, code, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(code, str(exc.detail)),
    )


async def _application_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, RestartPermissionError):
        _log_api_error(request, 403, "RESTART_FORBIDDEN", exc)
        return JSONResponse(
            status_code=403,
            content=error_payload("RESTART_FORBIDDEN", str(exc)),
        )
    if isinstance(exc, RestartUnavailableError):
        _log_api_error(request, 503, "RESTART_UNAVAILABLE", exc)
        return JSONResponse(
            status_code=503,
            content=error_payload("RESTART_UNAVAILABLE", str(exc)),
        )
    if isinstance(exc, RestartError):
        _log_api_error(request, 500, "RESTART_FAILED", exc)
        return JSONResponse(
            status_code=500,
            content=error_payload("RESTART_FAILED", str(exc)),
        )
    if isinstance(exc, UpdatePermissionError):
        _log_api_error(request, 403, "UPDATE_FORBIDDEN", exc)
        return JSONResponse(
            status_code=403,
            content=error_payload("UPDATE_FORBIDDEN", str(exc)),
        )
    if isinstance(exc, UpdateBlockedError):
        _log_api_error(request, 409, "UPDATE_BLOCKED", exc)
        return JSONResponse(
            status_code=409,
            content=error_payload("UPDATE_BLOCKED", str(exc)),
        )
    if isinstance(exc, UpdateSourceError):
        _log_api_error(request, 502, "UPDATE_SOURCE_FAILED", exc)
        return JSONResponse(
            status_code=502,
            content=error_payload("UPDATE_SOURCE_FAILED", str(exc)),
        )
    if isinstance(exc, UpdateError):
        _log_api_error(request, 500, "UPDATE_FAILED", exc)
        return JSONResponse(
            status_code=500,
            content=error_payload("UPDATE_FAILED", str(exc)),
        )
    if isinstance(exc, DocumentContentConflictError):
        _log_api_error(request, 409, "CONTENT_CONFLICT", exc)
        return JSONResponse(
            status_code=409,
            content=error_payload("CONTENT_CONFLICT", str(exc)),
        )
    if isinstance(exc, DocumentImportConflictError):
        _log_api_error(request, 409, "IMPORT_SOURCE_CHANGED", exc)
        return JSONResponse(
            status_code=409,
            content=error_payload("IMPORT_SOURCE_CHANGED", str(exc)),
        )
    if isinstance(exc, PortableStoreAccessError):
        _log_api_error(request, 403, "STORE_ACCESS_DENIED", exc)
        return JSONResponse(
            status_code=403,
            content=error_payload("STORE_ACCESS_DENIED", str(exc)),
        )
    if isinstance(exc, PortableStoreNotInitializedError):
        _log_api_error(request, 404, "STORE_NOT_INITIALIZED", exc)
        return JSONResponse(
            status_code=404,
            content=error_payload("STORE_NOT_INITIALIZED", str(exc)),
        )
    if isinstance(exc, PortableStoreError):
        _log_api_error(request, 422, "STORE_INVALID", exc)
        return JSONResponse(
            status_code=422,
            content=error_payload("STORE_INVALID", str(exc)),
        )
    if isinstance(exc, KnowledgeBaseNotInitializedError):
        _log_api_error(request, 503, "NOT_INITIALIZED", exc)
        return JSONResponse(
            status_code=503,
            content=error_payload("NOT_INITIALIZED", str(exc)),
        )
    if isinstance(exc, KnowledgeBaseProcessingError):
        _log_api_error(request, 409, "PROCESSING", exc)
        return JSONResponse(
            status_code=409,
            content=error_payload("PROCESSING", str(exc)),
        )
    if isinstance(
        exc,
        (DocumentNotFoundError, GraphNodeNotFoundError, JobNotFoundError),
    ):
        _log_api_error(request, 404, "NOT_FOUND", exc)
        return JSONResponse(
            status_code=404,
            content=error_payload("NOT_FOUND", str(exc)),
        )
    if isinstance(exc, GraphRevisionChangedError):
        _log_api_error(request, 409, "GRAPH_CHANGED", exc)
        return JSONResponse(
            status_code=409,
            content=error_payload("GRAPH_CHANGED", str(exc)),
        )
    if isinstance(exc, RecycleConflictError):
        _log_api_error(request, 409, "INVALID_PARAM", exc)
        return JSONResponse(
            status_code=409,
            content=error_payload("INVALID_PARAM", str(exc)),
        )
    if isinstance(exc, UnsupportedDocumentFormatError):
        _log_api_error(request, 415, "UNSUPPORTED_FORMAT", exc)
        return JSONResponse(
            status_code=415,
            content=error_payload("UNSUPPORTED_FORMAT", str(exc)),
        )
    if isinstance(exc, DocumentTooLargeError):
        _log_api_error(request, 413, "FILE_TOO_LARGE", exc)
        return JSONResponse(
            status_code=413,
            content=error_payload("FILE_TOO_LARGE", str(exc)),
        )
    if isinstance(exc, DocumentImportPathError):
        _log_api_error(request, 400, "INVALID_PATH", exc)
        return JSONResponse(
            status_code=400,
            content=error_payload("INVALID_PATH", str(exc)),
        )
    if isinstance(exc, DocumentConversionError):
        _log_api_error(request, 422, "CONVERSION_FAILED", exc)
        return JSONResponse(
            status_code=422,
            content=error_payload("CONVERSION_FAILED", str(exc)),
        )
    if isinstance(exc, DocumentIngestError):
        _log_api_error(request, 502, "INGEST_FAILED", exc)
        return JSONResponse(
            status_code=502,
            content=error_payload("INGEST_FAILED", str(exc)),
        )
    if isinstance(
        exc, (GraphQueryError, RAGQueryError, IngestError, ValueError, TypeError)
    ):
        _log_api_error(request, 422, "INVALID_PARAM", exc)
        return JSONResponse(
            status_code=422,
            content=error_payload("INVALID_PARAM", str(exc)),
        )

    if isinstance(exc, DocumentImportError):
        _log_api_error(request, 422, "IMPORT_FAILED", exc)
        return JSONResponse(
            status_code=422,
            content=error_payload("IMPORT_FAILED", str(exc)),
        )

    LOGGER.exception("未处理的 API 异常", exc_info=exc)
    _log_api_error(request, 500, "INTERNAL", exc)
    return JSONResponse(
        status_code=500,
        content=error_payload("INTERNAL", "内部错误"),
    )


def _log_api_error(
    request: Request,
    status: int,
    code: str,
    exc: Exception,
) -> None:
    try:
        context = request.app.state.kemo_context
        DailyTSVLogger(
            context.settings.resolve_log_dir(),
            context.settings.log_level,
        ).log(
            "api",
            "api_error",
            (
                f"method={request.method}, path={request.url.path}, status={status}, "
                f"code={code}, error={type(exc).__name__}"
            ),
            level="ERROR" if status >= 500 else "WARNING",
        )
    except Exception:
        pass
