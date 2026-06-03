# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the dev-only Chroma repo search tooling."""

import os
import subprocess
import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import chroma_dev_search
from scripts.chroma_dev_search import (
    AGENT_CHROMA_MCP_SERVERS,
    DEFAULT_CHROMA_DATABASE,
    DEFAULT_CHROMA_TENANT,
    DEFAULT_COLLECTION_NAME,
    SPARSE_EMBEDDING_KEY,
    _normalize_search_argv,
    _parse_codex_mcp_names,
    _sdk_search_object,
    apply_env_file,
    build_hybrid_search_payload,
    build_index_parser,
    build_search_parser,
    check_agent_chroma_setup,
    chroma_config_from_env,
    chunk_text,
    ensure_chroma_config,
    iter_repo_files,
    load_env_file,
    missing_chroma_config_keys,
    search_repo,
)


def test_python_chunker_records_and_injects_class_method_context():
    source = """
class MovieResolver:
    def resolve(self, title):
        candidate = title.strip()
        return candidate
""".lstrip()

    chunks = chunk_text(
        Path("repo/plugin.video.nzbdav/resources/lib/resolver.py"),
        source,
        git_commit="abc123",
        max_document_bytes=4096,
        target_document_bytes=2048,
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.metadata["parent_class"] == "MovieResolver"
    assert chunk.metadata["method_name"] == "resolve"
    assert chunk.metadata["function_name"] == "resolve"
    assert chunk.metadata["symbol"] == "MovieResolver.resolve"
    assert (
        chunk.metadata["structural_context"] == "class MovieResolver > method resolve"
    )
    assert (
        chunk.metadata["source_doc_id"]
        == "repo/plugin.video.nzbdav/resources/lib/resolver.py"
    )
    assert chunk.metadata["chunk_index"] == 0
    assert chunk.metadata["git_commit"] == "abc123"
    assert "Context: class MovieResolver > method resolve" in chunk.document
    assert "def resolve" in chunk.document


def test_large_python_function_splits_with_overlap_and_preserves_context():
    body = "\n".join("    value_{0} = {0}".format(index) for index in range(90))
    source = "def build_payload():\n{}\n    return value_89\n".format(body)

    chunks = chunk_text(
        Path("scripts/example.py"),
        source,
        max_document_bytes=900,
        target_document_bytes=700,
        overlap_fraction=0.15,
    )

    assert len(chunks) > 1
    assert all(len(chunk.document.encode("utf-8")) <= 900 for chunk in chunks)
    assert {chunk.metadata["function_name"] for chunk in chunks} == {"build_payload"}
    assert {chunk.metadata["structural_context"] for chunk in chunks} == {
        "function build_payload"
    }
    assert chunks[1].metadata["start_line"] <= chunks[0].metadata["end_line"]
    assert "value_0 = 0" in chunks[0].document
    assert "Context: function build_payload" in chunks[-1].document


def test_semantic_block_packing_overlaps_contiguous_chunks():
    functions = []
    for index in range(8):
        functions.append(
            "def func_{0}():\n    label = 'function {0}'\n    return label\n".format(
                index
            )
        )
    source = "\n".join(functions)

    chunks = chunk_text(
        Path("scripts/packed.py"),
        source,
        max_document_bytes=1200,
        target_document_bytes=420,
        overlap_fraction=0.20,
    )

    assert len(chunks) > 1
    overlapping_functions = []
    for i in range(len(chunks) - 1):
        for index in range(8):
            function_name = "func_{}".format(index)
            if (
                function_name in chunks[i].document
                and function_name in chunks[i + 1].document
            ):
                overlapping_functions.append(function_name)

    assert overlapping_functions


def test_semantic_overlap_never_pushes_chunk_over_hard_limit():
    def source_function(name, line_count):
        body = "\n".join(
            "    value_{0} = '{1}'".format(index, "x" * 20)
            for index in range(line_count)
        )
        return "def {0}():\n{1}\n    return value_{2}\n".format(
            name, body, line_count - 1
        )

    source = source_function("first", 16) + "\n" + source_function("second", 16)

    chunks = chunk_text(
        Path("scripts/packed.py"),
        source,
        max_document_bytes=1000,
        target_document_bytes=700,
        overlap_fraction=0.15,
    )

    assert all(len(chunk.document.encode("utf-8")) <= 1000 for chunk in chunks)


def test_pack_does_not_emit_overlap_tail_as_standalone_chunk(monkeypatch):
    path = Path("scripts/packed.py")
    overlap_block = chroma_dev_search.SemanticBlock(
        lines=["overlap = '{}'".format("x" * 10)],
        start_line=1,
        end_line=1,
        kind="module",
        symbol="overlap",
        structural_context="overlap",
    )
    second_block = chroma_dev_search.SemanticBlock(
        lines=["second = '{}'".format("y" * 160)],
        start_line=2,
        end_line=2,
        kind="module",
        symbol="second",
        structural_context="second",
    )
    original_fit = chroma_dev_search._fit_overlap_before_block
    fit_calls = []

    def flaky_fit(path_arg, overlap, block, max_document_bytes):
        fit_calls.append(None)
        if len(fit_calls) == 1:
            return list(overlap)
        return original_fit(path_arg, overlap, block, max_document_bytes)

    monkeypatch.setattr(chroma_dev_search, "_fit_overlap_before_block", flaky_fit)

    chunks = chroma_dev_search._pack_semantic_blocks(
        path,
        [overlap_block, second_block],
        git_commit="",
        max_document_bytes=250,
        target_document_bytes=200,
        overlap_fraction=0.5,
    )

    assert [chunk.metadata["symbol"] for chunk in chunks] == ["overlap", "second"]


def test_oversized_single_source_line_is_rejected():
    source = "value = '{}'\n".format("x" * 2000)

    with pytest.raises(ValueError, match="single source line"):
        chunk_text(
            Path("scripts/oversized.py"),
            source,
            max_document_bytes=1000,
            target_document_bytes=700,
        )


def test_chunk_ids_stay_stable_when_content_changes():
    original = "def resolve():\n    return 'old'\n"
    changed = "def resolve():\n    return 'new'\n"

    original_chunk = chunk_text(Path("scripts/example.py"), original)[0]
    changed_chunk = chunk_text(Path("scripts/example.py"), changed)[0]

    assert original_chunk.chunk_id == changed_chunk.chunk_id
    assert (
        original_chunk.metadata["content_hash"]
        != changed_chunk.metadata["content_hash"]
    )


def test_cli_parsers_reject_non_positive_counts():
    with pytest.raises(SystemExit):
        build_index_parser().parse_args(["--batch-size", "0"])

    search_parser = build_search_parser()
    with pytest.raises(SystemExit):
        search_parser.parse_args(["resolver", "--limit", "0"])
    with pytest.raises(SystemExit):
        search_parser.parse_args(["resolver", "--candidates", "-1"])


def test_search_parser_accepts_exact_contains_filter():
    args = build_search_parser().parse_args(
        ["resolver fallback", "--contains", "must be a positive integer"]
    )

    assert " ".join(args.contains) == "must be a positive integer"


def test_search_parser_accepts_just_split_contains_filter():
    args = build_search_parser().parse_args(
        [
            "resolver fallback",
            "--contains",
            "must",
            "be",
            "a",
            "positive",
            "integer",
        ]
    )

    assert " ".join(args.contains) == "must be a positive integer"


def test_search_parser_allows_contains_without_dummy_query():
    args = build_search_parser().parse_args(["--contains", "needle"])

    assert args.query is None
    assert args.contains == ["needle"]


def test_search_argv_normalizer_allows_option_like_contains_literals():
    argv = _normalize_search_argv(
        [
            "make dev",
            "--limit",
            "1",
            "--contains",
            "--",
            "scripts/chroma_check_config.py",
            "--env-file",
            ".env",
            "--prompt",
        ]
    )

    args = build_search_parser().parse_args(argv)

    assert args.limit == 1
    assert args.contains == ["scripts/chroma_check_config.py --env-file .env --prompt"]


def test_build_hybrid_search_payload_uses_rrf_sparse_group_by():
    payload = build_hybrid_search_payload(
        "resolver fallback",
        limit=7,
        candidates=111,
        group_by_source=True,
    )

    search = payload["searches"][0]
    rank = search["rank"]
    assert rank["$mul"][0] == {"$val": -1}
    rrf_terms = rank["$mul"][1]["$sum"]
    assert rrf_terms[0]["$div"]["right"]["$sum"][1]["$knn"]["return_rank"]
    sparse_rank = rrf_terms[1]["$div"]["right"]["$sum"][1]["$knn"]
    assert sparse_rank["key"] == "sparse_embedding"
    assert sparse_rank["query"] == "resolver fallback"
    assert sparse_rank["limit"] == 111
    assert search["group_by"]["keys"] == ["source_doc_id"]
    assert search["group_by"]["aggregate"]["$min_k"]["keys"] == ["#score"]
    assert search["limit"] == {"limit": 7, "offset": 0}
    assert set(search["select"]["keys"]) == {"#document", "#metadata", "#score"}


def test_sdk_search_object_uses_installed_chromadb_group_by_api():
    pytest.importorskip("chromadb")

    search = _sdk_search_object(
        "resolver fallback",
        limit=3,
        candidates=17,
        group_by_source=True,
    )

    payload = search.to_dict()
    assert payload["group_by"]["keys"] == ["source_doc_id"]
    assert payload["group_by"]["aggregate"]["$min_k"]["keys"] == ["#score"]
    assert payload["rank"]["$rrf"]["ranks"][1]["$knn"]["key"] == SPARSE_EMBEDDING_KEY


def test_sdk_search_object_imports_group_by_from_sdk_submodule(monkeypatch):
    chromadb = types.ModuleType("chromadb")

    class FakeK:  # pylint: disable=too-few-public-methods
        DOCUMENT = "#document"
        EMBEDDING = "#embedding"
        METADATA = "#metadata"
        SCORE = "#score"

        def __init__(self, name):
            self.name = name

    class FakeKnn:  # pylint: disable=too-few-public-methods
        def __init__(self, query, key, return_rank=False, limit=16):
            self.query = query
            self.key = key
            self.return_rank = return_rank
            self.limit = limit

    class FakeRrf:  # pylint: disable=too-few-public-methods
        def __init__(self, ranks, weights, k):
            self.ranks = ranks
            self.weights = weights
            self.k = k

    class FakeSearch:
        def __init__(self):
            self.group_by_value = None
            self.limit_value = None
            self.rank_value = None
            self.select_value = None

        def rank(self, rank):
            self.rank_value = rank
            return self

        def limit(self, limit):
            self.limit_value = limit
            return self

        def select(self, *keys):
            self.select_value = keys
            return self

        def group_by(self, group_by):
            self.group_by_value = group_by
            return self

    class FakeMinK:  # pylint: disable=too-few-public-methods
        def __init__(self, keys, k):
            self.keys = keys
            self.k = k

    class FakeGroupBy:  # pylint: disable=too-few-public-methods
        def __init__(self, keys, aggregate):
            self.keys = keys
            self.aggregate = aggregate

    chromadb.K = FakeK
    chromadb.Knn = FakeKnn
    chromadb.Rrf = FakeRrf
    chromadb.Search = FakeSearch

    operator = types.ModuleType("chromadb.execution.expression.operator")
    operator.GroupBy = FakeGroupBy
    operator.MinK = FakeMinK

    monkeypatch.setitem(sys.modules, "chromadb", chromadb)
    monkeypatch.setitem(
        sys.modules, "chromadb.execution", types.ModuleType("chromadb.execution")
    )
    monkeypatch.setitem(
        sys.modules,
        "chromadb.execution.expression",
        types.ModuleType("chromadb.execution.expression"),
    )
    monkeypatch.setitem(sys.modules, "chromadb.execution.expression.operator", operator)

    search = _sdk_search_object(
        "resolver fallback",
        limit=3,
        candidates=17,
        group_by_source=True,
    )

    assert isinstance(search.group_by_value, FakeGroupBy)
    assert search.group_by_value.keys.name == "source_doc_id"
    assert isinstance(search.group_by_value.aggregate, FakeMinK)


def test_search_repo_uses_document_contains_filter(monkeypatch, capsys):
    class FakeCollection:  # pylint: disable=too-few-public-methods
        def __init__(self):
            self.get_call = None
            self.search_called = False

        def get(self, **kwargs):
            self.get_call = kwargs
            return {
                "ids": ["chunk-1"],
                "documents": ["File: scripts/example.py\nneedle\n"],
                "metadatas": [
                    {
                        "path": "scripts/example.py",
                        "start_line": 10,
                        "end_line": 12,
                        "structural_context": "function example",
                    }
                ],
            }

        def search(self, _search):
            self.search_called = True
            return {"ids": [[]]}

    class FakeClient:  # pylint: disable=too-few-public-methods
        def __init__(self):
            self.collection = FakeCollection()

        def get_collection(self, name):
            assert name == "repo_code"
            return self.collection

    fake_client = FakeClient()
    monkeypatch.setattr(chroma_dev_search, "apply_env_file", lambda _path: None)
    monkeypatch.setattr(
        chroma_dev_search,
        "chroma_config_from_env",
        lambda: {"collection": "repo_code"},
    )
    monkeypatch.setattr(chroma_dev_search, "chroma_client", lambda _config: fake_client)

    result = search_repo(
        Namespace(
            query="broad query",
            contains="needle",
            env_file=".env",
            limit=5,
            candidates=20,
            no_group_by=False,
            json=False,
        )
    )

    assert result == 0
    assert fake_client.collection.get_call == {
        "where_document": {"$contains": "needle"},
        "include": ["documents", "metadatas"],
        "limit": 5,
    }
    assert fake_client.collection.search_called is False
    output = capsys.readouterr().out
    assert "scripts/example.py:10-12" in output
    assert "function example" in output
    assert "needle" in output


def test_search_repo_requires_query_or_contains_before_chroma_config(monkeypatch):
    monkeypatch.setattr(chroma_dev_search, "apply_env_file", lambda _path: None)
    monkeypatch.setattr(
        chroma_dev_search,
        "chroma_config_from_env",
        lambda: pytest.fail("Chroma config should not be loaded without input"),
    )

    with pytest.raises(SystemExit, match="query is required unless --contains is used"):
        search_repo(
            Namespace(
                query=None,
                contains=[],
                env_file=".env",
                limit=5,
                candidates=20,
                no_group_by=False,
                json=False,
            )
        )


def test_index_repo_deletes_stale_indexed_records_before_upsert(monkeypatch):
    class FakeCollection:  # pylint: disable=too-few-public-methods
        def __init__(self):
            self.calls = []

        def get(self, **kwargs):
            self.calls.append(("get", kwargs))
            return {
                "ids": ["keep-chunk", "stale-chunk", "foreign-chunk"],
                "metadatas": [
                    {"source_doc_id": "tracked.py"},
                    {"source_doc_id": "deleted.py"},
                    {"external": "leave-alone"},
                ],
            }

        def delete(self, ids):
            self.calls.append(("delete", list(ids)))

        def upsert(self, ids, documents, metadatas):
            self.calls.append(("upsert", list(ids), documents, metadatas))

    chunks = [
        chroma_dev_search.ChromaChunk(
            chunk_id="keep-chunk",
            document="File: tracked.py\nold content replaced\n",
            metadata={"source_doc_id": "tracked.py", "path": "tracked.py"},
        ),
        chroma_dev_search.ChromaChunk(
            chunk_id="new-chunk",
            document="File: tracked.py\nnew content\n",
            metadata={"source_doc_id": "tracked.py", "path": "tracked.py"},
        ),
    ]
    collection = FakeCollection()
    monkeypatch.setenv("CHROMA_COLLECTION", "repo_code")
    monkeypatch.setattr(chroma_dev_search, "apply_env_file", lambda _path: None)
    monkeypatch.setattr(chroma_dev_search, "collect_chunks", lambda _root: chunks)
    monkeypatch.setattr(
        chroma_dev_search,
        "chroma_config_from_env",
        lambda: {"collection": "repo_code"},
    )
    monkeypatch.setattr(chroma_dev_search, "chroma_client", lambda _config: object())
    monkeypatch.setattr(
        chroma_dev_search,
        "get_or_create_collection",
        lambda _client, _config, reset=False: collection,
    )

    result = chroma_dev_search.index_repo(
        Namespace(
            env_file=".env",
            root=".",
            dry_run=False,
            reset=False,
            batch_size=64,
        )
    )

    assert result == 0
    assert collection.calls[0] == (
        "get",
        {"include": ["metadatas"], "limit": 1000, "offset": 0},
    )
    assert collection.calls[1] == ("delete", ["stale-chunk"])
    assert collection.calls[2][0] == "upsert"
    assert collection.calls[2][1] == ["keep-chunk", "new-chunk"]


def test_get_or_create_collection_raises_when_reset_delete_fails(monkeypatch):
    class FakeClient:  # pylint: disable=too-few-public-methods
        def delete_collection(self, name):
            raise RuntimeError("auth failed")

        def get_or_create_collection(self, **_kwargs):
            return object()

    def fake_schema():
        return object()

    monkeypatch.setattr(chroma_dev_search, "create_chroma_schema", fake_schema)

    with pytest.raises(RuntimeError, match="auth failed"):
        chroma_dev_search.get_or_create_collection(
            FakeClient(), {"collection": "repo_code"}, reset=True
        )


def test_get_or_create_collection_ignores_missing_collection_on_reset(monkeypatch):
    class NotFoundError(Exception):
        pass

    class FakeClient:  # pylint: disable=too-few-public-methods
        def __init__(self):
            self.calls = []

        def delete_collection(self, name):
            self.calls.append(("delete_collection", name))
            raise NotFoundError("collection missing")

        def get_or_create_collection(self, **kwargs):
            self.calls.append(("get_or_create_collection", kwargs["name"]))
            return "collection"

    client = FakeClient()

    def fake_schema():
        return object()

    monkeypatch.setattr(chroma_dev_search, "create_chroma_schema", fake_schema)

    result = chroma_dev_search.get_or_create_collection(
        client, {"collection": "repo_code"}, reset=True
    )

    assert result == "collection"
    assert client.calls == [
        ("delete_collection", "repo_code"),
        ("get_or_create_collection", "repo_code"),
    ]


def test_load_env_file_reads_chroma_values_without_dotenv(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "CHROMA_HOST=api.trychroma.com",
                "CHROMA_API_KEY='ck-test'",
                'CHROMA_TENANT="tenant-id"',
                "CHROMA_DATABASE=cdb",
                "IGNORED_LINE",
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_env_file(env_path)

    assert loaded["CHROMA_HOST"] == "api.trychroma.com"
    assert loaded["CHROMA_API_KEY"] == "ck-test"
    assert loaded["CHROMA_TENANT"] == "tenant-id"
    assert loaded["CHROMA_DATABASE"] == "cdb"
    assert "IGNORED_LINE" not in loaded


def test_apply_env_file_ignores_empty_values_so_defaults_survive(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "CHROMA_API_KEY=ck-file",
                "CHROMA_TENANT=",
                "CHROMA_DATABASE=",
                "CHROMA_COLLECTION=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for key in chroma_dev_search.CHROMA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    apply_env_file(env_path)

    assert os.environ["CHROMA_API_KEY"] == "ck-file"
    assert "CHROMA_DATABASE" not in os.environ
    assert chroma_config_from_env()["database"] == DEFAULT_CHROMA_DATABASE


def test_apply_env_file_treats_empty_environment_values_as_unset(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("CHROMA_API_KEY=ck-file\n", encoding="utf-8")
    monkeypatch.setenv("CHROMA_API_KEY", "")

    apply_env_file(env_path)

    assert os.environ["CHROMA_API_KEY"] == "ck-file"


def test_chroma_config_from_env_uses_shared_defaults(monkeypatch):
    for key in chroma_dev_search.CHROMA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CHROMA_API_KEY", "ck-env")

    config = chroma_config_from_env()

    assert config["host"] == "api.trychroma.com"
    assert config["tenant"] == DEFAULT_CHROMA_TENANT
    assert config["database"] == DEFAULT_CHROMA_DATABASE
    assert config["collection"] == DEFAULT_COLLECTION_NAME


def test_missing_chroma_config_reads_env_file_and_environment(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("CHROMA_TENANT=file-tenant\n", encoding="utf-8")
    monkeypatch.setenv("CHROMA_API_KEY", "ck-env")

    assert missing_chroma_config_keys(env_path) == []


def test_missing_chroma_config_uses_shared_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_TENANT", raising=False)
    monkeypatch.delenv("CHROMA_COLLECTION", raising=False)

    assert missing_chroma_config_keys(tmp_path / ".env") == ["CHROMA_API_KEY"]


def test_ensure_chroma_config_prompts_for_key_and_appends_shared_defaults(
    tmp_path, monkeypatch
):
    env_path = tmp_path / ".env"
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_TENANT", raising=False)
    prompted = []

    def input_value(prompt):
        prompted.append(prompt)
        return "tenant-from-user"

    def secret_value(prompt):
        prompted.append(prompt)
        return "ck-from-user"

    result = ensure_chroma_config(
        env_path,
        prompt=True,
        interactive=True,
        input_func=input_value,
        secret_func=secret_value,
    )

    assert result == 0
    loaded = load_env_file(env_path)
    assert loaded["CHROMA_API_KEY"] == "ck-from-user"
    assert loaded["CHROMA_TENANT"] == DEFAULT_CHROMA_TENANT
    assert loaded["CHROMA_HOST"] == "api.trychroma.com"
    assert loaded["CHROMA_DATABASE"] == "cdb"
    assert loaded["CHROMA_COLLECTION"] == DEFAULT_COLLECTION_NAME == "nzb"
    assert prompted == ["Chroma API key (ask farmfresh, required): "]


def test_ensure_chroma_config_migrates_legacy_collection_default(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "CHROMA_API_KEY=ck-existing",
                "CHROMA_COLLECTION=nzbdavkodi_code",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("CHROMA_COLLECTION", raising=False)

    result = ensure_chroma_config(env_path, prompt=False, interactive=False)

    assert result == 0
    loaded = load_env_file(env_path)
    assert loaded["CHROMA_API_KEY"] == "ck-existing"
    assert loaded["CHROMA_COLLECTION"] == DEFAULT_COLLECTION_NAME == "nzb"
    assert "nzbdavkodi_code" not in env_path.read_text(encoding="utf-8")


def test_ensure_chroma_config_rewrites_empty_placeholders(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "CHROMA_HOST=",
                "CHROMA_API_KEY=",
                "CHROMA_TENANT=",
                "CHROMA_DATABASE=",
                "CHROMA_COLLECTION=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for key in chroma_dev_search.CHROMA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    result = ensure_chroma_config(
        env_path,
        prompt=True,
        interactive=True,
        secret_func=lambda _prompt: "ck-from-user",
    )

    assert result == 0
    loaded = load_env_file(env_path)
    assert loaded["CHROMA_HOST"] == "api.trychroma.com"
    assert loaded["CHROMA_API_KEY"] == "ck-from-user"
    assert loaded["CHROMA_TENANT"] == DEFAULT_CHROMA_TENANT
    assert loaded["CHROMA_DATABASE"] == DEFAULT_CHROMA_DATABASE
    assert loaded["CHROMA_COLLECTION"] == DEFAULT_COLLECTION_NAME


def test_ensure_chroma_config_rewrites_empty_defaults_without_prompt(
    tmp_path, monkeypatch
):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "CHROMA_API_KEY=ck-existing",
                "CHROMA_TENANT=",
                "CHROMA_DATABASE=",
                "CHROMA_COLLECTION=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for key in chroma_dev_search.CHROMA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    result = ensure_chroma_config(env_path, prompt=False, interactive=False)

    assert result == 0
    loaded = load_env_file(env_path)
    assert loaded["CHROMA_API_KEY"] == "ck-existing"
    assert loaded["CHROMA_TENANT"] == DEFAULT_CHROMA_TENANT
    assert loaded["CHROMA_DATABASE"] == DEFAULT_CHROMA_DATABASE
    assert loaded["CHROMA_COLLECTION"] == DEFAULT_COLLECTION_NAME


def test_ensure_chroma_config_appends_effective_exported_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("CHROMA_API_KEY=ck-existing\n", encoding="utf-8")
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setenv("CHROMA_HOST", "custom.trychroma.test")
    monkeypatch.setenv("CHROMA_TENANT", "tenant-from-env")
    monkeypatch.setenv("CHROMA_DATABASE", "database-from-env")
    monkeypatch.setenv("CHROMA_COLLECTION", "collection-from-env")

    result = ensure_chroma_config(env_path, prompt=False, interactive=False)

    assert result == 0
    loaded = load_env_file(env_path)
    assert loaded["CHROMA_HOST"] == "custom.trychroma.test"
    assert loaded["CHROMA_API_KEY"] == "ck-existing"
    assert loaded["CHROMA_TENANT"] == "tenant-from-env"
    assert loaded["CHROMA_DATABASE"] == "database-from-env"
    assert loaded["CHROMA_COLLECTION"] == "collection-from-env"


def test_ensure_chroma_config_fails_noninteractive_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_TENANT", raising=False)

    with pytest.raises(SystemExit, match="CHROMA_API_KEY"):
        ensure_chroma_config(tmp_path / ".env", prompt=True, interactive=False)


def test_iter_repo_files_limits_indexing_to_git_tracked_files(tmp_path):
    tracked = tmp_path / "tracked.py"
    untracked = tmp_path / "scratch_secret.py"
    tracked.write_text("print('tracked')\n", encoding="utf-8")
    untracked.write_text("SECRET = 'do-not-index'\n", encoding="utf-8")
    subprocess.run(
        ["git", "init"],
        cwd=str(tmp_path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        ["git", "add", "tracked.py"],
        cwd=str(tmp_path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    files = set(iter_repo_files(tmp_path))

    assert Path("tracked.py") in files
    assert Path("scratch_secret.py") not in files


def test_iter_repo_files_excludes_composite_directory_prefixes(tmp_path):
    included = tmp_path / "tracked.py"
    generated_zip_entry = tmp_path / "repo" / "zips" / "generated.txt"
    report_entry = tmp_path / "docs" / "reports" / "report.md"
    included.write_text("print('tracked')\n", encoding="utf-8")
    generated_zip_entry.parent.mkdir(parents=True)
    generated_zip_entry.write_text("generated\n", encoding="utf-8")
    report_entry.parent.mkdir(parents=True)
    report_entry.write_text("# report\n", encoding="utf-8")
    subprocess.run(
        ["git", "init"],
        cwd=str(tmp_path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "add",
            "tracked.py",
            "repo/zips/generated.txt",
            "docs/reports/report.md",
        ],
        cwd=str(tmp_path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    files = set(iter_repo_files(tmp_path))

    assert Path("tracked.py") in files
    assert Path("repo/zips/generated.txt") not in files
    assert Path("docs/reports/report.md") not in files


def test_collect_chunks_skips_tracked_symlinks(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("SECRET = 'do-not-index'\n", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    linked.symlink_to(outside)
    subprocess.run(
        ["git", "init"],
        cwd=str(tmp_path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        ["git", "add", "linked.txt"],
        cwd=str(tmp_path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    chunks = chroma_dev_search.collect_chunks(tmp_path)

    assert not chunks


def test_parse_codex_mcp_names_reads_table_output():
    output = """Name            Command  Args
chroma          uvx      chroma-mcp --client-type cloud
chroma-docs     npx      mcp-remote https://docs.trychroma.com/mcp
package-search  npx      mcp-remote https://mcp.trychroma.com/package-search/v1
"""

    assert _parse_codex_mcp_names(output) == {
        "chroma",
        "chroma-docs",
        "package-search",
    }


def test_check_agent_chroma_setup_reports_missing_mcp_and_skill(
    tmp_path, monkeypatch, capsys
):
    env_path = tmp_path / ".env"
    env_path.write_text("CHROMA_API_KEY=ck-test\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".codex" / "skills" / "chroma").mkdir(parents=True)
    (home / ".codex" / "skills" / "chroma" / "SKILL.md").write_text(
        "# Chroma\n", encoding="utf-8"
    )

    def runner(_cmd):
        return types.SimpleNamespace(
            returncode=0,
            stdout="Name            Command\nchroma          uvx\n",
            stderr="",
        )

    result = check_agent_chroma_setup(
        env_path,
        home=home,
        which_func=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
        runner=runner,
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "collection=nzb" in output
    assert "Chroma skill" in output
    assert "Superpowers skill symlink" in output
    assert "missing: chroma-docs, package-search" in output
    assert "codex mcp add chroma-docs" in output
    assert "codex mcp add package-search" in output
    assert "ck-test" not in output


def test_check_agent_chroma_setup_reports_broken_skill_symlink(
    tmp_path, monkeypatch, capsys
):
    env_path = tmp_path / ".env"
    env_path.write_text("CHROMA_API_KEY=ck-test\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".codex" / "skills" / "chroma").mkdir(parents=True)
    (home / ".codex" / "skills" / "chroma" / "SKILL.md").write_text(
        "# Chroma\n", encoding="utf-8"
    )
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills" / "superpowers").symlink_to(
        home / ".codex" / "superpowers" / "skills"
    )

    def runner(_cmd):
        return types.SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                ["Name            Command"]
                + [
                    "{}          command".format(name)
                    for name in AGENT_CHROMA_MCP_SERVERS
                ]
            ),
            stderr="",
        )

    result = check_agent_chroma_setup(
        env_path,
        home=home,
        which_func=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
        runner=runner,
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "Superpowers skill symlink: missing" in output
    assert "MCP servers: present" in output


def test_check_agent_chroma_setup_passes_when_agent_bits_are_present(
    tmp_path, monkeypatch, capsys
):
    env_path = tmp_path / ".env"
    env_path.write_text("CHROMA_API_KEY=ck-test\n", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".codex" / "skills" / "chroma").mkdir(parents=True)
    (home / ".codex" / "skills" / "chroma" / "SKILL.md").write_text(
        "# Chroma\n", encoding="utf-8"
    )
    (home / ".codex" / "superpowers" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills" / "superpowers").symlink_to(
        home / ".codex" / "superpowers" / "skills"
    )

    def runner(_cmd):
        return types.SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                ["Name            Command"]
                + [
                    "{}          command".format(name)
                    for name in AGENT_CHROMA_MCP_SERVERS
                ]
            ),
            stderr="",
        )

    result = check_agent_chroma_setup(
        env_path,
        home=home,
        which_func=lambda name: "/usr/local/bin/codex" if name == "codex" else None,
        runner=runner,
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "MCP servers: present" in output
