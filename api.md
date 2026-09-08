# kemo-graph 外部智能体 API

> **用途**：本文件定义 kemo-graph 对外提供给 kemo-agent、其他智能体或自动化程序的 HTTP API。
> **不包括**：Web 前端页面、React 路由、浏览器交互约定。
> **当前版本**：`1.3.1`
> **实现来源**：`api/__init__.py`、`api/routes.py`、`api/schemas.py`。

---

## 1. 服务与调用边界

### 1.1 独立启动

外部 API 可以不启动 Web 前端，直接启动独立 FastAPI 应用：

```powershell
cd <kemo-graph-install-dir>
uvicorn api:app --host 127.0.0.1 --port 8000
```

默认 API 根路径：

```text
http://127.0.0.1:8000/api/v1
```

也可以使用 `python start_web.py` 启动；该方式会额外挂载网页前端，但 API 路径保持相同。

### 1.2 鉴权与部署安全

**当前 API 未实现应用层鉴权。** 因此默认只应监听本机回环地址：

```text
127.0.0.1
```

不要直接将该端口暴露到公网或不可信局域网。删除、导入、配置和维护端点均可修改本地知识库数据。

### 1.3 数据与操作原则

- 所有 JSON 请求的 `Content-Type` 为 `application/json`。
- 文档导入端点使用 `multipart/form-data`。
- 所有 JSON 请求模型拒绝未声明字段；不要传入额外参数。
- 图谱或 RAG 正在构建时，依赖该数据的一些读取/修改请求会返回 `409 PROCESSING`，调用方应等待后重试。
- 删除操作具有副作用，智能体执行前应先查询、确认目标 ID 与影响范围。
- 节点和关系删除均有公开 HTTP API；删除前应先调用详情端点核对来源和影响范围。

---

## 2. 通用响应格式

所有端点均返回以下包络：

```json
{
  "ok": true,
  "data": {},
  "error": null
}
```

失败时：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "INVALID_PARAM",
    "message": "具体错误说明"
  }
}
```

### 2.1 常见错误码

| HTTP | error.code | 含义 | 调用方建议 |
|---:|---|---|---|
| 400 / 422 | `INVALID_PARAM` | 参数、请求体或业务前置条件非法 | 修正参数后再试 |
| 404 | `NOT_FOUND` | source_id、node_id 或路由不存在 | 先刷新状态/图谱确认 ID |
| 409 | `PROCESSING` | 知识库正在整理 | 等待后重试，不并发修改 |
| 409 | `IMPORT_SOURCE_CHANGED` | 源文件与调用方确认的 SHA-256 不一致，或捕获期间发生变化 | 重新扫描文件后再发起导入 |
| 413 | `FILE_TOO_LARGE` | 导入文件超过 50 MB | 拆分或压缩内容 |
| 415 | `UNSUPPORTED_FORMAT` | 不支持的文档扩展名 | 先转换为支持格式 |
| 422 | `CONVERSION_FAILED` | 文档无法转换为 Markdown | 检查文件是否损坏或加密 |
| 422 | `IMPORT_FAILED` | 导入过程失败 | 查看 message 与服务日志 |
| 502 | `INGEST_FAILED` | 转换成功，但图谱/RAG 整理失败 | 文档已保留，可稍后调用 ingest 重试 |
| 503 | `NOT_INITIALIZED` | 知识库尚未初始化 | 先导入或扫描至少一篇文档 |
| 403 | `STORE_ACCESS_DENIED` | Store 路径不符合绝对路径或 allowlist 边界 | 修正路径或 portable_stores 配置 |
| 404 | `STORE_NOT_INITIALIZED` | 目标位置没有有效 manifest | 先调用 stores/initialize |
| 422 | `STORE_INVALID` | Store 清单、scope、owner 或目录状态非法 | 检查 kemo-graph-storage/manifest.json |
| 500 | `INTERNAL` | 未预期内部错误 | 查看 `log/YYYY-MM-DD.tsv` |

---

## 3. 读取状态与图谱

### 3.1 获取知识库状态

```http
GET /api/v1/status
```

用于智能体在执行导入、查询、删除前确认初始化状态、文档数量、Graph/RAG 状态及 FAISS 健康状态。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/status
```

返回 `data` 包含知识库概览；调用方应以实际响应字段为准，不应假设空库已存在 Graph 或 RAG 索引。

---

### 3.2 获取完整图谱

```http
GET /api/v1/graph
GET /api/v1/graph?nodes_page=1&nodes_page_size=100
```

不传分页参数时返回当前全部节点；传入 `nodes_page` 后仅对节点分页，关系线和节点群总结仍始终全量返回。适合智能体需要：

- 找到 `node_id` / `edge_id`；
- 审查可删除目标；
- 在本地做关系分析；
- 查看节点群总结。

成功响应示例：

```json
{
  "ok": true,
  "data": {
    "nodes": [
      {
        "node_id": "node-uuid",
        "keyword": "知识图谱",
        "summary": "……",
        "aliases": ["KG"],
        "tags": ["AI"],
        "ref_count": 2,
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00"
      }
    ],
    "edges": [
      {
        "edge_id": "edge-uuid",
        "source_node_id": "node-a",
        "relation": "包含",
        "target_node_id": "node-b",
        "weight": 0.9,
        "support_count": 1,
        "created_at": "2026-08-01T00:00:00+00:00"
      }
    ],
    "groups": [
      {
        "group_id": "group-uuid",
        "summary": "……",
        "node_count": 3,
        "edge_count": 2,
        "node_ids": ["node-a", "node-b", "node-c"]
      }
    ],
    "nodes_pagination": {
      "page": null,
      "page_size": 1,
      "total": 1,
      "total_pages": 1
    }
  },
  "error": null
}
```

兼容别名：`GET /api/v1/graph/full`。

### 3.3 GPU 可视化分页

Phase 14A 新增可视化专用数据契约，节点和关系分别分页，避免节点翻页时重复传输全部关系：

```http
GET /api/v1/graph/visualization/meta
GET /api/v1/graph/visualization/nodes?page=1&page_size=1000&expected_revision={revision}
GET /api/v1/graph/visualization/edges?page=1&page_size=2000&expected_revision={revision}
```

`meta` 返回当前内容驱动的 64 位 `revision`、节点/关系/群组数量和群组摘要。节点页额外返回每个节点的 `source_ids` 与 `group_ids`；关系页只返回关系。所有分页响应都携带相同的 `revision`。

