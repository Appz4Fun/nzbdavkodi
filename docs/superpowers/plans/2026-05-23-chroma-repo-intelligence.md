# Chroma Repo Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the repo Chroma tooling from semantic code search into a measured repo-intelligence index that can rank complex code, expose refactor candidates, detect stale indexes, and be reused across repos.

**Architecture:** Keep `scripts/chroma_dev_search.py` as the shared implementation module and keep `scripts/chroma_index_repo.py`, `scripts/chroma_search_repo.py`, and `scripts/chroma_check_config.py` as thin entrypoints. Extend the current function/method chunking model with measured metadata, synthetic file-summary documents, extraction-candidate documents, metadata-filtered search modes, stale-index checks, reusable repo configuration, and an evaluation suite. All new behavior remains dev-only and must not affect Kodi addon runtime code.

**Tech Stack:** Python 3.8-compatible source style for committed repo code, Python 3.14 for Chroma dev execution, Chroma Cloud Qwen/Splade hybrid search, pytest, just, argparse, stdlib `ast`, stdlib `subprocess`.

---

## Current State

- Existing Chroma docs and AGENTS guidance already require Chroma-first repo search.
- `scripts/chroma_dev_search.py` already chunks Python by functions, methods, classes, and module spans.
- Current chunk metadata includes path, line span, language, chunk kind, parent class, function/method name, symbol, structural context, git commit, and content hash.
- Current search supports hybrid dense+sparse search, optional source grouping, exact document `--contains`, and JSON output.
- The checkout currently has uncommitted Chroma-tooling changes. Implementation must re-read `git status --short` before editing and must not revert unrelated work.

## File Structure

Modify:
- `scripts/chroma_dev_search.py` - shared metrics, summaries, candidates, status, search filters, evaluation logic.
- `scripts/chroma_index_repo.py` - keep as thin wrapper unless new subcommands require dispatch.
- `scripts/chroma_search_repo.py` - keep as thin wrapper unless new subcommands require dispatch.
- `scripts/chroma_check_config.py` - optionally reuse config helpers for `chroma-status`.
- `tests/test_chroma_dev_search.py` - unit coverage for all new parsing, metadata, filters, status, and evaluation behavior.
- `justfile` - add or update `chroma-status`, `chroma-eval`, and any reusable-template recipes.
- `docs/chroma-dev-search.md` - document metrics, summaries, query modes, status, evaluation, and cross-repo setup.
- `AGENTS.md` - tighten agent guidance so Chroma metadata search is used for refactor/complexity work.

Create:
- `scripts/chroma_repo_template/README.md` - cross-repo setup recipe.
- `scripts/chroma_repo_template/chroma_repo_config.py` - minimal per-repo configuration template.
- `docs/chroma-eval-queries.json` - committed evaluation query set for this repo.

Do not modify:
- `repo/plugin.video.nzbdav/**` runtime addon code.
- `repo/zips/**`.
- `.env` or any secret-bearing files.

---

### Task 1: Add Measured Code Metrics To Chunk Metadata

**Covers:** improvement 1, "Index metrics as metadata."

**Files:**
- Modify: `scripts/chroma_dev_search.py`
- Test: `tests/test_chroma_dev_search.py`

- [ ] **Step 1: Write failing metric metadata tests**

Add tests that prove Python chunks expose stable numeric metrics.

```python
def test_python_chunker_records_code_metrics_on_function_chunks():
    source = """
def choose(value):
    if value > 10:
        return "large"
    if value > 5:
        return "medium"
    return "small"
""".lstrip()

    chunks = chunk_text(
        Path("repo/plugin.video.nzbdav/resources/lib/example.py"),
        source,
        git_commit="abc123",
    )

    assert len(chunks) == 1
    metadata = chunks[0].metadata
    assert metadata["line_count"] == 6
    assert metadata["nonblank_line_count"] == 6
    assert metadata["word_count"] >= 10
    assert metadata["function_line_count"] == 6
    assert metadata["cyclomatic_complexity"] == 3
    assert metadata["branch_count"] == 2
    assert metadata["loop_count"] == 0
    assert metadata["try_count"] == 0
```

