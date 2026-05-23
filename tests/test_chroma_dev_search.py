# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the dev-only Chroma repo search tooling."""

import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import chroma_dev_search
from scripts.chroma_dev_search import (
    SPARSE_EMBEDDING_KEY,
    _normalize_search_argv,
    _sdk_search_object,
    build_hybrid_search_payload,
    build_index_parser,
    build_search_parser,
    chunk_text,
    ensure_chroma_config,
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
            "def func_{0}():\n"
            "    label = 'function {0}'\n"
            "    return label\n".format(index)
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


def test_oversized_single_source_line_is_rejected():
    source = "value = '{}'\n".format("x" * 2000)

    with pytest.raises(ValueError, match="single source line"):
        chunk_text(
            Path("scripts/oversized.py"),
            source,
            max_document_bytes=1000,
            target_document_bytes=700,
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


def test_missing_chroma_config_reads_env_file_and_environment(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("CHROMA_TENANT=file-tenant\n", encoding="utf-8")
    monkeypatch.setenv("CHROMA_API_KEY", "ck-env")

    assert missing_chroma_config_keys(env_path) == []


def test_ensure_chroma_config_prompts_and_appends_missing_values(tmp_path, monkeypatch):
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
    assert loaded["CHROMA_TENANT"] == "tenant-from-user"
    assert loaded["CHROMA_HOST"] == "api.trychroma.com"
    assert loaded["CHROMA_DATABASE"] == "cdb"
    assert loaded["CHROMA_COLLECTION"] == "nzbdavkodi_code"
    assert any("Chroma API key" in prompt for prompt in prompted)
    assert any("Chroma tenant ID" in prompt for prompt in prompted)


def test_ensure_chroma_config_fails_noninteractive_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.delenv("CHROMA_TENANT", raising=False)

    with pytest.raises(SystemExit, match="Missing Chroma configuration"):
        ensure_chroma_config(tmp_path / ".env", prompt=True, interactive=False)