客户端应先读取 `meta`，随后把该值作为 `expected_revision` 请求各页。加载期间图谱发生变化时，服务端返回 HTTP 409：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "GRAPH_CHANGED",
    "message": "图谱已发生变化，请重新加载……"
  }
}
```

旧的 `GET /api/v1/graph` 行为保持不变。

### 3.4 确定性局部图谱

```http
GET /api/v1/graph/neighborhood/{node_id}?depth=2&direction=both&limit=2000&edge_limit=10000&expected_revision={revision}
```

- `depth`：1～6；
- `direction`：`forward`、`backward` 或 `both`；
- `limit`：节点上限，最大 10000；
- `edge_limit`：内部关系上限，最大 50000；
- `expected_revision`：可选的快照一致性保护。

该端点直接按 `node_id` 执行数据库 BFS，不调用 LLM、Embedding 或 Rerank。响应中的节点包含 `depth`，并用 `truncated` / `edges_truncated` 表示是否达到安全上限。

---

## 4. 查询 API

四种查询模式互相独立：

| 模式 | 端点 | 用途 |
|---|---|---|
| 图谱查询 | `POST /query/graph` | 关键词/实体命中、关系扩展、群总结 |
| 向量查询 | `POST /query/rag` | 文档切片语义召回与重排序 |
| 混合查询 | `POST /query/hybrid` | 先图谱命中并增强关联切片，再执行向量召回和重排序 |
| 全局查询 | `POST /query/global` | 先召回相关节点群，再结合群总结和关键实体生成整体回答 |

四种查询都接受可选查询参数 `force=true`。启用时跳过已有缓存并重新执行完整检索，成功结果仍会更新缓存。默认情况下，CLI、API 与网页端共享 `data/search_cache.db` 中的结果；知识状态或有效配置变化后旧记录不会参与查询。

### 4.1 图谱查询

```http
POST /api/v1/query/graph
Content-Type: application/json
```

请求体：

```json
{
  "query": "知识图谱如何增强向量检索",
  "depth": 3,
  "direction": "both",
  "confidence": 0.7
}
```

字段：

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|:---:|---|---|
| `query` | string | 是 | 非空 | 用户问题或概念描述 |
| `depth` | integer | 否 | 1–10，默认 3 | 单方向关系扩展深度 |
| `direction` | string | 否 | `forward` / `backward` / `both`，默认 `both` | 关系遍历方向 |
| `confidence` | number/null | 否 | 0–1 | 实体/节点匹配置信度阈值；空值使用配置默认值 |

`direction="both"` 会同时执行正向和反向扩展；调用方不应把 `depth=3` 理解成只返回三个节点。

响应 `data` 包含：

```text
query
hit_nodes        # 直接匹配的节点
expanded_nodes   # BFS 扩展得到的节点
edges            # 涉及的关系线
relations        # 一跳：A->[关系]->B
paths            # 多跳：A->[关系]->B->[关系]->C
groups           # 关联节点群总结
```

`relations` 每项保留完整 edge 字段并增加 `text`。`paths` 每项包含：

```json
{
  "text": "知识图谱->[增强]->RAG->[使用]->Embedding",
  "node_ids": ["node-a", "node-b", "node-c"],
  "edge_ids": ["edge-ab", "edge-bc"],
  "weight": 0.84,
  "depth": 2
}
```

多跳路径由本地 BFS 最短树确定性生成，权重取链上最小关系权重，数量受 `graph_path_limit` 控制。关系方向不会为了排版被改写。

---

### 4.2 向量（RAG）查询

```http
POST /api/v1/query/rag
Content-Type: application/json
```

请求体：

```json
{
  "query": "如何导入 PDF 文档",
  "top_k": 10,
  "threshold": 0.6
}
```

字段：

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|:---:|---|---|
| `query` | string | 是 | 非空 | 查询文本 |
| `top_k` | integer/null | 否 | 1–100 | 最多返回多少个重排序结果；空值使用配置默认值 |
| `threshold` | number/null | 否 | 0–1 | 最终分数阈值；空值使用 `rag_similarity_threshold` |

处理流程：

```text
原始问题
  → 查询规划（可选 LLM 改写、同义表达、相关概念与子问题）
  → 一次批量生成全部查询向量，并过滤语义漂移扩展
  → FAISS 多路召回 + 加权 RRF 融合
  → 有界精确词面候选补充（中文短词、标识符等）
  → 分层切片家族折叠并补充父级上下文
  → Rerank → 阈值过滤；仅在默认阈值、扩展查询且无结果时使用受限低分兜底
```

查询规划失败时会退化为原始问题，不会让检索整体失败。Graph、RAG 与 Hybrid
共享同一个已规划查询；Hybrid 的 chunk、实体摘要与节点群向量检索也复用同一批
查询向量，不会为每一路重复请求 Embedding。

响应 `data.results` 中每项包含：

```text
chunk_id
content
score
granularity       # small / medium / large
parent_chunk_id
context           # 父级上下文或 null
source: { source_id, relative_path }
```

---

### 4.3 混合查询

```http
POST /api/v1/query/hybrid
Content-Type: application/json
```

请求体：

```json
{
  "query": "知识图谱和向量检索的关系",
  "graph_depth": 3,
  "rag_top_k": 10,
  "graph_confidence": 0.7,
  "rag_threshold": 0.6
}
```

字段：

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|:---:|---|---|
| `query` | string | 是 | 非空 | 查询文本 |
| `graph_depth` | integer | 否 | 1–10，默认 3 | 图谱扩展深度 |
| `rag_top_k` | integer/null | 否 | 1–100 | RAG 返回数量 |
| `graph_confidence` | number/null | 否 | 0–1 | 图谱节点匹配阈值 |
| `rag_threshold` | number/null | 否 | 0–1 | RAG 结果阈值 |

固定流程：

```text
图谱查询
  → 命中节点和扩展节点
  → chunk_nodes 找到关联文档切片
  → 给这些切片施加 configured hybrid_enhancement_factor
  → Embedding / FAISS / Rerank
  → 同时检索实体摘要和群组总结向量
  → 分别返回 graph、rag、entities 与 communities
```

响应：

```json
{
  "ok": true,
  "data": {
    "graph": { "hit_nodes": [], "expanded_nodes": [], "edges": [], "relations": [], "paths": [], "groups": [] },
    "rag": { "query": "……", "results": [] },
    "entities": [{ "node_id": "…", "keyword": "…", "summary": "…", "score": 0.8, "source": "entity" }],
    "communities": [{ "group_id": "…", "summary": "…", "node_count": 8, "score": 0.6, "source": "community" }]
  },
  "error": null
}
```

Graph 与 RAG 结果不去重，因为一个是结构关系，另一个是文档片段。

实体和群组分数分别乘以 `vector_search.entity_weight` 与
`vector_search.community_weight`。这两类结果是补充上下文，不会改写旧的
`graph` / `rag` 响应结构。

---

### 4.4 混合检索问答

```http
POST /api/v1/query/answer
Content-Type: application/json
```

请求字段与混合查询相同。系统先完成混合检索，再把图谱关系链、节点摘要、RAG 原文片段、实体与群组上下文交给 LLM；模型只能依据这些只读检索证据回答。

响应 `data`：

```text
query
answer       # 带来源说明的知识库回答
retrieval    # 完整混合检索结果，供调用方审计答案依据
```

若没有检索证据，接口不调用模型，直接返回证据不足提示。

---

### 4.5 全局查询

```http
POST /api/v1/query/global
Content-Type: application/json
```

请求体：

```json
{
  "query": "知识库中主要讨论了哪些技术方向？",
  "top_k": 5
}
```

`top_k` 允许 1–100，表示参与回答的最大相关群组数。系统先检索群组总结向量，
再加载这些群组的成员，并选取引用次数最高的 10 个关键实体作为受约束上下文。

响应 `data` 包含：

```text
query
answer          # LLM 基于选中群组与实体生成的回答
communities     # 分数、群组统计和 node_ids
key_entities    # 关键节点、权重、引用次数及所属群组
```

若尚未生成节点群总结，接口不会调用 LLM，而是返回空的 `communities`、
`key_entities` 和“请先生成节点群总结”的提示。

---

### 4.6 搜索缓存与历史

```http
GET    /api/v1/search/cache?page=1&page_size=20
GET    /api/v1/search/cache/{cache_key}
DELETE /api/v1/search/cache?stale_only=true
DELETE /api/v1/search/cache
```

列表按最近命中或更新时间倒序返回。每条记录包含：

```text
cache_key
query_mode       # graph / rag / hybrid / global
query
normalized_query
params           # 已解析的有效查询参数
result_size      # UTF-8 JSON 字节数
state_hash
is_stale         # 是否与当前知识库状态不一致
hit_count        # 真正从缓存返回的次数
created_at / updated_at / last_hit_at
```

详情端点额外返回 `result`，其结构与原查询端点的 `data` 完全一致。读取历史详情不会增加 `hit_count`。

`stale_only=true` 只删除过期记录；不传时清空全部缓存。删除文档或节点时，服务端会主动清空缓存结果，避免已删除正文继续保留在历史结果中。

缓存是透明加速层。缓存数据库损坏或暂时不可写时，查询会降级执行原始检索，不会因缓存故障阻断知识查询。

---

## 5. 文档 API

### 5.1 列出文档

```http
GET /api/v1/documents
GET /api/v1/documents?status=active
GET /api/v1/documents?status=pending
GET /api/v1/documents?status=all
GET /api/v1/documents?status=active&page=1&page_size=20
```

返回：

```json
{
  "ok": true,
  "data": {
    "documents": [
      {
        "source_id": "source-uuid",
        "relative_path": "markdown/example-a1b2c3.md",
        "content_hash": "sha256…",
        "graph_status": "ready",
        "rag_status": "ready",
        "exists_status": "active",
        "created_at": "…",
        "updated_at": "…"
      }
    ],
    "pagination": {
      "page": 1,
      "page_size": 20,
      "total": 1,
      "total_pages": 1
    }
  },
  "error": null
}
```

建议智能体删除或读取内容前，先调用本端点取得 `source_id`。

---

### 5.2 读取转换后的 Markdown 内容

```http
GET /api/v1/documents/{source_id}/content
```

返回 `source_id`、`relative_path`、`content_hash`、Graph/RAG 状态与 `content`。内容是知识库实际使用的 Markdown，而不是原始 PDF/DOCX 二进制文件。

### 5.2.1 精确编辑 Markdown

```http
PUT /api/v1/documents/{source_id}/content
Content-Type: application/json
```

```json
{
  "content": "# 修改后的 Markdown\n",
  "expected_content_hash": "读取内容时获得的 64 位 SHA-256"
}
```

`expected_content_hash` 用于防止多个客户端互相覆盖。若文档在读取后已被其他操作更新，返回 HTTP 409 / `CONTENT_CONFLICT`。

保存成功后只原子替换 Markdown、重算 `content_hash`，并把 `graph_status` 与 `rag_status` 设为 `pending`。旧 `graph_hash`、`rag_hash`、图谱和向量继续保留，直到调用 `/jobs/ingest` 或 `/ingest` 成功完成先准备后替换。保存本身不调用模型。

### 5.2.2 网页预览的知识文档渲染范围

`content` 字段始终返回规范 Markdown 原文；HTTP API 不把它转换成 HTML。网页端在本地完成安全渲染，当前支持：

- CommonMark 与 GFM：标题、列表、任务列表、引用、水平分隔线、表格、脚注、删除线、链接、图片和代码块；
- KaTeX 数学公式：`$...$`、`$$...$$`、`\(...\)`、`\[...\]`，以及 `aligned`、`cases`、矩阵、分数、积分、求和、极限和 `\xrightarrow{}` 等常见结构；
- Mermaid 图表 fenced block，按需加载；
- Obsidian 双链 `[[节点]]`、别名双链 `[[节点|显示名]]`、Callout `> [!NOTE]` 和 `![[嵌入]]` 安全占位；
- YAML/TOML frontmatter 识别与代码语言高亮。

网页预览默认不执行原始 HTML、`<script>` 或任意导入文档脚本。KaTeX 或 Mermaid 语法错误只影响当前公式/图表，并保留原始文本，不会让整个文档预览失败。API 调用方若不使用网页端，应将 `content` 交给自己的 Markdown/数学渲染器；不要把 `content` 当作已经清洗过的 HTML 直接插入页面。

---

### 5.3 上传并导入文档

```http
POST /api/v1/import?ingest=true
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|:---:|---|
| `file` | binary | 是 | 单个本地文档 |
| `ingest` | query bool | 否，默认 `true` | 是否转换后立即构建图谱和 RAG |

