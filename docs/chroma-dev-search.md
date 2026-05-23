# Chroma Dev Search

This repo uses Chroma Cloud only as a development aid for Codex and local
codebase retrieval. It is not part of the Kodi addon runtime, search provider
flow, playback resolver, or release artifact.

## Setup

Use Python 3.14 for the dev tooling. The requirement intentionally uses the
full `chromadb` Python SDK because its wheel includes the embedding-function
schema files needed to create a Chroma Cloud collection with Qwen and Splade
indexes.

```bash
python3.14 -m pip install -r requirements-dev-chroma.txt
```

Copy `.env.example` to `.env` and fill in the Chroma values:

```bash
CHROMA_HOST=api.trychroma.com
CHROMA_API_KEY=...
CHROMA_TENANT=...
CHROMA_DATABASE=cdb
CHROMA_COLLECTION=nzbdavkodi_code
```

`.env` is gitignored. Do not put real Chroma keys in committed files.

## Index

Build or refresh the repo index:

```bash
just chroma-install
just chroma-index --reset
```

The indexer uses Chroma Cloud Qwen for dense embeddings and Chroma Cloud Splade
for sparse embeddings. The collection is created with a Schema that enables the
dense vector index and one sparse vector index named `sparse_embedding`.

By default, the indexer:

- walks source and documentation files in the repo;
- skips local secrets, generated zips, caches, virtualenvs, and report folders;
- chunks Python files around complete functions, methods, and class spans;
- injects file, line, and structural context into every document;
- keeps each Chroma document under 16 KiB;
- uses 10% to 20% overlap between contiguous chunks;
- stores `source_doc_id`, `chunk_index`, line span, language, symbol, parent
  class, function/method name, git commit, and content hash metadata.

Run a dry chunking pass without writing to Chroma:

```bash
just chroma-index --dry-run
```

## Search

Search the indexed repo:

```bash
just chroma-search "stream proxy fallback validation"
```

Search uses Chroma's Search API with Reciprocal Rank Fusion over:

- dense semantic search on `#embedding`;
- sparse keyword search on `sparse_embedding`.

The search groups by `source_doc_id` with `MinK(#score, 1)` so results are
deduplicated by source file before they are printed.

## Codex MCP

The local Codex config includes Chroma's `package-search` MCP server. That server
helps with third-party package source lookup. It is separate from this repo
index; use the repo scripts above for `nzbdavkodi` source search.

Restart Codex after MCP config changes so the server is discovered at startup.