Add a second test for mixed chunks.

```python
def test_mixed_chunk_metadata_records_file_level_counts():
    source = "VALUE = 1\n\nclass Thing:\n    pass\n\ndef run():\n    return VALUE\n"

    chunks = chunk_text(Path("repo/plugin.video.nzbdav/resources/lib/example.py"), source)

    assert chunks
    metadata = chunks[0].metadata
    assert metadata["file_line_count"] == 7
    assert metadata["file_function_count"] == 1
    assert metadata["file_class_count"] == 1
```

- [ ] **Step 2: Run the metric tests and confirm red**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_python_chunker_records_code_metrics_on_function_chunks tests/test_chroma_dev_search.py::test_mixed_chunk_metadata_records_file_level_counts -v
```

Expected: both tests fail with missing metadata keys.

- [ ] **Step 3: Implement AST metric helpers**

Add helpers near the existing AST chunking helpers in `scripts/chroma_dev_search.py`.

```python
@dataclass
class CodeMetrics:
    line_count: int = 0
    nonblank_line_count: int = 0
    word_count: int = 0
    function_line_count: int = 0
    cyclomatic_complexity: int = 1
    branch_count: int = 0
    loop_count: int = 0
    try_count: int = 0
    file_line_count: int = 0
    file_function_count: int = 0
    file_class_count: int = 0
    max_function_lines: int = 0
    max_complexity_in_file: int = 1
```

Add an AST visitor compatible with Python 3.8 syntax.

```python
class _ComplexityVisitor(ast.NodeVisitor):
    def __init__(self):
        self.complexity = 1
        self.branch_count = 0
        self.loop_count = 0
        self.try_count = 0

    def visit_If(self, node):
        self.complexity += 1
        self.branch_count += 1
        self.generic_visit(node)

    def visit_For(self, node):
        self.complexity += 1
        self.loop_count += 1
        self.generic_visit(node)

    def visit_AsyncFor(self, node):
        self.visit_For(node)

    def visit_While(self, node):
        self.complexity += 1
        self.loop_count += 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        self.complexity += 1
        self.try_count += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        self.complexity += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_IfExp(self, node):
        self.complexity += 1
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self.complexity += 1 + len(node.ifs)
        self.generic_visit(node)
```

Extend `SemanticBlock` with a `metrics` field and populate metrics when creating Python blocks. Keep default metrics for non-Python blocks.

- [ ] **Step 4: Attach metrics in `_metadata_for_blocks`**

Aggregate block metrics into metadata.

```python
metadata.update(
    {
        "line_count": end_line - start_line + 1,
        "nonblank_line_count": sum(block.metrics.nonblank_line_count for block in blocks),
        "word_count": sum(block.metrics.word_count for block in blocks),
        "function_line_count": max(block.metrics.function_line_count for block in blocks),
        "cyclomatic_complexity": max(block.metrics.cyclomatic_complexity for block in blocks),
        "branch_count": sum(block.metrics.branch_count for block in blocks),
        "loop_count": sum(block.metrics.loop_count for block in blocks),
        "try_count": sum(block.metrics.try_count for block in blocks),
        "file_line_count": max(block.metrics.file_line_count for block in blocks),
        "file_function_count": max(block.metrics.file_function_count for block in blocks),
        "file_class_count": max(block.metrics.file_class_count for block in blocks),
        "max_function_lines": max(block.metrics.max_function_lines for block in blocks),
        "max_complexity_in_file": max(block.metrics.max_complexity_in_file for block in blocks),
    }
)
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_python_chunker_records_code_metrics_on_function_chunks tests/test_chroma_dev_search.py::test_mixed_chunk_metadata_records_file_level_counts -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add scripts/chroma_dev_search.py tests/test_chroma_dev_search.py
git commit -m "Add Chroma code metrics metadata"
```

Before committing, run `git status --short` and stage only intended files.

---

### Task 2: Add Synthetic File Summary Documents

**Covers:** improvement 2, "Create one file summary document per source file."

**Files:**
- Modify: `scripts/chroma_dev_search.py`
- Test: `tests/test_chroma_dev_search.py`

- [ ] **Step 1: Write failing file-summary tests**

```python
def test_collect_chunks_adds_file_summary_document(tmp_path, monkeypatch):
    repo = tmp_path
    source_path = repo / "repo/plugin.video.nzbdav/resources/lib/example.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        "def alpha():\n    return 1\n\n"
        "def beta(value):\n    if value:\n        return 2\n    return 3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(chroma_dev_search, "current_git_commit", lambda _root: "abc123")

    chunks = collect_chunks(repo)

    summaries = [
        chunk for chunk in chunks
        if chunk.metadata.get("chunk_kind") == "file_summary"
    ]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.metadata["path"] == "repo/plugin.video.nzbdav/resources/lib/example.py"
    assert summary.metadata["symbol"] == "file summary"
    assert summary.metadata["file_function_count"] == 2
    assert "Responsibilities:" in summary.document
    assert "Top symbols: beta, alpha" in summary.document
