# kemo-graph

<p align="center">
  <img src="kemo-graph-logo.png" alt="kemo-graph logo" width="200">
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong>
</p>

<p align="center">
  <strong>Local knowledge graph and retrieval infrastructure for the Kemo ecosystem.</strong>
</p>

<p align="center">
  Turn multi-format materials into a traceable knowledge graph and vector index,<br>
  so agents can not only find the source text, but also understand concepts, relations, provenance and context.
</p>

<p align="center">
  <a href="version.json"><img src="https://img.shields.io/badge/version-1.3.1-00a98f" alt="version 1.3.1"></a>
  <a href="https://github.com/kesepain-KE/kemo-graph"><img src="https://img.shields.io/badge/status-early%20development-5966d9" alt="status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-green.svg" alt="license"></a>
  <a href="api.md"><img src="https://img.shields.io/badge/API-agent%20integration-0ea5e9" alt="API"></a>
</p>

---

## What if agents didn't have to guess from a pile of files

An agent that serves you over the long term will eventually run into the same problem.

Materials keep growing: project documents, course notes, design drafts, saved web pages, PDFs, Word files, spreadsheets and scattered records — all carefully kept. Yet when the agent actually needs to answer a question, it often ends up retrieving a few similar passages and guessing the context around them.

It may find "what was mentioned", but not necessarily: which concepts this one relates to; which documents support a given relation; which source a search result came from; or which knowledge is still trustworthy after files are updated or deleted.

**kemo-graph aims to be the layer in the Kemo ecosystem that handles exactly this.**

It ties raw materials, converted Markdown, graph nodes, relation evidence and text vectors into one maintainable chain. Agents no longer just "search for answers" in files — they can follow the knowledge structure to find relations, then return to the original text to confirm the evidence.

It is not another agent that chats. It is the knowledge layer that lets agents use, understand and maintain materials over the long term.

---

## What it does

| Scenario | Capability |
|---|---|
| Project knowledge accumulation | Connect design documents, notes and decision records into a queryable concept network |
| Agent knowledge collaboration | Let kemo-agent fetch graph relations and source evidence through the API on demand |
| Multi-format ingestion | Convert local PDF, Word, PowerPoint, Excel, email, text, tabular and structured-data files into Markdown |
| Source tracing | Every graph or retrieval hit can go back to its original document — no conclusion without evidence |
| Multiple retrieval modes | Graph, vector, hybrid, Q&A and global-topic search for different kinds of questions |
| Tunable extraction and robust recall | Fine/standard/coarse graph extraction profiles, query expansion, multi-query FAISS fusion and exact-term fallback for both semantic and short-keyword hits |
| Incremental maintenance | Only affected data is updated when files change, instead of rebuilding the whole knowledge base |
| Safe deletion | Shared sources are checked before deletion to avoid harming knowledge supported by other documents |
| Standalone deployment | Local Web, CLI and HTTP API, or as a knowledge backend for the wider Kemo ecosystem |

---

## Quick start

### Requirements

- Python 3.10+
- Node.js 18+ (needed to build the web frontend)
- Git
- An accessible Kemo gateway (kemo-adapter-api) with registered LLM, Embedding and Rerank models

### Get and run

```powershell
git clone https://github.com/kesepain-KE/kemo-graph.git
cd kemo-graph

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
```

At minimum, configure your gateway key in `.env`:

```dotenv
KEMO_API_KEY=your-kemo-gateway-key
```

Graph extraction defaults to `large` (coarse) and can be adjusted in the Web “System settings” panel or `config/config.json` to `small` (fine), `medium` (standard), or `large` (coarse). Use `graph_extract_chunk_size` to tune the baseline section size. Coarse mode also applies per-section entity/relation budgets and puts details into summaries instead of creating graph fragments. Retrieval keeps the original query, adds bounded synonym/concept expansions, fuses multi-query candidates, and uses an exact-term fallback for short keywords and identifiers.

After changing the graph profile, the next scan marks completed documents as Graph-pending automatically. Run `python start.py rebuild-knowledge-base` to rebuild the graph without rebuilding unchanged RAG vectors.

Confirm the gateway address and models in `config/config.json`, then build the web frontend and start:

```powershell
cd web\frontend
npm install
npm run build
cd ..\..

python start_web.py
```

Open `http://127.0.0.1:8000`.

---

## Basic usage

### Web

Once running, the browser lets you: upload materials, inspect converted Markdown and processing status, browse the knowledge graph, search in different modes, manage documents and the recycle bin, and follow background task progress.

<p align="center">
  <img src="kemo-graph-web.png" alt="kemo-graph web knowledge retrieval interface" width="100%">
</p>

<p align="center">
  <sub>A local-first knowledge retrieval workspace combining graph, vector, hybrid retrieval and LLM answers.</sub>
</p>

#### Knowledge-document preview and rendering

The Web preview renders the normalized Markdown that is actually used by Graph/RAG, rather than sending PDF, Office or other binary files directly to the browser. It uses `react-markdown`, GFM, KaTeX and a Mermaid renderer loaded on demand. Supported features include:

- headings, bold, italic, strikethrough, nested lists, task lists, block quotes, thematic breaks, tables, footnotes, links, images and fenced code;
- inline math such as `$a^2+b^2=c^2$` and display math with `$$...$$`;
- common LaTeX wrappers `\(...\)` and `\[...\]`, plus `aligned`, `cases`, matrices, fractions, integrals, sums, limits, Chinese `\text{}` and `\xrightarrow{}`;
- language-aware code highlighting;
- Mermaid fenced blocks, for example:

  ````markdown
  ```mermaid
  flowchart TD
      A[Source] --> B[Markdown]
      B --> C[Knowledge graph]
      B --> D[RAG vectors]
  ```
  ````