支持扩展名：

```text
.pdf, .docx, .pptx, .xlsx, .xlsm, .xls, .epub, .rtf, .eml,
.md, .markdown, .txt, .log, .html, .htm, .rst, .csv, .tsv,
.json, .jsonl, .ndjson, .yaml, .yml, .xml
```

最大文件：**50 MB**。

文本类文件会自动识别 UTF-8、UTF-16、GB18030、Big5 等常见编码；CSV 会同时检测编码与分隔符。
PDF 仅提取已有文本层，不执行 OCR。扫描版 PDF 会返回 `CONVERSION_FAILED`，应由主智能体先完成 OCR 再导入。

导入接口只接受本地普通文件。URL、网页抓取、视频、音频、远程链接和 OCR 不在 kemo-graph 转换层内处理；这些内容应由上游智能体先归一化为 Markdown 或本地文档。

同一转换逻辑也可在项目根目录直接使用：

```powershell
python -m markitdown C:\path\to\document.pdf -o C:\path\to\document.md
python convert.py C:\path\to\document.docx -o C:\path\to\document.md
```

Python 调用方式：

```python
from markitdown import MarkItDown

result = MarkItDown().convert("document.pdf")
markdown = result.text_content
```

CLI、Python API、网页导入和 `provider.tools.document_tools` 共用同一个 Converter 注册表，不会维护多套格式转换逻辑。

PowerShell 示例：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/import?ingest=false" `
  -Form @{ file = Get-Item $env:KEMO_GRAPH_IMPORT_FILE }
```

成功响应：

```json
{
  "ok": true,
  "data": {
    "source_id": "source-uuid",
    "original_filename": "example.pdf",
    "detected_format": "pdf",
    "markdown_relative_path": "markdown/example-a1b2c3d4.md",
    "conversion_status": "completed",
    "ingest_status": "pending",
    "size": 123456,
    "origin_hash": "原文件 SHA-256",
    "content_hash": "规范 Markdown SHA-256",
    "origin_modified_at": "UTC ISO 时间"
  },
  "error": null
}
```

`ingest=true` 时，`ingest_status` 可能为：

```text
completed  # 图谱和 RAG 整理成功
failed     # Markdown 已保留，但整理失败；可稍后重试
```

`failed` 的成功导入响应会携带 `ingest_error` 和可选的 `ingest` 摘要；调用方可随后调用 [5.5 整理文档](#55-整理文档)。

---

### 5.4 兼容的纯 Markdown 文本上传

```http
POST /api/v1/upload
Content-Type: application/json
```

请求：

```json
{
  "filename": "note.md",
  "content": "# 标题\n\n正文"
}
```

该端点只适合智能体已有 Markdown 正文时使用：写入并注册为待整理文档，不会在此请求中调用模型。文件名不能含 `/`、`\\` 或 `..`。

对于 PDF、Office、EPUB、RTF 等二进制文件，必须使用 `/import`。

---

### 5.5 整理文档

```http
POST /api/v1/ingest
Content-Type: application/json
```

请求：

```json
{
  "paths": ["markdown/example-a1b2c3d4.md"],
  "mode": "both"
}
```

字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|:---:|---|
| `paths` | string[] / null | 否 | 要整理的 Markdown 相对路径；`null` 表示扫描所有待处理文档 |
| `mode` | string | 否，默认 `both` | `graph`、`rag`、`both` |

返回包括：

```text
processed
graph_updated
rag_updated
failed
details
```

增量规则：内容哈希没有变化的文档不会重复调用 LLM、Embedding 或 Rerank。

---

### 5.6 删除源文档

```http
DELETE /api/v1/documents/{source_id}
```

这是破坏性操作。执行前建议：

1. `GET /documents` 确认 `source_id`；
2. 必要时 `GET /documents/{source_id}/content` 审核内容；
3. 再发起 DELETE。

内部结果：

```text
Markdown → external/recycle/
sources.exists_status → deleted
删除该 source 的 node_sources / edge_sources / chunks / embeddings / chunk_nodes
同步删除 FAISS 向量
重算仍有来源的节点引用数和边权重
```

响应包含：

```text
deleted_source_id
relative_path
recycled_path
graph_deleted
rag_deleted
```

### 5.7 批量删除与当前库全部删除

```http
POST   /api/v1/documents/delete-batch
DELETE /api/v1/documents?confirm=delete-all
```

批量请求体：

```json
{"source_ids": ["source-uuid-1", "source-uuid-2"]}
```

两者都只作用于当前 API 实例绑定的知识库，不会跨 Portable Store。返回 `requested / deleted / failed / documents / failures`；批量操作逐篇报告失败，已成功项不会因另一篇失败而伪装成未执行。调用方必须在执行前向用户显示影响数量并二次确认；清空端点还要求固定确认参数 `confirm=delete-all`。

---

## 6. 图谱节点与关系 API

### 6.1 获取节点详情

```http
GET /api/v1/nodes/{node_id}
```

返回节点摘要、别名、标签、节点权重、引用次数、全部来源绑定和双向关系。来源包含 `original_path`、`relative_path`、`origin_hash`、处理状态、证据文本与证据权重。

### 6.2 删除节点

```http
DELETE /api/v1/nodes/{node_id}
```

这是破坏性操作。调用方应先使用 `GET /graph` 或图谱查询确认节点 ID、关系线和来源影响。

删除逻辑：

```text
删除 node_id
  → 删除该节点的全部 edges / edge_sources
  → 删除 node_sources、nodes、chunk_nodes 中相关记录
  → 清空旧 groups / group_nodes，等待后续总结重建
  → 检查每个绑定 source：
      若该 source 没有任何其他节点 → Markdown 移入回收站，并级联删除其 Graph/RAG/FAISS 数据
      若仍绑定其他节点 → 只解除当前节点绑定，文件与向量保留