```

- [ ] **Step 2: Run the test and confirm red**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_collect_chunks_adds_file_summary_document -v
```

Expected: FAIL with no `file_summary` chunks.

- [ ] **Step 3: Implement file summary chunk generation**

Add a helper that builds one synthetic chunk per file after normal chunks.

```python
def _file_summary_chunk(path: Path, chunks: Sequence[ChromaChunk], git_commit: str) -> ChromaChunk:
    metrics = _aggregate_file_chunk_metrics(chunks)
    top_symbols = _top_file_symbols(chunks)
    document = "\n".join(
        [
            "File: {}".format(_relative_path(path)),
            "Context: file summary",
            "---",
            "Lines: {}".format(metrics["file_line_count"]),
            "Functions: {}".format(metrics["file_function_count"]),
            "Classes: {}".format(metrics["file_class_count"]),
            "Max function lines: {}".format(metrics["max_function_lines"]),
            "Max complexity: {}".format(metrics["max_complexity_in_file"]),
            "Top symbols: {}".format(", ".join(top_symbols)),
            "Responsibilities: {}".format(_responsibility_text(path, top_symbols)),
            "",
        ]
    )
    metadata = dict(metrics)
    metadata.update(
        {
            "source_doc_id": _relative_path(path),
            "path": _relative_path(path),
            "chunk_index": -1,
            "start_line": 1,
            "end_line": metrics["file_line_count"],
            "language": _language_for_path(path),
            "chunk_kind": "file_summary",
            "parent_class": "",
            "function_name": "",
            "method_name": "",
            "symbol": "file summary",
            "structural_context": "file summary",
            "git_commit": git_commit,
            "content_hash": hashlib.sha256(document.encode("utf-8")).hexdigest(),
        }
    )
    return ChromaChunk(
        chunk_id=_chunk_id(path, -1, document),
        document=document,
        metadata=metadata,
    )
```

The `_responsibility_text()` helper should be deterministic and local:

```python
def _responsibility_text(path: Path, symbols: Sequence[str]) -> str:
    name = path.name
    if symbols:
        return "{}: {}".format(name, ", ".join(symbols[:5]))
    return name
```

- [ ] **Step 4: Update `collect_chunks`**

For each file, collect normal chunks first, then append the file summary.

```python
file_chunks = chunk_text(rel_path, text, git_commit=git_commit)
chunks.extend(file_chunks)
if file_chunks:
    chunks.append(_file_summary_chunk(rel_path, file_chunks, git_commit))
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_collect_chunks_adds_file_summary_document -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add scripts/chroma_dev_search.py tests/test_chroma_dev_search.py
git commit -m "Index Chroma file summary documents"
```

---

### Task 3: Add Metadata Query Modes To Search

**Covers:** improvement 3, "Add query modes, not just free-text search."

**Files:**
- Modify: `scripts/chroma_dev_search.py`
- Test: `tests/test_chroma_dev_search.py`
- Modify: `docs/chroma-dev-search.md`