- Obsidian-style links: `[[Data structures]]` and `[[Data structures|open the concept]]`;
- Obsidian-style callouts:

  ```markdown
  > [!NOTE] Note
  > This body is rendered as a themed information panel.
  ```

- safe placeholder rendering for `![[document or image]]` embeds;
- YAML/TOML frontmatter recognition, hidden from the normal reading flow.

Math is rendered with KaTeX. Wide matrices and long formulas scroll inside the document preview instead of breaking the surrounding card. Mermaid is dynamically loaded only when a document contains a diagram, so ordinary documents do not pay the chart-engine startup cost.

Raw HTML and `<script>` are intentionally not executed, preventing imported documents from becoming script-injection surfaces. Web pages, video, audio, OCR and remote links should still be normalized by the upstream agent before they reach kemo-graph. Unsupported custom KaTeX macros are kept as source with a localized error indicator; they do not blank the whole document.

### Command line

```powershell
# Convert to Markdown only; no knowledge-base initialization or model call
python -m markitdown $env:KEMO_GRAPH_IMPORT_FILE -o output.md
python convert.py $env:KEMO_GRAPH_IMPORT_FILE -o output.md

# Import a file (without spending model quota yet)
python start.py import $env:KEMO_GRAPH_IMPORT_FILE --no-ingest

# Scan and ingest all pending documents
python start.py ingest

# Query
python start.py query-hybrid "how does a knowledge graph improve retrieval"
python start.py query-answer "answer based on both graph and source text"

# Sync authoritative kemo-agent table records into their independent Store
python start.py --store-root $env:KEMO_GRAPH_STORE_ROOT source-sync records.json

# Status and maintenance
python start.py status
python start.py list-docs
python start.py organize-graph
python start.py rebuild-all

# Check and apply updates
python start.py update-check
python start.py update

# Root updater: asks whether to force a refresh when versions are identical
python update.py
```

The normalization layer handles local ordinary files only. Web crawling, video, audio, OCR and remote links should be handled by the upstream agent before it passes Markdown or a local file to kemo-graph; the converter never accesses the network.

### HTTP API

kemo-graph can run as a standalone service, exposing graph query, hybrid retrieval, import and maintenance endpoints to agents such as kemo-agent:

```powershell
uvicorn api:app --host 127.0.0.1 --port 8000
```

The full request fields, response envelope and error codes are defined in [api.md](api.md).

---

## Data and privacy

- Converted Markdown, graph and index data all live in your local working directory; Markdown is the source of truth, everything else can be rebuilt at any time.
- Graph building, Embedding and Rerank call models through the Kemo gateway; before processing sensitive materials, review your gateway, model and network boundaries.
- Logs keep only operation summaries and error categories — never keys, full document content or full prompts.

> **Note**: the external API has no built-in application-level authentication. It should only listen on `127.0.0.1` by default; for cross-device or public access, put a VPN, reverse proxy, TLS or authentication layer in front. Never expose an unprotected port directly.

---

## What we want it to become

kemo-graph does not try to replace every file manager, every database, or every search system.

It aims to be a stable knowledge foundation in the Kemo ecosystem: materials keep their provenance once they enter the system; relations can be discovered without drifting from source evidence; when files change, the system knows what to update and what to keep; models and providers may change, but the local source of truth stays in your hands.

An agent that truly accompanies a long-lived project should not only have a longer context window — it should also have a knowledge foundation that can keep understanding materials, verifying sources and maintaining structure.

---

## Current status

The core loop is already runnable: unified import, incremental updates, graph and vector retrieval, hybrid Q&A, safe deletion, scheduled maintenance, plus three entry points (Web, CLI, HTTP API) and an external knowledge-service interface for agents such as kemo-agent.

The current release is **1.3.1**. It hardens local-file import: `/stores/import-path` may receive a caller-confirmed SHA-256, and the service creates one private snapshot from an already-open file handle for hashing, conversion, and post-conversion verification. A source that changes during import returns `409 IMPORT_SOURCE_CHANGED` without committing mismatched Markdown or source mappings. Concurrent imports use unique temporary files, failures restore Markdown and the file map, and Big5, GB18030, Shift-JIS, CP1250, and CP1252 text detection is more reliable. The original absolute path remains the source identity, so repeated imports do not needlessly replace the `source_id`.

Still being polished: conversion quality for complex document layouts, storage and index strategy for large knowledge bases and high concurrency, built-in authentication and permission tiers for the external API, and richer manual graph correction and provenance review interfaces.

If you are trying this early version, bug reports, retrieval feedback, sample document formats, and real scenarios of how you want agents to use a knowledge base are all welcome.

---

## Related Kemo ecosystem projects

- [kemo-agent](https://github.com/kesepain-KE/kemo-agent) — a local multi-user Agent Runtime for personal AI infrastructure; it can use kemo-graph's graph and RAG knowledge through the API.
- [kemo-adapter-api](https://github.com/kesepain-KE/kemo-adapter-api) — the unified Kemo model gateway, providing LLM, Embedding and Rerank models to ecosystem components over one protocol.

Each project can be used independently, or together as one local AI infrastructure where each plays its own role.

---

## Maintainer

[@kesepain](https://github.com/kesepain-KE)

---

## Contributing

kemo-graph is still in early development. Bug reports, format samples, retrieval quality feedback, documentation improvements and feature contributions are all welcome.

Suggested flow: Fork this repository → create a feature branch → make your changes and run the necessary tests → open a Pull Request explaining what changed, why, and how it was verified.

---

## License

This project is open-sourced under the [Apache License 2.0](LICENSE).