```

响应示例：

```json
{
  "ok": true,
  "data": {
    "deleted_node_id": "node-uuid",
    "deleted_keyword": "知识图谱",
    "cascade_deleted_edges": 3,
    "recycled_files": ["recycle/markdown/example.md"],
    "unlinked_files": ["markdown/another.md"]
  },
  "error": null
}
```

### 6.3 获取关系详情

```http
GET /api/v1/relations/{edge_id}
```

返回源/目标节点、关系、权重、支持数和全部文档证据。`path` 固定使用人类可读格式：

```text
A->[关系]->B
```

### 6.4 删除关系

```http
DELETE /api/v1/relations/{edge_id}
```

这是破坏性操作。系统删除关系及全部 `edge_sources` 证据，使旧节点群总结失效，同步清理群组辅助向量与搜索缓存。节点和源文档本身不会因单独删除关系而删除。

---

## 7. 配置与维护 API

这些端点同样在外部 API 中可用，但属于管理操作。自动化智能体不应擅自修改配置或运行维护动作。

### 7.1 获取配置

```http
GET /api/v1/config
```

返回可读取的当前配置。`kemo.api_key` 若已显式配置只返回固定掩码 `************`，并增加只读 `kemo.api_key_source`（`config / environment / none`）；真实密钥不会回显。

### 7.2 保存完整配置

```http
PUT /api/v1/config
Content-Type: application/json
```

请求体是完整配置对象。配置由服务端校验并原子写入。

**风险**：这是全量配置更新，不是 JSON Patch。调用方必须先 `GET /config`，只修改必要字段后带回完整对象；不要自行编造或删除未知配置项。

密钥优先级：非空 `kemo.api_key` > `kemo.api_key_env` 指定的环境变量。把掩码原样提交会保留现有显式密钥；把字段清空并保存会删除显式密钥并回退到环境变量。`kemo.api_key_source` 和 `kemo.protocol_version` 是 Web 只读字段。

图谱抽取颗粒度由以下两个字段共同控制：

- `graph_extract_granularity`：`small`、`medium` 或 `large`。分别对应细、标准、粗三档；当前默认是 `large`，优先控制节点和关系密度。细粒度会产生更多 Markdown 语义分段，粗粒度请求更少且每段有更严格的实体/关系预算。为兼容旧部署模板，也接受 `fine`、`balanced`、`coarse`，服务端会规范化为上述三档。
- `graph_extract_chunk_size`：颗粒度档位使用的基准字符上限，范围为 `2000~100000`。实际分段上限为基准值乘以档位倍率（`small=0.5`、`medium=1`、`large=2`），并限制在同一范围内。

每段默认预算为：`large` 最多 12 个实体/16 条关系，`medium` 最多 20 个实体/28 条关系，`small` 最多 36 个实体/54 条关系。预算是硬上限而不是填充目标；细节应并入节点 `summary`，不能为了凑数创建碎片节点或共现关系。

修改后，下一次来源扫描会根据图谱抽取配置指纹把已完成文档的 `graph_status` 标为 `pending`；随后可运行网页批量重建或 `python start.py rebuild-knowledge-base`。RAG 配置未变化时不会因此重做向量索引。配置保存动作本身不会直接删除已提交的 Graph/RAG 数据。

### 7.3 手动生成节点群总结

```http
POST /api/v1/maintenance/summarize
```

只有累计变动文件数量达到 `summary_trigger_file_count` 时才会真正生成总结；否则返回跳过原因。该动作可能调用图谱 LLM。

### 7.4 清理过期回收站

```http
POST /api/v1/maintenance/cleanup-recycle
```

按回收站 `.meta.json` 的 `expires_at` 永久删除到期文件；不可恢复。

### 7.5 永久清空回收站

```http
DELETE /api/v1/maintenance/recycle
```

强制删除回收站中的全部文件与元数据，不考虑到期时间。该操作不可恢复，必须由用户明确确认。

### 7.6 后台整理文档

```http
POST /api/v1/jobs/ingest
Content-Type: application/json

{"paths": null, "mode": "both"}
```

与同步 `/ingest` 使用相同参数，但立即返回 job。网页导入完成后使用此端点，避免页面切换或请求连接中断影响长任务。

### 7.7 后台总结节点群

```http
POST /api/v1/jobs/summarize
```

立即返回 `summarize` job，在后台根据图谱连通分量生成节点群摘要。网页端可通过 `/jobs/{job_id}` 或顶部“运行记录”持续追踪，不受页面切换影响；该任务可能调用图谱 LLM。

### 7.8 知识图谱整理

```http
POST /api/v1/maintenance/organize-graph
Content-Type: application/json

{"use_llm": true, "summarize": true}
```

只整理 Graph 投影：合并已检查的重叠节点/关系、迁移来源事实、重算权重和 `chunk_nodes`。不重读 Markdown，不调用 Embedding。

### 7.9 变化文档知识库重建

```http
POST /api/v1/maintenance/rebuild-knowledge-base
```

重试新增、变化、删除和失败文档；未变化文档按哈希跳过。

### 7.10 全项目影子重建

```http
POST /api/v1/maintenance/rebuild-all
```

在影子目录从全部活动 Markdown 重建 Graph、RAG、FAISS 和派生关联。校验通过才切换正式目录，并在 `data/` 同级保留旧知识库备份；失败不改变正式库。

### 7.11 查询后台任务

```http
GET /api/v1/jobs?limit=100
GET /api/v1/jobs/{job_id}
```

| 字段 | 说明 |
|---|---|
| `job_id` / `kind` | 稳定任务身份与类型 |
| `status` | `queued / running / completed / failed` |
| `progress` | 0~1 |
| `detail` | 当前阶段 |
| `result` / `error` | 完成结果或失败摘要 |
| `created_at` / `started_at` / `finished_at` / `updated_at` | UTC ISO 时间 |
| `events[]` | 有序阶段与错误事件 |

任务状态持久化在 `sources.db`。服务重启后遗留的未完成任务会标记为 failed，不会永久卡住。

### 7.12 检查与安装应用更新

```http
GET  /api/v1/update/status
POST /api/v1/update/check
POST /api/v1/update/apply
```

- `status` 只读取本地版本、上次检查状态和 Git 工作区预检，不访问网络。
- `check` 从公开仓库 `kesepain-KE/kemo-graph` 的 `main/version.json` 检查最新 SemVer。
- 根目录 `update.py` 在本地与远端版本相同时也会询问是否强制重新执行更新；
  用户确认后仍必须通过 Git 快进、工作区保护、依赖安装和前端构建检查。
- `apply` 将更新作为 `kind=update` 的持久化后台任务提交，只允许来自 `localhost`、`127.0.0.1` 或 `::1` 的请求。
  旧客户端可以发送空请求体；需要同版本重装时发送 `{"force": true}`，或使用
  `?force=true`。服务端仅在 `force_update_available=true` 且 `can_force_apply=true`
  时接受强制请求；新版本存在时仍走普通升级路径。
- Web 的“强制重新安装”、`python start.py update --force` 与上述 API 使用同一后台任务和
  更新实现，不会绕过工作区保护。
- 自动安装只支持可安全 fast-forward 的 Git 工作区；程序文件有未提交修改时返回 `409 UPDATE_BLOCKED`。
- Git 合并后依赖安装或前端构建失败时，若没有并发的程序文件修改，会自动回滚到更新前提交并恢复用户配置；
  检测到并发代码修改时会停止自动回滚并报告人工处理要求。
- 用户配置和运行数据不参与源码覆盖。安装成功后响应任务结果中的 `restart_required` 为 `true`，调用方应提示用户重启 Web 服务。
- 终端用户也可在项目根目录无参数运行 `python update.py`；更新实现位于 `update/updater.py`，运行状态和备份位于 `update/runtime/`。

### 7.13 Web 进程底层重启

```http
GET  /api/v1/system/runtime
POST /api/v1/system/restart
```

重启请求体必须显式确认：

```json
{"confirm": "restart"}
```

- 只允许本机调用，并且仅在使用 `python start_web.py` 启动时可用。
- 服务先创建独立重启守护器，再向 Uvicorn 请求优雅退出；调度器与维护任务管理器会执行正常关闭流程。
- 存在运行中或排队中的后台任务、每日维护正在执行时，服务会拒绝重启，避免中断数据库和索引写入。
- 守护器等待旧 PID 完全消失和端口释放，再用原始参数启动新的 Python 解释器。
- `runtime` 返回当前 PID，网页据此确认连接到的是新进程，而不是短暂仍存活的旧实例。
- 也可以在项目根目录运行 `python restart.py`，该模块会读取 `update/runtime/web-runtime.json` 并调用同一重启流程。

---

## 8. 智能体推荐调用流程

### 8.1 只导入、不消耗模型额度

```text
POST /import?ingest=false
  → GET /documents
  → GET /documents/{source_id}/content
  → 审核转换后的 Markdown