- [ ] **Step 1: Write failing parser tests**

```python
def test_search_parser_accepts_metadata_filters():
    args = build_search_parser().parse_args(
        [
            "complex proxy code",
            "--path", "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py",
            "--symbol", "prepare_stream",
            "--kind", "function",
            "--language", "python",
            "--min-lines", "100",
            "--min-complexity", "20",
            "--top-complexity",
            "--file-summary",
        ]
    )

    assert args.path == "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"
    assert args.symbol == "prepare_stream"
    assert args.kind == "function"
    assert args.language == "python"
    assert args.min_lines == 100
    assert args.min_complexity == 20
    assert args.top_complexity is True
    assert args.file_summary is True
```

- [ ] **Step 2: Write failing metadata-filter construction test**

```python
def test_search_repo_applies_metadata_filters(monkeypatch):
    class FakeCollection:
        def __init__(self):
            self.get_call = None

        def get(self, **kwargs):
            self.get_call = kwargs
            return {"ids": [], "documents": [], "metadatas": []}

    class FakeClient:
        def __init__(self):
            self.collection = FakeCollection()

        def get_collection(self, name):
            return self.collection

    fake_client = FakeClient()
    monkeypatch.setattr(chroma_dev_search, "apply_env_file", lambda _path: None)
    monkeypatch.setattr(chroma_dev_search, "chroma_config_from_env", lambda: {"collection": "repo_code"})
    monkeypatch.setattr(chroma_dev_search, "chroma_client", lambda _config: fake_client)

    result = search_repo(
        Namespace(
            query="ignored",
            contains=[],
            env_file=".env",
            limit=10,
            candidates=20,
            no_group_by=False,
            json=True,
            path="repo/plugin.video.nzbdav/resources/lib/stream_proxy.py",
            symbol="prepare_stream",
            kind="function",
            language="python",
            min_lines=100,
            min_complexity=20,
            top_complexity=True,
            file_summary=False,
        )
    )

    assert result == 0
    assert fake_client.collection.get_call["where"] == {
        "$and": [
            {"path": {"$eq": "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"}},
            {"symbol": {"$eq": "prepare_stream"}},
            {"chunk_kind": {"$eq": "function"}},
            {"language": {"$eq": "python"}},
            {"line_count": {"$gte": 100}},
            {"cyclomatic_complexity": {"$gte": 20}},
        ]
    }
```

- [ ] **Step 3: Implement parser flags**

Add to `build_search_parser()`:

```python
parser.add_argument("--path", default="", help="Filter indexed chunks by exact repo path")
parser.add_argument("--symbol", default="", help="Filter indexed chunks by exact symbol")
parser.add_argument("--kind", default="", help="Filter by chunk_kind metadata")
parser.add_argument("--language", default="", help="Filter by indexed language")
parser.add_argument("--min-lines", type=_positive_int, default=0)
parser.add_argument("--min-complexity", type=_positive_int, default=0)
parser.add_argument("--top-complexity", action="store_true")
parser.add_argument("--file-summary", action="store_true")
```

- [ ] **Step 4: Implement metadata filter builder**

```python
def _metadata_filter_from_args(args: argparse.Namespace) -> Dict[str, object]:
    clauses = []
    if args.path:
        clauses.append({"path": {"$eq": args.path}})
    if args.symbol:
        clauses.append({"symbol": {"$eq": args.symbol}})
    if args.kind:
        clauses.append({"chunk_kind": {"$eq": args.kind}})
    if args.language:
        clauses.append({"language": {"$eq": args.language}})
    if args.file_summary:
        clauses.append({"chunk_kind": {"$eq": "file_summary"}})
    if args.min_lines:
        clauses.append({"line_count": {"$gte": args.min_lines}})
    if args.min_complexity:
        clauses.append({"cyclomatic_complexity": {"$gte": args.min_complexity}})
    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}
```

- [ ] **Step 5: Route filtered searches through `collection.get`**

