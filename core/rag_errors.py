"""Stable public errors for RAG and FAISS operations."""

class RAGError(RuntimeError):
    """RAG 引擎错误基类。"""


class FaissUnavailableError(RAGError):
    """FAISS 依赖未安装。"""


class IndexIntegrityError(RAGError):
    """FAISS 或 SQLite 中的向量数据不一致。"""


class RAGQueryError(RAGError):
    """RAG 查询参数或重排序结果无效。"""