```

之后再决定是否：

```text
POST /jobs/ingest {"paths":["…"], "mode":"both"}
  → GET /jobs/{job_id}
```

### 8.2 普通查询

```text
GET /status
  → POST /query/hybrid
```

若只需要概念关系，使用 `POST /query/graph`；若只需要原文片段，使用 `POST /query/rag`。

### 8.3 删除前的安全流程

```text
删除文档：
GET /documents → GET /documents/{source_id}/content → DELETE /documents/{source_id}

删除节点：
GET /graph 或 POST /query/graph → 检查节点/边/来源影响 → DELETE /nodes/{node_id}
```

不要依据 keyword 猜测 UUID；始终读取实际 `source_id`、`node_id`。

---

## 9. 已知能力边界

### 9.1 不支持远程 URL 导入

`/import` 只接收上传的本地文件，不接收 URL。调用方若持有网络 URL，应先自行安全下载、校验后再上传。

### 9.2 不支持恢复回收站文件

用户主动删除的文档或节点关联独占文档会移入 `external/recycle/`，但当前没有恢复 API。到期清理后永久删除。

---

## 10. 运行与日志

运行日志默认按 UTC 日期写入：

```text
<configured-log-dir>/YYYY-MM-DD.tsv
```

日志会记录导入、图谱构建、RAG、FAISS、删除、维护和 API 错误的摘要；不会记录 API Key、Bearer Token、完整文档正文或完整模型提示词。

对外 API 实现自身可独立启动：

```powershell
uvicorn api:app --host 127.0.0.1 --port 8000
```

如需在自动化环境运行，调用方应显式配置并保护：

```text
KEMO_API_KEY
KEMO_GRAPH_CONFIG
KEMO_GRAPH_WEB_HOST
KEMO_GRAPH_WEB_PORT
```

---

## 11. 绝对路径分布式 Store API

面向 kemo-agent 和组织级全局扩展，服务可在任意绝对知识位置建立独立数据库与索引。调用方传入知识位置 `store_root`，系统固定使用：

```text
<store_root>\kemo-graph-storage\
```

本文不绑定源码版安装目录、Windows 盘符或部署版容器路径。所有
`<configured-...-root>`、`<absolute-source-file-from-host-config>` 都是部署配置
占位符，不能作为字面值发送。全局拓展必须从自身配置读取路径，在运行时解析为当前
操作系统的绝对路径，再放入 JSON 请求体；不得根据 kemo-graph 的安装目录猜测数据
位置。

其中包含该位置独立的 `sources.db`、`search_cache.db`、`Graph/graph.db`、`RAG/rag.db`、FAISS 索引、规范 Markdown 与回收站。不同 Store 不共享数据库；联合查询只在内存中融合。

### 11.1 初始化

```http
POST /api/v1/stores/initialize
Content-Type: application/json
```

```json
{
  "store_root": "<configured-user-memory-root>",
  "scope": "memory.user",
  "owner_id": "alice",
  "display_name": "Alice 的统一记忆索引"
}
```

`scope` 取值：

```text
knowledge.global
knowledge.shared
knowledge.user
memory.temporary
memory.important
memory.permanent
memory.user
```

`memory.user` 面向 kemo-agent 当前的每用户记忆表存储：同一用户的四档记忆共享
一个 Store，不能再按 tier 拆成多个数据库。旧三档 memory scope 仅为已有调用方
保留兼容。

重复初始化是幂等的，并保持原 `store_id`。已有 Store 不允许静默变更 scope/owner。

### 11.2 响应身份

除 initialize/info 与 federated 外，单 Store 响应的 `data` 统一为：

```json
{
  "store": {
    "store_id": "uuid",
    "scope": "memory.user",
    "owner_id": "alice",
    "root_path": "<configured-user-memory-root>"
  },
  "result": {}
}
```

调用方应使用 `store_id` 与 `scope` 归属结果，不能只依赖路径字符串。

### 11.3 完整端点索引

```text
# 生命周期和导入
POST /api/v1/stores/initialize
POST /api/v1/stores/info
POST /api/v1/stores/status
POST /api/v1/stores/import-path
POST /api/v1/stores/import
POST /api/v1/stores/upload
POST /api/v1/stores/ingest
POST /api/v1/stores/sources/sync
POST /api/v1/stores/sources/status
POST /api/v1/stores/sources/delete

# 检索与回答
POST /api/v1/stores/query/graph
POST /api/v1/stores/query/rag
POST /api/v1/stores/query/hybrid
POST /api/v1/stores/query/answer
POST /api/v1/stores/query/global
POST /api/v1/stores/query/federated

# 文档、节点、关系
POST /api/v1/stores/documents/list
POST /api/v1/stores/documents/content
POST /api/v1/stores/documents/update
POST /api/v1/stores/documents/delete
POST /api/v1/stores/documents/delete-batch
POST /api/v1/stores/documents/delete-all
POST /api/v1/stores/nodes/get
POST /api/v1/stores/nodes/delete
POST /api/v1/stores/relations/get
POST /api/v1/stores/relations/delete

# 图谱和 GPU 分页
POST /api/v1/stores/graph/full
POST /api/v1/stores/graph/visualization/meta
POST /api/v1/stores/graph/visualization/nodes
POST /api/v1/stores/graph/visualization/edges
POST /api/v1/stores/graph/neighborhood

# 搜索缓存
POST /api/v1/stores/cache/list
POST /api/v1/stores/cache/show
POST /api/v1/stores/cache/clear

# 维护与任务历史
POST /api/v1/stores/maintenance/organize-graph
POST /api/v1/stores/maintenance/rebuild-knowledge-base
POST /api/v1/stores/maintenance/rebuild-all
POST /api/v1/stores/maintenance/summarize
POST /api/v1/stores/maintenance/cleanup-recycle
POST /api/v1/stores/jobs/list
POST /api/v1/stores/jobs/get
```

绝对路径放在 JSON body，不放入 URL path/query。Store 维护端点当前同步执行，以免任务被错误提交到默认知识库的后台管理器。

### 11.4 外部表记录同步

kemo-agent 的记忆以 SQLite 表为权威来源。kemo-graph 不直接写该数据库，而由
全局扩展把记录转换为稳定来源包络并推送：

```http
POST /api/v1/stores/sources/sync
Content-Type: application/json
```

```json
{
  "store_root": "<configured-user-memory-root>",
  "ingest_after_sync": false,
  "records": [
    {
      "source_uri": "kemo-agent://users/alice/memory/preferences.md",
      "source_type": "memory.fragment",
      "display_name": "preferences.md",
      "content": "# 用户偏好\n\n回答保持简洁。",
      "revision": "3",
      "updated_at": "2026-08-05T12:00:00+00:00",
      "metadata": {"tier": "permanent", "weight": 2},
      "deleted": false,
      "ingest_mode": "both"
    }
  ]
}
```

同步规则：

- `source_uri` 是跨重启稳定身份；记忆 tier 晋升不得改变 URI；
- `revision` 相同但规范正文不同记为 `conflict`，不会覆盖现有数据；
- `updated_at` 早于现有记录记为 `stale`；
- 正文相同但 revision/metadata 改变时只更新元数据，不重建 Graph/RAG；
- 正文变化后 Graph/RAG 变为 pending；`ingest_after_sync=true` 可立即整理；
- `deleted=true` 是 tombstone：立即级联清理 Graph/RAG 和派生 Markdown，不进入回收站；
- `content_hash` 是上游正文哈希，`sources.content_hash` 是规范 Markdown 哈希，两者分别保存；
- 批次上限 1000 条，单条正文 50 MiB，metadata 64 KiB。

查看状态：

```http
POST /api/v1/stores/sources/status
```

```json
{
  "store_root": "<configured-user-memory-root>",
  "source_type": "memory.fragment",
  "include_deleted": false,
  "page": 1,
  "page_size": 100
}
```

显式删除多个稳定来源：

```http
POST /api/v1/stores/sources/delete
```

```json
{
  "store_root": "<configured-user-memory-root>",
  "source_uris": ["kemo-agent://users/alice/memory/preferences.md"]
}
```

### 11.5 绝对路径导入

```http
POST /api/v1/stores/import-path
```

```json
{
  "store_root": "<configured-user-knowledge-root>",
  "path": "<absolute-source-file-from-host-config>",
  "ingest_after_import": false,
  "expected_origin_hash": "<optional-64-char-source-sha256>"
}
```

`path` 必须为绝对普通文件路径。`expected_origin_hash` 可省略；可靠调用方应传入自己刚扫描得到的原文件 SHA-256。服务端会先把源文件捕获为私有临时快照，确认快照哈希匹配后再转换；哈希不一致或捕获期间文件变化时返回 HTTP 409 / `IMPORT_SOURCE_CHANGED`，不会注册该文档。返回同时包含原文件 `origin_hash` 与规范 Markdown `content_hash`，二者不得混用。

### 11.6 multipart 文件上传导入

调用方与 kemo-graph 不在同一文件系统，或不希望授予服务端源文件读取权限时，使用：

```http
POST /api/v1/stores/import?ingest=false
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|:---:|---|
| `store_root` | text | 是 | 已初始化 portable Store 的绝对路径；放在 multipart 表单中，不进入 URL 日志 |
| `file` | binary | 是 | 单个待转换文档，最大 50 MB |
| `ingest` | query bool | 否，默认 `false` | 转换导入后是否立即构建 Graph 与 RAG |