When metadata filters or `--top-complexity` are present, use `collection.get(where=..., include=["documents", "metadatas"], limit=...)`. Sort rows locally by `cyclomatic_complexity`, then `line_count`, when `--top-complexity` is true.

- [ ] **Step 6: Document query modes**

Add examples to `docs/chroma-dev-search.md`:

```bash
just chroma-search "large proxy functions" --min-complexity 20 --top-complexity --json
just chroma-search "file summaries" --file-summary --limit 20
just chroma-search "resolver submit flow" --path repo/plugin.video.nzbdav/resources/lib/resolver.py --min-lines 100
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_search_parser_accepts_metadata_filters tests/test_chroma_dev_search.py::test_search_repo_applies_metadata_filters -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run:

```bash
git add scripts/chroma_dev_search.py tests/test_chroma_dev_search.py docs/chroma-dev-search.md
git commit -m "Add Chroma metadata search modes"
```

---

### Task 4: Index Refactor Extraction Candidate Documents

**Covers:** improvement 4, "Index refactor candidates explicitly."

**Files:**
- Modify: `scripts/chroma_dev_search.py`
- Test: `tests/test_chroma_dev_search.py`
- Modify: `docs/chroma-dev-search.md`

- [ ] **Step 1: Write failing extraction-candidate test**

```python
def test_large_complex_function_adds_refactor_candidate_document():
    source = """
def prepare_stream(value):
    result = []
    if value > 0:
        result.append("positive")
    if value > 10:
        result.append("large")
    for item in result:
        if item:
            result.append(item.upper())
    try:
        return ",".join(result)
    except TypeError:
        return ""
""".lstrip()

    chunks = chunk_text(
        Path("repo/plugin.video.nzbdav/resources/lib/stream_proxy.py"),
        source,
        max_document_bytes=4096,
        target_document_bytes=2048,
    )

    candidates = [
        chunk for chunk in chunks
        if chunk.metadata.get("chunk_kind") == "refactor_candidate"
    ]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.metadata["symbol"] == "prepare_stream"
    assert candidate.metadata["refactor_reason"] == "large_complex_function"
    assert "Suggested extraction:" in candidate.document
```

- [ ] **Step 2: Run the test and confirm red**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_large_complex_function_adds_refactor_candidate_document -v
```

Expected: FAIL with no `refactor_candidate` chunks.

- [ ] **Step 3: Implement candidate generation**

Add `_refactor_candidate_chunks()` after normal chunk packing. Generate candidates only for Python functions/methods where either `function_line_count >= 80` or `cyclomatic_complexity >= 15`.

Document format:

```text
File: repo/plugin.video.nzbdav/resources/lib/stream_proxy.py
Lines: 6267-6705
Context: refactor candidate prepare_stream
---
Symbol: prepare_stream
Reason: large_complex_function
Lines: 439
Cyclomatic complexity: 49
Suggested extraction: split IO setup, mode selection, session context creation, and response metadata assembly into helpers.
```

Metadata fields:

```python
{
    "chunk_kind": "refactor_candidate",
    "refactor_reason": "large_complex_function",
    "candidate_line_count": block.metrics.function_line_count,
    "candidate_complexity": block.metrics.cyclomatic_complexity,
}
```

- [ ] **Step 4: Append candidates from `chunk_text`**

Return normal chunks plus candidate chunks. Use chunk indexes below `-1000` or a stable suffix in `_chunk_id` so candidate IDs do not collide with normal chunks.

- [ ] **Step 5: Add search docs**

Document:

```bash
just chroma-search "refactor candidate stream proxy" --kind refactor_candidate --top-complexity
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_large_complex_function_adds_refactor_candidate_document -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add scripts/chroma_dev_search.py tests/test_chroma_dev_search.py docs/chroma-dev-search.md
git commit -m "Index Chroma refactor candidates"
```

---

### Task 5: Add Stale Index Status Guard

**Covers:** improvement 5, "Add a stale-index guard."

**Files:**
- Modify: `scripts/chroma_dev_search.py`
- Modify: `justfile`
- Test: `tests/test_chroma_dev_search.py`
- Modify: `docs/chroma-dev-search.md`

