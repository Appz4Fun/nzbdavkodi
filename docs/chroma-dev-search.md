# Chroma Dev Search

This repo uses Chroma Cloud only as a development aid for Codex and local
codebase retrieval. It is not part of the Kodi addon runtime, search provider
flow, playback resolver, or release artifact.

## Setup

Use `just make-dev` for first-time setup. It installs the normal test/lint
dependencies plus the Python 3.14 Chroma dev SDK, verifies `import chromadb`,
then checks `.env` and the current environment for Chroma Cloud configuration.
If the required Chroma values are missing in an interactive shell, it stops and
prompts for them without echoing the API key. In non-interactive shells, it
exits with a clear missing-config message instead of hanging.

```bash
just make-dev
```

For a targeted reinstall of only the Chroma SDK, use:

```bash
just chroma-install
```

The Chroma requirement intentionally uses the full `chromadb` Python SDK because
its wheel includes the embedding-function schema files needed to create a Chroma
Cloud collection with Qwen and Splade indexes.

The checker reads `.env` first and lets exported shell variables override it.
These are the Chroma values it understands:

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

For normal agent development, prefer the native Chroma MCP server first:

```python
mcp__chroma__.chroma_query_documents(
    collection_name="nzbdavkodi_code",
    query_texts=["stream proxy fallback validation"],
    n_results=5,
    include=["documents"],
)
```

Request only the fields needed for the task. Avoid embedding or full metadata
fields unless you are debugging the index itself; sparse embeddings are large
and waste context.

Use the local wrapper when MCP is unavailable, when testing the CLI, or when a
human wants a repeatable shell command:

```bash
just chroma-search "stream proxy fallback validation"
```

Search uses Chroma's Search API with Reciprocal Rank Fusion over:

- dense semantic search on `#embedding`;
- sparse keyword search on `sparse_embedding`.

The search groups by `source_doc_id` with `MinK(#score, 1)` so results are
deduplicated by source file before they are printed.

When you know a literal symbol, error message, or code phrase, use an exact
document filter:

```bash
just chroma-search "argparse positive integer" --contains "must be a positive integer"
```

`--contains` uses Chroma's document filter and is case-sensitive. Use it after a
semantic search when the hybrid ranker found the general area but not the exact
chunk you need.

If the literal itself contains words that look like CLI options, put search
options first and end the command with `--contains --`:

```bash
just chroma-search "make-dev chroma config" --limit 1 --contains -- scripts/chroma_check_config.py --env-file .env --prompt
```

After Chroma identifies likely files and line spans, use `rg` for exact symbols,
strings, and call sites. Chroma is best for conceptual discovery; `rg` is still
cheaper once you know the literal text.

## Normal Development Workflow

Use Chroma as the first map, not as a replacement for reading code:

1. Start with the native MCP server and a conceptual query:

   ```python
   mcp__chroma__.chroma_query_documents(
       collection_name="nzbdavkodi_code",
       query_texts=["resolver WebDAV authentication failure user message"],
       n_results=5,
       include=["documents"],
   )
   ```

2. Read the returned file names, line spans, and structural context. If the
   first result is clearly relevant, inspect that local file span directly.

3. Use `rg` for exact symbols and call sites once Chroma narrows the area:

   ```bash
   rg -n "_handle_webdav_error|auth_failed" repo tests
   ```

4. Use `--contains` or MCP `where_document` filters only for known literals:

   ```bash
   just chroma-search "webdav auth failed" --contains "auth_failed" --limit 3
   ```

5. Refresh after meaningful edits:

   ```bash
   just chroma-index --reset
   ```

Good Chroma queries describe behavior and intent. Good `rg` queries name the
literal function, setting ID, exception text, or test assertion you already
found.

## Codex MCP

The local Codex config includes:

- native Chroma Cloud MCP for this repo's `nzbdavkodi_code` collection;
- `chroma-docs` for Chroma product documentation;
- `package-search` for third-party package source lookup.

Use the repo collection for `nzbdavkodi` source search, `chroma-docs` for Chroma
API behavior, and `package-search` for external package source.

Restart Codex after MCP config changes so the server is discovered at startup.