该端点与 `/import` 共用文件名、扩展名、大小、转换和 ingest 失败校验。响应的 `data`
同时包含稳定 `store` 身份和 `result` 导入结果。portable 上传默认只转换并登记为待整理，
需要立即整理时必须显式传入 `ingest=true`。

### 11.7 联合查询

```http
POST /api/v1/stores/query/federated
```

```json
{
  "store_roots": [
    "<configured-global-knowledge-root>",
    "<configured-user-memory-root>"
  ],
  "query": "问题",
  "mode": "hybrid",
  "top_k": 20,
  "graph_depth": 3,
  "force": false
}
```

支持 `graph/rag/hybrid/global/answer`。响应中的每条 `merged_results` 带 Store 身份和 `federated_score`。单 Store 失败记录在 `stores_failed`，不会取消其他 Store 的成功结果；调用方必须检查成功数与失败列表。

### 11.8 给 kemo-agent 全局拓展的推荐数据域映射

全局拓展应把一个 `store_root` 理解为一个**数据域位置**，而不是一条记录或一个
记忆 tier。每个数据域只创建一个可见的 `kemo-graph-storage`：

| 上游数据域 | 推荐 `store_root` | scope | 写入方式 |
|---|---|---|---|
| 全局知识文件 | `<agent_root>/global_knowledge` | `knowledge.global` | `stores/import-path` |
| 跨用户共享知识文件 | `<agent_root>/shared_knowledge` | `knowledge.shared` | `stores/import-path` |
| 用户知识文件 | `<agent_root>/users/<user>/knowledge` | `knowledge.user` | `stores/import-path` |
| 用户记忆表 | `<agent_root>/users/<user>/improve` | `memory.user` | `stores/sources/sync` |

边界规则：

- 知识库仍以文件为权威来源；全局拓展枚举允许格式的文件并逐个调用
  `import-path`，不要把文件正文伪装成记忆表记录；
- `memory.sqlite3` 是 kemo-agent 的权威业务库；kemo-graph 不直接写入，也不要求
  获得 SQLite 写权限；
- 同一用户的 `seven_days / one_month / half_year / permanent` 记忆 tier
  全部进入同一个 `memory.user` Store，tier 放在 `metadata.tier`；
- 不要为单条记忆、单个会话或单个文件创建独立 Store；这会产生过多 SQLite、
  FAISS 和搜索缓存文件，也会让联合检索成本线性放大。

### 11.9 全局拓展启动与刷新顺序

推荐的启动握手：

```text
1. POST /stores/initialize  幂等确保每个已配置数据域存在
2. POST /stores/info        保存并核对 store_id、scope、owner_id、root_path
3. POST /stores/status      读取文档、Graph、RAG、FAISS 健康状态
4. 推送本轮变化文件/记录
5. 对发生正文变化的 Store 执行 /stores/ingest
6. 再次调用 /stores/status；必要时 POST /stores/documents/list 并在 body 传 status=pending
7. 对用户查询调用单 Store hybrid，或调用 federated 跨域融合
```

`stores/initialize` 可以安全重复调用；scope 或 owner 与现有 manifest 不一致时不会
静默改写，而是返回 `STORE_INVALID`。全局拓展应持久保存响应中的 `store_id`，启动
时同时核对物理路径与稳定 ID，防止路径配置误指向另一套知识库。

初始化成功响应示例（部分字段）：

```json
{
  "ok": true,
  "data": {
    "manifest": {
      "schema_version": 1,
      "store_id": "66c96ea6-d0b3-4e50-accc-d9bc1bc51519",
      "display_name": "Alice 的统一记忆索引",
      "scope": "memory.user",
      "owner_id": "alice",
      "root_path": "<configured-user-memory-root>",
      "storage_directory": "kemo-graph-storage"
    },
    "storage_root": "<configured-user-memory-root>/kemo-graph-storage",
    "initialized_now": true,
    "status": {}
  },
  "error": null
}
```

### 11.10 SourceEnvelope 严格字段契约

`records` 每项字段如下。请求模型拒绝任何未知字段。

| 字段 | 必填 | 限制 | 语义 |
|---|---:|---|---|
| `source_uri` | 是 | 1–2048 字符；小写 URI scheme；批次内唯一 | 跨重启、改名策略和 tier 晋升保持稳定的权威身份 |
| `source_type` | 是 | 1–128；`^[a-z][a-z0-9_.-]*$` | 如 `memory.fragment` |
| `display_name` | 是 | 1–255；只能是文件名，不能含路径或 `..` | 用户可读逻辑名称；不作为身份 |
| `content` | 非删除时是 | UTF-8 规范正文；最大 50 MiB | 用于生成派生 Markdown、Graph 与 RAG |
| `content_hash` | 否 | 64 位 SHA-256 十六进制 | 上游正文哈希；调用方负责计算正确性 |
| `revision` | 是 | 1–255 字符 | 上游不透明版本；可以是整数转字符串、UUID 或业务版本 |
| `updated_at` | 是 | 带时区 ISO 8601；最大 64 字符 | 因果顺序；服务端统一比较 UTC 时间 |
| `metadata` | 否 | JSON 对象；序列化后最大 64 KiB | tier、weight、filename_key 等非正文属性 |
| `deleted` | 否 | 默认 `false` | `true` 表示上游 tombstone；此时 `content` 可省略 |
| `ingest_mode` | 否 | `graph / rag / both`，默认 `both` | 本条正文后续需要构建的投影 |

正文会统一换行为 LF，并保证末尾换行。`content_hash` 是上游提供的身份校验信息；
kemo-graph 另行计算规范 Markdown 的 `sources.content_hash`。两个哈希不能互换。

推荐稳定 URI：

```text
# 记忆：使用不会因 filename/tier 改变的数据库记录 ID
kemo-agent://users/alice/memory/fragments/1842
```

不要把正文哈希放进 `source_uri`；否则正文每次变化都会被识别为新来源，旧 Graph、
RAG 与向量将无法按同一身份增量替换。

#### 11.9.1 memory_fragments 映射建议

| kemo-agent 字段 | SourceEnvelope |
|---|---|
| `id` | `source_uri` 末段稳定 ID |
| `filename` | `display_name` |
| `content` | `content` |
| `content_hash` | `content_hash` |
| `revision` | `revision`（转字符串） |
| `content_updated_at` | `updated_at` |
| `tier / weight / filename_key` | `metadata` |

tier 晋升只产生 metadata/revision 更新，不改变 URI；正文没有变化时服务端会返回
`metadata_updated`，不会重新构建 Graph/RAG。