- [ ] **Step 1: Write failing status parser test**

```python
def test_status_parser_exists():
    args = build_status_parser().parse_args(["--env-file", ".env"])

    assert args.env_file == ".env"
```

- [ ] **Step 2: Write failing status comparison test**

```python
def test_chroma_status_reports_stale_commit(monkeypatch, capsys):
    class FakeCollection:
        metadata = {"git_commit": "old123"}

        def get(self, **_kwargs):
            return {
                "ids": ["summary"],
                "documents": [""],
                "metadatas": [{"git_commit": "old123", "path": "README.md"}],
            }

    class FakeClient:
        def get_collection(self, name):
            assert name == "repo_code"
            return FakeCollection()

    monkeypatch.setattr(chroma_dev_search, "apply_env_file", lambda _path: None)
    monkeypatch.setattr(chroma_dev_search, "chroma_config_from_env", lambda: {"collection": "repo_code"})
    monkeypatch.setattr(chroma_dev_search, "chroma_client", lambda _config: FakeClient())
    monkeypatch.setattr(chroma_dev_search, "current_git_commit", lambda _root: "new456")
    monkeypatch.setattr(chroma_dev_search, "_git_dirty_indexable_paths", lambda _root: [])

    result = status_repo(Namespace(env_file=".env", root="."))

    assert result == 1
    assert "stale" in capsys.readouterr().out.lower()
```

- [ ] **Step 3: Implement `status_repo`**

Status should:
- load Chroma config;
- read collection metadata and/or one indexed summary/chunk metadata;
- compare indexed `git_commit` to local `git rev-parse HEAD`;
- list dirty indexable paths from `git status --porcelain`;
- return `0` when current and clean, `1` when stale or dirty, `2` for config/connection failure.

- [ ] **Step 4: Add `just chroma-status`**

Add to `justfile`:

```make
# Report whether the Chroma repo index matches HEAD and indexed files are clean.
chroma-status *args:
    #!/usr/bin/env bash
    set -euo pipefail
    "${CHROMA_PYTHON:-python3.14}" scripts/chroma_dev_search.py status {{args}}
```

If the script entrypoint does not support subcommands yet, add `scripts/chroma_status_repo.py` as a thin wrapper instead and call it from `justfile`.

- [ ] **Step 5: Document status checks**

Add:

```bash
just chroma-status
just chroma-index --reset
```

State that agents should run `just chroma-status` before relying on metrics-heavy Chroma answers.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_status_parser_exists tests/test_chroma_dev_search.py::test_chroma_status_reports_stale_commit -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add scripts/chroma_dev_search.py tests/test_chroma_dev_search.py justfile docs/chroma-dev-search.md
git commit -m "Add Chroma index status guard"
```

---

### Task 6: Extract Cross-Repo Chroma Template

**Covers:** improvement 6, "Make it reusable across repos."

**Files:**
- Create: `scripts/chroma_repo_template/README.md`
- Create: `scripts/chroma_repo_template/chroma_repo_config.py`
- Modify: `scripts/chroma_dev_search.py`
- Test: `tests/test_chroma_dev_search.py`
- Modify: `docs/chroma-dev-search.md`

- [ ] **Step 1: Write failing config override test**

```python
def test_repo_config_can_override_collection_and_excludes(tmp_path, monkeypatch):
    config_path = tmp_path / "chroma_repo_config.py"
    config_path.write_text(
        "COLLECTION_NAME = 'example_code'\n"
        "EXCLUDED_DIRS = {'vendor'}\n"
        "TEXT_SUFFIXES = {'.py', '.md'}\n",
        encoding="utf-8",
    )

    config = load_repo_config(config_path)

    assert config.collection_name == "example_code"
    assert "vendor" in config.excluded_dirs
    assert ".py" in config.text_suffixes
