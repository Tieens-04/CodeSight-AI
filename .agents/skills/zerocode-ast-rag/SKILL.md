---
name: zerocode-ast-rag
description: Use when extracting Python or TypeScript AST chunks with tree-sitter, calling gemini-embedding-001, designing or querying Qdrant collections, or implementing GitHub Blob RAM retrieval.
---

# Zero-Code AST Indexing & Hybrid Qdrant RAG

## Overview

Enforces **Zero-Code Persistence (`NFR-3`)**, deterministic Qdrant gRPC indexing, and Google AI Studio Free Tier RPM safety across `CA-11`..`CA-16`. Customer source code is embedded transiently in RAM and fetched on demand via GitHub Blob API — never stored on disk or inside Qdrant payloads.

## Qdrant Collection & Payload Contract (`NFR-3`)

- **Collection:** `codesight_ast_index` (configurable via `QDRANT_COLLECTION_NAME`).
- **Connection:** Host port `6333` (HTTP) / `6334` (gRPC, `prefer_grpc=True`).
- **Vector Config:** `size=768`, `distance=Distance.COSINE` (paired with `output_dimensionality=768`).
- **Deterministic Point ID:** Qdrant rejects arbitrary strings. Always use UUIDv5:
  `str(uuid.uuid5(uuid.NAMESPACE_URL, f"{repo}:{file_path}:{blob_sha}:{symbol_name}:{start_line}"))`
- **Mandatory Keyword Indexes (`PayloadSchemaType.KEYWORD`):** `repo_id`, `repo_full_name`, `file_path`, `symbol_name`, `called_symbols`, `blob_sha`, `chunk_type`.

### Strict Payload Allowlist

| Allowed Key | Type | Constraint |
| :--- | :--- | :--- |
| `repo_id`, `repo_full_name`, `file_path`, `blob_sha` | `str` | Canonical repo UUID & slug; repo-relative POSIX path; Git blob SHA |
| `chunk_type`, `language` | `str` | `chunk_type` in `{"code", "architecture_doc"}`; `language` (`python`, `typescript`, `tsx`, `markdown`) |
| `symbol_name`, `symbol_kind` | `str` | `function`, `method`, `class`, `interface`, `section` |
| `start_line`, `end_line`, `cyclomatic_complexity` | `int` | 1-based inclusive AST node range |
| `signature` | `str` | Header ONLY (strip `block` / `statement_block`), single-line `<= 300` chars |
| `called_symbols`, `imports` | `list[str]` | Outgoing call identifiers and imported module/symbol names |

**Forbidden Payload Keys (`NFR-3` Breach):** `code`, `raw_code`, `source`, `content`, `body`, `text`, `document`, `snippet`, `docstring`. Never install or use generic `mcp-server-qdrant` (`qdrant-store` writes raw `document` text and 384-dim vectors).

## `google-genai` Batched Embedding Pattern

Use `google-genai` (`from google import genai`), never deprecated `google-generativeai`:

```python
import asyncio
from google import genai
from google.genai import types

BATCH_SIZE = 64
RPM_GUARD = asyncio.Semaphore(3)

async def embed_transient_chunks(client: genai.Client, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        async with RPM_GUARD:
            resp = await client.aio.models.embed_content(
                model="gemini-embedding-001",
                contents=batch,
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_DOCUMENT",  # Use RETRIEVAL_QUERY at search time
                    output_dimensionality=768,
                ),
            )
            vectors.extend([emb.values for emb in resp.embeddings])
    return vectors
```

## Live & Fixture Zero-Code Audit

Run before closing any `CA-13`..`CA-16` ticket to prove zero raw code leaked into Qdrant:

```powershell
python .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py --collection codesight_ast_index
python .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py --self-test
```

## Common Mistakes

| Mistake | Fix |
| :--- | :--- |
| Capturing `node.text` into `signature` | Slice only up to the child `block` (Python) or `statement_block` (TS/TSX) and collapse whitespace. |
| Omitting `output_dimensionality=768` on query | `gemini-embedding-001` defaults to `3072` dims; mismatching `768` vs `3072` fails Qdrant search. |
| Writing temp files during repo indexing or PR review | Stream tarballs/blobs in RAM (`io.BytesIO`) or clean ephemeral dirs inside `try ... finally`. |