### 11.11 同步结果、幂等和删除状态机

成功同步返回 HTTP 200。即使某条记录为 `stale` 或 `conflict`，HTTP 仍为 200，
调用方必须检查 `data.result` 中的计数与 `details`：

```json
{
  "ok": true,
  "data": {
    "store": {
      "store_id": "66c96ea6-d0b3-4e50-accc-d9bc1bc51519",
      "scope": "memory.user",
      "owner_id": "alice",
      "root_path": "<configured-user-memory-root>"
    },
    "result": {
      "received": 3,
      "created": 1,
      "updated": 0,
      "metadata_updated": 1,
      "unchanged": 0,
      "deleted": 0,
      "stale": 1,
      "conflicts": 0,
      "failed": 0,
      "details": [
        {
          "source_uri": "kemo-agent://users/alice/memory/fragments/1842",
          "source_id": "kemo-graph-source-uuid",
          "status": "created"
        }
      ],
      "ingest": null,
      "search_cache_deleted": 4
    }
  },
  "error": null
}
```

| 状态 | 条件 | 调用方动作 |
|---|---|---|
| `created` | URI 首次出现 | 等待/触发 ingest |
| `updated` | 正文变化，或删除后恢复 | 等待/触发 ingest |
| `metadata_updated` | 正文相同，仅版本或 metadata 变化 | 无需 ingest |
| `unchanged` | 完全幂等重放，或删除不存在的 URI | 无操作 |
| `deleted` | 活动来源收到 tombstone | 从本地同步游标确认删除已消费 |
| `stale` | `updated_at` 早于现有记录 | 不要重试旧事件；刷新上游游标 |
| `conflict` | 相同 revision 对应不同规范正文 | 停止自动覆盖并记录告警，检查上游版本生成逻辑 |

批次会在写入前统一校验：最多 1000 条，批次内 URI 不得重复。格式校验失败会让
整个请求返回 422，而不会只跳过某一条。

删除有两种入口：

1. **上游同步删除（推荐）**：在 `sources/sync` 中发送 `deleted=true`，同时携带
   上游 revision 与 updated_at，保留完整因果顺序；
2. **运维强制删除**：调用 `sources/delete` 只传 `source_uris`，由服务端生成删除
   revision 与时间，适用于人工清理，不适合作为上游 change feed 的常规路径。

tombstone 会立即删除派生 Markdown，并级联清理 Graph、RAG、FAISS 和搜索缓存；
派生 Markdown 不进入回收站，因为上游 SQLite 才是可恢复的权威来源。以后使用
同一 URI 和更新版本恢复时，会复用原来的 kemo-graph `source_id`。

### 11.12 整理策略与超时

`ingest_after_sync` 的选择：

- `false`（推荐给事件接收线程）：只同步并标记 pending，响应较快；全局拓展的
  后台 worker 再合并变化并调用 `/stores/ingest`；
- `true`：在同一个 HTTP 请求内按每条 `ingest_mode` 直接构建，可能持续数分钟，
  调用方必须配置长超时，且不应占用用户请求线程。

`stores/ingest` 请求：

```json
{
  "store_root": "<configured-user-memory-root>",
  "paths": null,
  "mode": "both"
}
```

`paths=null` 处理当前 Store 的全部 pending 项；指定路径时必须使用 kemo-graph
返回的 `relative_path`，不能传 `source_uri` 或上游绝对路径。调用方可通过
`sources/status` 取得路径，并按业务所需的 `ingest_mode` 分组整理。

整理响应示例：

```json
{
  "processed": 3,
  "graph_updated": 2,
  "rag_updated": 3,
  "skipped": 0,
  "failed": 1,
  "details": [
    {
      "path": "env-reference-eacda38418.md",
      "graph": "failed",
      "rag": "updated",
      "graph_error": "Provider request failed"
    }
  ]
}
```

HTTP 200 不代表每篇整理都成功，必须检查 `failed` 与 `details`。失败项可以通过
指定其 `relative_path` 再次调用 ingest；已经 ready 的另一投影不会重复构建。

建议客户端超时：

| 操作 | 建议总超时 |
|---|---:|
| info/status/source-status | 15 秒 |
| graph/rag/hybrid 查询 | 120 秒 |
| answer/global（包含 LLM） | 300 秒 |
| import-path | 120 秒 |
| ingest/organize/rebuild/summarize | 30 分钟或交给后台 worker |

同一个 Store 的写操作应由全局拓展串行化。不要启动多个 kemo-graph Web 进程同时
写同一套 SQLite/FAISS；构建期间查询可能返回 `409 PROCESSING`。

### 11.13 检索模式选择

| 需求 | 端点 | 是否生成自然语言回答 | 建议 |
|---|---|---:|---|
| 精确概念、关系和路径 | `query/graph` | 否 | 返回节点、边、`A->[关系]->B` 路径 |
| 原文证据与语义相似内容 | `query/rag` | 否 | 返回 chunk、父级上下文、分数和来源 |
| 智能体自行组织最终回答 | `query/hybrid` | 否 | **全局拓展默认选择**；同时返回 Graph 与 RAG |
| 让 kemo-graph 直接回答 | `query/answer` | 是 | 会额外调用 LLM；适合独立知识问答入口 |
| 节点群级全局问题 | `query/global` | 是 | 必须先执行 summarize 生成节点群总结 |
| 跨知识/记忆/历史数据域 | `query/federated` | 取决于 mode | 对各 Store 独立查询后在内存中融合 |

推荐的单 Store 混合检索请求：

```json
{
  "store_root": "<configured-user-memory-root>",
  "query": "用户偏好的回答风格是什么？",
  "graph_depth": 2,
  "rag_top_k": 8,
  "graph_confidence": null,
  "rag_threshold": null,
  "direction": "both",
  "force": false
}
```

- `graph_depth`：1–10；普通问答建议 1–2，系统关系分析再使用 3 以上；
- `rag_top_k`：1–100；`null` 使用服务端配置；
- confidence/threshold 为 `null` 时使用服务端配置；
- `direction`：`forward / backward / both`；
- `force=true` 只绕过搜索结果缓存，不会绕过 processing 状态或数据一致性检查。

混合响应的 `data.result` 主要字段：

```text
graph.hit_nodes       直接命中的节点
graph.expanded_nodes  BFS 扩展节点
graph.edges           机器可读关系边
graph.relations       A->[关系]->B
graph.paths           A->[关系]->B->[关系]->C
graph.groups          相关节点群
rag.results           召回 chunk、父级 context、score、source
entities              实体向量召回
communities           群组总结向量召回
```

全局拓展通常应把这些证据交给 kemo-agent 的主回答模型，而不是再调用
`query/answer`，避免两次 LLM 总结和上下文损失。

### 11.14 跨 Store 联合检索响应

`query/federated` 最多接受 100 个绝对 Store 路径。每个 Store 独立失败隔离：

```json
{
  "ok": true,
  "data": {
    "query": "用户最近关心什么？",
    "mode": "hybrid",
    "stores_requested": 3,
    "stores_succeeded": 2,
    "stores_failed": [
      {
        "store_root": "<configured-user-history-root>",
        "error_type": "PortableStoreNotInitializedError",
        "message": "知识位置尚未初始化"
      }
    ],
    "stores": [
      {
        "store": {
          "store_id": "uuid",
          "scope": "memory.user",
          "owner_id": "alice",
          "store_root": "<configured-user-memory-root>"
        },
        "result": {}
      }
    ],
    "merged_results": []
  },
  "error": null
}
```

必须同时检查 `stores_succeeded` 和 `stores_failed`；HTTP 200 只代表联合查询调度
成功，不代表每个 Store 都可用。`merged_results` 主要融合可排序的 RAG/实体/群组
结果；完整的单 Store Graph 结构仍位于 `stores[i].result`。

### 11.15 状态、分页与变更游标

Store 健康检查：

```http
POST /api/v1/stores/status
```

`data.result`：

```json
{
  "initialized": true,
  "sources": {
    "total": 3,
    "active": 3,
    "pending_graph": 0,
    "pending_rag": 0
  },
  "graph": {
    "total_nodes": 98,
    "total_edges": 135,
    "total_groups": 0
  },
  "rag": {
    "total_chunks": 21,
    "total_vectors": 21,
    "faiss_healthy": true
  }
}
```