```

- [ ] **Step 2: Implement `RepoChromaConfig` and loader**

Add:

```python
@dataclass
class RepoChromaConfig:
    collection_name: str = DEFAULT_COLLECTION_NAME
    excluded_dirs: Sequence[str] = tuple(sorted(EXCLUDED_DIRS))
    excluded_names: Sequence[str] = tuple(sorted(EXCLUDED_NAMES))
    text_suffixes: Sequence[str] = tuple(sorted(TEXT_SUFFIXES))
    text_names: Sequence[str] = tuple(sorted(TEXT_NAMES))
```

Add `load_repo_config(path: Path) -> RepoChromaConfig` using `runpy.run_path()` and only reading uppercase known keys.

- [ ] **Step 3: Wire config into file iteration and collection defaults**

Allow index/search/status parsers to accept:

```bash
--repo-config scripts/chroma_repo_config.py
```

Use config values for collection default, excludes, and suffixes. Environment variables still override the collection name.

- [ ] **Step 4: Add template README**

Write `scripts/chroma_repo_template/README.md` with:

```markdown
# Chroma Repo Template

Copy `chroma_repo_config.py` into a repository and set:

- `COLLECTION_NAME`
- `EXCLUDED_DIRS`
- `EXCLUDED_NAMES`
- `TEXT_SUFFIXES`
- `TEXT_NAMES`

Then add just recipes for `chroma-install`, `chroma-index`, `chroma-search`, `chroma-status`, and `chroma-eval`.
```

- [ ] **Step 5: Add template config**

Write `scripts/chroma_repo_template/chroma_repo_config.py`:

```python
COLLECTION_NAME = "repo_code"
EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "vendor",
}
EXCLUDED_NAMES = {".DS_Store", ".env", "uv.lock"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".sh", ".txt"}
TEXT_NAMES = {"AGENTS.md", "README.md", "justfile"}
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_repo_config_can_override_collection_and_excludes -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run:

```bash
git add scripts/chroma_dev_search.py tests/test_chroma_dev_search.py docs/chroma-dev-search.md scripts/chroma_repo_template
git commit -m "Add reusable Chroma repo template"
```

---

### Task 7: Add Chroma Evaluation Suite

**Covers:** improvement 7, "Add an evaluation suite."

**Files:**
- Create: `docs/chroma-eval-queries.json`
- Modify: `scripts/chroma_dev_search.py`
- Modify: `justfile`
- Test: `tests/test_chroma_dev_search.py`
- Modify: `docs/chroma-dev-search.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Create evaluation query file**

Create `docs/chroma-eval-queries.json`:

```json
[
  {
    "query": "fallback stream cutover validation",
    "expected_path": "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py",
    "expected_symbol": "_select_live_fallback_source"
  },
  {
    "query": "resolver submit timeout user interface pump",
    "expected_path": "repo/plugin.video.nzbdav/resources/lib/resolver.py",
    "expected_symbol": "_submit_nzb_with_ui_pump"
  },
  {
    "query": "results dialog scrollbar linked to list",
    "expected_path": "repo/plugin.video.nzbdav/resources/skins/Default/1080i/results-dialog.xml",
    "expected_contains": "control type=\"scrollbar\" id=\"60\""
  },
  {
    "query": "Chroma metadata filter exact contains",
    "expected_path": "scripts/chroma_dev_search.py",
    "expected_symbol": "search_repo"
  }
]
```

- [ ] **Step 2: Write failing eval parser test**

```python
def test_eval_parser_accepts_query_file():
    args = build_eval_parser().parse_args(["--queries", "docs/chroma-eval-queries.json"])

    assert args.queries == "docs/chroma-eval-queries.json"
```

- [ ] **Step 3: Write failing eval runner test with fake search**

```python
def test_eval_repo_reports_passes_and_failures(tmp_path, monkeypatch, capsys):
    query_file = tmp_path / "queries.json"
    query_file.write_text(
        json.dumps(
            [
                {
                    "query": "fallback stream cutover validation",
                    "expected_path": "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py",
                    "expected_symbol": "_select_live_fallback_source",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        chroma_dev_search,
        "_run_eval_query",
        lambda _collection, _query, _limit: [
            {
                "metadata": {
                    "path": "repo/plugin.video.nzbdav/resources/lib/stream_proxy.py",
                    "symbol": "_select_live_fallback_source",
                },
                "document": "",
            }
        ],
    )

    result = eval_repo(Namespace(env_file=".env", queries=str(query_file), limit=5, json=False))

    assert result == 0
    assert "PASS" in capsys.readouterr().out
```

- [ ] **Step 4: Implement eval runner**

Add:
- `build_eval_parser()`
- `_load_eval_queries(path)`
- `_eval_query_matches(row, expectation)`
- `eval_repo(args)`

Return `0` only when every expected query matches within the top `limit`; return `1` on misses.

- [ ] **Step 5: Add `just chroma-eval`**

Add to `justfile`:

```make
# Evaluate Chroma search quality against known repo queries.
chroma-eval *args:
    #!/usr/bin/env bash
    set -euo pipefail
    "${CHROMA_PYTHON:-python3.14}" scripts/chroma_dev_search.py eval --queries docs/chroma-eval-queries.json {{args}}
```

If using wrapper scripts instead of subcommands, create `scripts/chroma_eval_repo.py` and call that wrapper.

- [ ] **Step 6: Document evaluation workflow**

Add:

```bash
just chroma-index --reset
just chroma-eval
```

State that indexer/chunking changes should pass eval before merge.

- [ ] **Step 7: Update `AGENTS.md`**

Add a short Chroma guidance line:

```markdown
- For refactor, complexity, or large-file questions, use Chroma metadata search first (`--file-summary`, `--min-complexity`, `--top-complexity`) and then verify with local exact tools.
```

- [ ] **Step 8: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_chroma_dev_search.py::test_eval_parser_accepts_query_file tests/test_chroma_dev_search.py::test_eval_repo_reports_passes_and_failures -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
git add scripts/chroma_dev_search.py tests/test_chroma_dev_search.py docs/chroma-dev-search.md docs/chroma-eval-queries.json justfile AGENTS.md
git commit -m "Add Chroma search evaluation suite"
```

---

## Final Verification

- [ ] **Run unit tests for Chroma tooling**

```bash
python3 -m pytest tests/test_chroma_dev_search.py -v
```

Expected: all tests pass.

- [ ] **Run lint/format checks**

```bash
just lint
```

Expected: ruff, black, and pylint pass.

- [ ] **Dry-run chunking**

```bash
just chroma-index --dry-run
```

Expected: prints prepared chunk count; no Chroma writes.

- [ ] **Refresh index**

```bash
just chroma-index --reset
```

Expected: indexes normal chunks, file summaries, and refactor candidates.

- [ ] **Check index status**

```bash
just chroma-status
```

Expected: reports indexed commit matching `HEAD` and no dirty indexed paths.

- [ ] **Run Chroma eval**

```bash
just chroma-eval
```

Expected: all committed eval queries pass.

- [ ] **Manual smoke queries**

```bash
just chroma-search "complex stream proxy functions" --min-complexity 20 --top-complexity --limit 5
just chroma-search "file summaries" --file-summary --limit 10
just chroma-search "fallback cutover refactor candidate" --kind refactor_candidate --top-complexity --limit 5
```

Expected: results include measured metadata and useful file/symbol context.

---

## Implementation Notes

- Keep all Chroma tooling dev-only. Do not import Chroma SDK from runtime addon code.
- Preserve Python 3.8-compatible syntax in committed scripts even though Chroma commands run under Python 3.14.
- Avoid printing `.env` values, Chroma API keys, or raw config values.
- For Codacy-style planning, use `cyclomatic_complexity`, `line_count`, `file_line_count`, and `max_complexity_in_file` metadata as ranking inputs, then verify with local exact tools.
- The first implementation pass should prefer one commit per task. If the working tree starts dirty, stage only the files listed in each task.

## Execution Choice

Plan complete. Recommended execution is **Subagent-Driven** because the seven tasks are mostly independent and each has focused tests. Inline execution is also reasonable if the current dirty Chroma-tooling changes need to be preserved and reviewed continuously in one session.