`status` 不列出每条失败原因。发现 pending、构建失败或数量异常时，调用：

```text
POST /stores/documents/list  查看 graph_status/rag_status/content_hash
POST /stores/sources/status  查看 URI/revision/metadata/relative_path
POST /stores/jobs/list       查看维护任务记录
POST /stores/jobs/get        查看单任务事件
```

`sources/status` 返回的每项包括：`source_id/source_uri/source_type/revision/`
`source_updated_at/metadata/external_content_hash/last_synced_at/relative_path/`
`content_hash/graph_status/rag_status/exists_status/created_at/updated_at`。

kemo-graph 当前不提供 kemo-agent 上游表的 change feed；全局拓展应在自己的业务层
维护游标，例如 `(content_updated_at, id)` 或业务 revision，并只把变化项推送给
`sources/sync`。接口本身允许安全重放，不能用“可能重复”作为跳过幂等设计的理由。

### 11.16 错误处理与重试矩阵

| HTTP | code | 是否重试 | 全局拓展处理 |
|---:|---|---:|---|
| 403 | `STORE_ACCESS_DENIED` | 否 | 检查绝对路径、`..`、allowed_roots 和进程权限 |
| 404 | `STORE_NOT_INITIALIZED` | 条件式 | 对已授权数据域执行一次 initialize，再重试原请求 |
| 404 | `NOT_FOUND` | 否 | 刷新 source/node/edge ID；不要无限重试旧 ID |
| 409 | `PROCESSING` | 是 | 1、2、4、8 秒退避并设置总等待上限 |
| 409 | `GRAPH_CHANGED` | 是 | 刷新 visualization revision 后重取分页 |
| 409 | `CONTENT_CONFLICT` | 否 | 重新读取最新 content_hash，人工/业务层解决并发编辑 |
| 413 | `FILE_TOO_LARGE` | 否 | 在上游拆分或拒绝文件 |
| 415 | `UNSUPPORTED_FORMAT` | 否 | 上游转换为受支持格式 |
| 422 | `INVALID_PARAM` | 否 | 修正字段、哈希、时间、URI 或分页参数 |
| 422 | `STORE_INVALID` | 否 | 检查 manifest、scope、owner 和目录完整性 |
| 422 | `CONVERSION_FAILED` | 条件式 | 修复文档/编码后重试；扫描 PDF 需先 OCR |
| 502 | `INGEST_FAILED` | 是 | Provider 临时失败可有限重试；保留失败详情 |
| 500 | `INTERNAL` | 有限 | 记录 request 上下文，指数退避最多 2–3 次 |

只有网络错误、超时、`PROCESSING`、临时 Provider/500 错误适合自动重试。422、403、
内容冲突和同步 `conflict` 必须先修正数据或配置。

### 11.17 可直接用于全局拓展的 Python 客户端示例

以下示例只使用 Python 标准库，展示初始化、同步、整理和混合查询。生产实现应把
HTTP 调用放入 kemo-agent 的全局拓展服务层，并增加日志、取消信号和持久化游标。

```python
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_URL = os.environ.get(
    "KEMO_GRAPH_BASE_URL",
    "http://127.0.0.1:8000/api/v1",
).rstrip("/")
STORE_ROOT = os.environ["KEMO_GRAPH_STORE_ROOT"]


class KemoGraphAPIError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code


def post(path: str, payload: dict[str, Any], *, timeout: float = 120) -> Any:
    request = Request(
        BASE_URL + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        error = body.get("error") or {}
        raise KemoGraphAPIError(
            exc.code,
            str(error.get("code") or "HTTP_ERROR"),
            str(error.get("message") or exc.reason),
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"kemo-graph 不可达：{exc.reason}") from exc
    if not body.get("ok"):
        error = body.get("error") or {}
        raise KemoGraphAPIError(
            200,
            str(error.get("code") or "UNKNOWN"),
            str(error.get("message") or "未知错误"),
        )
    return body["data"]


# 1. 幂等初始化
store = post(
    "/stores/initialize",
    {
        "store_root": STORE_ROOT,
        "scope": "memory.user",
        "owner_id": "alice",
        "display_name": "Alice 的统一记忆索引",
    },
)
print("store_id:", store["manifest"]["store_id"])

# 2. 推送一条变化记录；实际连接器应由 memory_store 业务层提供这些字段
sync = post(
    "/stores/sources/sync",
    {
        "store_root": STORE_ROOT,
        "ingest_after_sync": False,
        "records": [
            {
                "source_uri": "kemo-agent://users/alice/memory/fragments/1842",
                "source_type": "memory.fragment",
                "display_name": "preferences.md",
                "content": "# 用户偏好\n\n回答保持简洁。\n",
                "revision": "3",
                "updated_at": "2026-08-05T12:00:00+00:00",
                "metadata": {
                    "tier": "permanent",
                    "weight": 2,
                    "filename_key": "preferences.md",
                },
                "deleted": False,
                "ingest_mode": "both",
            }
        ],
    },
)
summary = sync["result"]
if summary["conflicts"] or summary["failed"]:
    raise RuntimeError(f"来源同步未完成：{summary['details']}")

# 3. 在后台 worker 中整理 pending 内容；模型调用可能耗时较长
if summary["created"] or summary["updated"]:
    ingest = post(
        "/stores/ingest",
        {"store_root": STORE_ROOT, "paths": None, "mode": "both"},
        timeout=1800,
    )
    if ingest["result"]["failed"]:
        raise RuntimeError(f"知识整理存在失败项：{ingest['result']['details']}")

# 4. 让主智能体取得 Graph + RAG 证据，自行组织最终回答
retrieval = post(
    "/stores/query/hybrid",
    {
        "store_root": STORE_ROOT,
        "query": "用户偏好的回答风格是什么？",
        "graph_depth": 2,
        "rag_top_k": 8,
        "graph_confidence": None,
        "rag_threshold": None,
        "direction": "both",
        "force": False,
    },
)
evidence = retrieval["result"]
print(json.dumps(evidence, ensure_ascii=False, indent=2))
```

### 11.18 PowerShell 联调示例

```powershell
$base = if ($env:KEMO_GRAPH_BASE_URL) {
  $env:KEMO_GRAPH_BASE_URL.TrimEnd("/")
} else {
  "http://127.0.0.1:8000/api/v1"
}
$store = $env:KEMO_GRAPH_STORE_ROOT
if (-not $store) {
  throw "请先由部署配置设置 KEMO_GRAPH_STORE_ROOT"
}

$status = Invoke-RestMethod `
  -Method Post `
  -Uri "$base/stores/status" `
  -ContentType "application/json" `
  -Body (@{ store_root = $store } | ConvertTo-Json)

$query = @{
  store_root = $store
  query = "定时任务为什么必须经过 time_plan？"
  graph_depth = 2
  rag_top_k = 8
  graph_confidence = $null
  rag_threshold = $null
  direction = "both"
  force = $false
} | ConvertTo-Json

$result = Invoke-RestMethod `
  -Method Post `
  -Uri "$base/stores/query/hybrid" `
  -ContentType "application/json" `
  -Body $query

$result.data.result | ConvertTo-Json -Depth 20
```

### 11.19 部署与安全底线

- API 当前没有应用层认证；默认仅监听 `127.0.0.1`；
- 全局拓展不应把来自用户输入的任意字符串直接作为 `store_root`，必须从受控的
  用户/数据域配置映射生成；
- 建议设置 `portable_stores.allowed_roots`，限制只能访问 kemo-agent 数据根目录；
- `store_root` 必须是绝对路径，不能含 `..`，也不能直接指向
  `kemo-graph-storage`；
- 不要把 API 暴露到公网。跨主机部署必须在外层提供 TLS、认证、授权和审计；
- 正文会通过 Kemo 网关执行图谱抽取、Embedding、Rerank 或回答，敏感数据策略应
  同时覆盖 kemo-agent、kemo-graph 和网关链路；
- 可通过 `http://127.0.0.1:8000/docs` 查看交互式 OpenAPI，或读取
  `/openapi.json` 生成客户端；实现连接器时仍以本文的生命周期和幂等规则为准。

更完整的目录、清单、迁移和隔离规则见：

> `开发文档/编程方案/16-绝对路径分布式知识库与全量API.md`
