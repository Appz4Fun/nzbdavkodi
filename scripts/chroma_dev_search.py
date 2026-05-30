#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Dev-only Chroma Cloud indexing and search helpers for Codex context."""

from __future__ import annotations

import argparse
import ast
import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, TypeVar

MAX_DOCUMENT_BYTES = 16 * 1024
TARGET_DOCUMENT_BYTES = 12 * 1024
OVERLAP_FRACTION = 0.15
COLLECTION_GET_PAGE_SIZE = 1000
SPARSE_EMBEDDING_KEY = "sparse_embedding"
T = TypeVar("T")
DEFAULT_COLLECTION_NAME = "nzb"
DEFAULT_CHROMA_HOST = "api.trychroma.com"
DEFAULT_CHROMA_TENANT = "eb3e5a60-028d-4f18-95fd-c9495fb8ddaa"
DEFAULT_CHROMA_DATABASE = "cdb"
CHROMA_ENV_KEYS = (
    "CHROMA_HOST",
    "CHROMA_API_KEY",
    "CHROMA_TENANT",
    "CHROMA_DATABASE",
    "CHROMA_COLLECTION",
)
REQUIRED_CHROMA_ENV_KEYS = ("CHROMA_API_KEY",)
CHROMA_ENV_DEFAULTS = {
    "CHROMA_HOST": DEFAULT_CHROMA_HOST,
    "CHROMA_TENANT": DEFAULT_CHROMA_TENANT,
    "CHROMA_DATABASE": DEFAULT_CHROMA_DATABASE,
    "CHROMA_COLLECTION": DEFAULT_COLLECTION_NAME,
}
CHROMA_ENV_MIGRATIONS = {
    "CHROMA_COLLECTION": {
        "nzbdavkodi_code": DEFAULT_COLLECTION_NAME,
    },
}
AGENT_CHROMA_MCP_SERVERS = ("chroma", "chroma-docs", "package-search")
AGENT_SKILL_PATHS = (
    ("Chroma skill", ".codex/skills/chroma/SKILL.md"),
    ("Superpowers skill symlink", ".agents/skills/superpowers"),
)
AGENT_MCP_INSTALL_HINTS = {
    "chroma": (
        "codex mcp add chroma --env CHROMA_CLIENT_TYPE=cloud "
        '--env CHROMA_HOST="$CHROMA_HOST" '
        '--env CHROMA_API_KEY="$CHROMA_API_KEY" '
        '--env CHROMA_TENANT="$CHROMA_TENANT" '
        '--env CHROMA_DATABASE="$CHROMA_DATABASE" -- '
        "uvx chroma-mcp --client-type cloud"
    ),
    "chroma-docs": (
        "codex mcp add chroma-docs -- " "npx mcp-remote https://docs.trychroma.com/mcp"
    ),
    "package-search": (
        'codex mcp add package-search --env X_CHROMA_TOKEN="$CHROMA_API_KEY" -- '
        "npx mcp-remote https://mcp.trychroma.com/package-search/v1 "
        "--header 'x-chroma-token: ${X_CHROMA_TOKEN}'"
    ),
}

EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv3",
    ".venv314",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "pages-dist",
    "repo/zips",
    "docs/superpowers",
    "docs/reports",
}

EXCLUDED_NAMES = {
    ".DS_Store",
    ".env",
    "CONTEXT.md",
    "uv.lock",
}

TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".po",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

TEXT_NAMES = {
    ".env.example",
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    "justfile",
    "LICENSE",
    "README.md",
}


@dataclass
class SemanticBlock:
    """A logical unit of source text before Chroma document packing."""

    lines: List[str]
    start_line: int
    end_line: int
    kind: str
    parent_class: str = ""
    function_name: str = ""
    method_name: str = ""
    symbol: str = ""
    structural_context: str = ""


@dataclass
class ChromaChunk:
    """A Chroma document plus metadata."""

    chunk_id: str
    document: str
    metadata: Dict[str, object]


def _strip_shell_quotes(value: str) -> str:
    value = value.strip()
    try:
        parts = shlex.split(value, comments=False, posix=True)
    except ValueError:
        parts = []
    if len(parts) == 1:
        return parts[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_env_file(path: Path) -> Dict[str, str]:
    """Read simple KEY=VALUE lines without requiring python-dotenv."""
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_shell_quotes(value)
    return values


def apply_env_file(path: Path) -> None:
    """Load .env values into os.environ without overriding non-empty values."""
    for key, value in load_env_file(path).items():
        if not value:
            continue
        if not os.environ.get(key):
            os.environ[key] = value


def merged_chroma_config(path: Path) -> Dict[str, str]:
    """Return Chroma config from .env plus environment, with safe defaults."""
    values = {
        key: value
        for key, value in load_env_file(path).items()
        if key in CHROMA_ENV_KEYS and value
    }
    for key in CHROMA_ENV_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    for key, value in CHROMA_ENV_DEFAULTS.items():
        values.setdefault(key, value)
    return values


def missing_chroma_config_keys(path: Path) -> List[str]:
    """Return required Chroma config keys absent from both .env and environment."""
    config = merged_chroma_config(path)
    return [key for key in REQUIRED_CHROMA_ENV_KEYS if not config.get(key)]


def _quote_env_value(value: str) -> str:
    return shlex.quote(value)


def _append_chroma_env(path: Path, values: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_env_file(path)
    lines = []
    if path.exists() and path.stat().st_size > 0:
        lines.append("")
    lines.append("# Chroma Cloud dev search")
    for key in CHROMA_ENV_KEYS:
        if key in existing or key not in values:
            continue
        lines.append("{}={}".format(key, _quote_env_value(values[key])))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")
    path.chmod(0o600)


def _rewrite_chroma_env(path: Path, values: Dict[str, str]) -> None:
    lines = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        key = raw_line.split("=", 1)[0].strip() if "=" in raw_line else ""
        if key in values:
            lines.append("{}={}".format(key, _quote_env_value(values[key])))
        else:
            lines.append(raw_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _migrated_chroma_env_values(values: Dict[str, str]) -> Dict[str, str]:
    migrated = {}
    for key, replacements in CHROMA_ENV_MIGRATIONS.items():
        value = values.get(key)
        if value in replacements:
            migrated[key] = replacements[value]
    return migrated


def _prompt_for_required_chroma_values(
    missing: Sequence[str],
    *,
    input_func=input,
    secret_func=getpass.getpass,
) -> Dict[str, str]:
    prompted = {}
    for key in missing:
        if key == "CHROMA_API_KEY":
            value = secret_func("Chroma API key (ask farmfresh, required): ").strip()
        elif key == "CHROMA_TENANT":
            value = input_func("Chroma tenant ID (required): ").strip()
        else:
            value = input_func("{} (required): ".format(key)).strip()
        if not value:
            raise SystemExit(
                "Missing Chroma configuration: {}. Re-run and provide a value.".format(
                    key
                )
            )
        prompted[key] = value
    return prompted


def ensure_chroma_config(
    env_file: Path,
    *,
    prompt: bool = False,
    interactive: Optional[bool] = None,
    input_func=input,
    secret_func=getpass.getpass,
) -> int:
    """Ensure .env/environment contain enough Chroma config for dev tooling."""
    env_file = Path(env_file)
    missing = missing_chroma_config_keys(env_file)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if missing and (not prompt or not interactive):
        raise SystemExit(
            "Missing Chroma configuration: {}. Set these in .env or the shell, "
            "then re-run just make-dev.".format(", ".join(missing))
        )

    prompted = {}
    if missing:
        print("Chroma Cloud dev search needs configuration before indexing/searching.")
        prompted = _prompt_for_required_chroma_values(
            missing,
            input_func=input_func,
            secret_func=secret_func,
        )

    existing_env_file = load_env_file(env_file)
    effective_values = merged_chroma_config(env_file)
    effective_values.update(prompted)
    effective_values.update(_migrated_chroma_env_values(effective_values))
    values_to_rewrite = {}
    values_to_append = {}
    for key in CHROMA_ENV_KEYS:
        value = effective_values.get(key, "")
        if key in existing_env_file and existing_env_file[key]:
            if existing_env_file[key] in CHROMA_ENV_MIGRATIONS.get(key, {}):
                values_to_rewrite[key] = value
            continue
        if not value:
            continue
        if key in existing_env_file:
            values_to_rewrite[key] = value
        else:
            values_to_append[key] = value
    if values_to_rewrite:
        _rewrite_chroma_env(env_file, values_to_rewrite)
    if values_to_append:
        _append_chroma_env(env_file, values_to_append)
    if values_to_rewrite or values_to_append:
        print("Updated {} with Chroma dev search keys.".format(env_file.as_posix()))
    else:
        print("Chroma dev search configuration is present.")
    return 0


def _parse_codex_mcp_names(output: str) -> Set[str]:
    names = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Name "):
            continue
        names.add(line.split(None, 1)[0])
    return names


def _run_codex_mcp_list(codex_path: str):
    return subprocess.run(
        [codex_path, "mcp", "list"],
        check=False,
        capture_output=True,
        text=True,
    )


def check_agent_chroma_setup(
    env_file: Path,
    *,
    home: Optional[Path] = None,
    which_func: Callable[[str], Optional[str]] = shutil.which,
    runner: Callable[[str], object] = _run_codex_mcp_list,
) -> int:
    """Check repo Chroma config plus Codex-side Chroma MCP/skill wiring."""
    env_file = Path(env_file)
    home = Path.home() if home is None else Path(home)
    status = 0

    missing_config = missing_chroma_config_keys(env_file)
    if missing_config:
        print("Chroma config: missing {}".format(", ".join(missing_config)))
        status = 1
    else:
        config = merged_chroma_config(env_file)
        print(
            "Chroma config: present "
            "host={host} tenant={tenant} "
            "database={database} collection={collection}".format(
                host=config["CHROMA_HOST"],
                tenant=config["CHROMA_TENANT"],
                database=config["CHROMA_DATABASE"],
                collection=config["CHROMA_COLLECTION"],
            )
        )

    missing_skills = []
    for label, relative_path in AGENT_SKILL_PATHS:
        path = home / relative_path
        if path.exists():
            print("{}: present".format(label))
        else:
            print("{}: missing ({})".format(label, path.as_posix()))
            missing_skills.append(label)
    if missing_skills:
        status = 1

    codex_path = which_func("codex")
    if not codex_path:
        print("Codex MCP: codex CLI missing; cannot verify MCP servers.")
        return 1

    result = runner(codex_path)
    if getattr(result, "returncode", 1) != 0:
        print("Codex MCP: `codex mcp list` failed.")
        stderr = getattr(result, "stderr", "")
        if stderr:
            print(stderr.strip())
        return 1

    configured_names = _parse_codex_mcp_names(getattr(result, "stdout", ""))
    missing_servers = [
        name for name in AGENT_CHROMA_MCP_SERVERS if name not in configured_names
    ]
    if missing_servers:
        print("MCP servers: missing: {}".format(", ".join(missing_servers)))
        print("Install missing Chroma MCP servers with:")
        for name in missing_servers:
            print("  {}".format(AGENT_MCP_INSTALL_HINTS[name]))
        status = 1
    else:
        print("MCP servers: present")
    return status


def _relative_path(path: Path) -> str:
    return path.as_posix()


def _language_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".rs":
        return "rust"
    if suffix in {".md", ".txt"}:
        return "markdown" if suffix == ".md" else "text"
    if suffix in {".xml", ".html"}:
        return suffix[1:]
    if suffix in {".json", ".toml", ".yaml", ".yml"}:
        return suffix[1:]
    if suffix == ".sh" or path.name == "justfile":
        return "shell"
    return "text"


def _node_start_line(node: ast.AST) -> int:
    starts = [getattr(node, "lineno", 1)]
    decorators = getattr(node, "decorator_list", None) or []
    for decorator in decorators:
        starts.append(getattr(decorator, "lineno", starts[0]))
    return min(starts)


def _node_end_line(node: ast.AST) -> int:
    return getattr(node, "end_lineno", getattr(node, "lineno", 1))


def _line_slice(lines: Sequence[str], start_line: int, end_line: int) -> List[str]:
    return list(lines[start_line - 1 : end_line])


def _is_interesting_top_level(node: ast.AST) -> bool:
    return isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))


def _is_method(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))


def _nonblank_lines(lines: Sequence[str]) -> bool:
    return any(line.strip() for line in lines)


def _context_for_block(
    kind: str,
    *,
    parent_class: str = "",
    function_name: str = "",
    method_name: str = "",
    symbol: str = "",
) -> str:
    if parent_class and method_name:
        return "class {} > method {}".format(parent_class, method_name)
    if parent_class and kind == "class":
        return "class {}".format(parent_class)
    if function_name:
        return "function {}".format(function_name)
    if symbol:
        return symbol
    return kind


def _make_block(
    lines: Sequence[str],
    start_line: int,
    end_line: int,
    kind: str,
    *,
    parent_class: str = "",
    function_name: str = "",
    method_name: str = "",
    symbol: str = "",
) -> SemanticBlock:
    context = _context_for_block(
        kind,
        parent_class=parent_class,
        function_name=function_name,
        method_name=method_name,
        symbol=symbol,
    )
    return SemanticBlock(
        lines=list(lines),
        start_line=start_line,
        end_line=end_line,
        kind=kind,
        parent_class=parent_class,
        function_name=function_name,
        method_name=method_name,
        symbol=symbol or context,
        structural_context=context,
    )


def _module_block(
    lines: Sequence[str], start_line: int, end_line: int
) -> Optional[SemanticBlock]:
    block_lines = _line_slice(lines, start_line, end_line)
    if not _nonblank_lines(block_lines):
        return None
    return _make_block(
        block_lines,
        start_line,
        end_line,
        "module",
        symbol="module lines {}-{}".format(start_line, end_line),
    )


def _class_blocks(node: ast.ClassDef, lines: Sequence[str]) -> List[SemanticBlock]:
    class_start = _node_start_line(node)
    class_end = _node_end_line(node)
    method_nodes = [_node for _node in node.body if _is_method(_node)]
    if not method_nodes:
        return [
            _make_block(
                _line_slice(lines, class_start, class_end),
                class_start,
                class_end,
                "class",
                parent_class=node.name,
                symbol=node.name,
            )
        ]

    blocks = []
    cursor = class_start
    for method in sorted(method_nodes, key=_node_start_line):
        method_start = _node_start_line(method)
        method_end = _node_end_line(method)
        if cursor < method_start:
            preamble = _module_block(lines, cursor, method_start - 1)
            if preamble:
                preamble.kind = "class"
                preamble.parent_class = node.name
                preamble.symbol = node.name
                preamble.structural_context = "class {}".format(node.name)
                blocks.append(preamble)
        method_name = getattr(method, "name", "")
        symbol = "{}.{}".format(node.name, method_name)
        blocks.append(
            _make_block(
                _line_slice(lines, method_start, method_end),
                method_start,
                method_end,
                "method",
                parent_class=node.name,
                function_name=method_name,
                method_name=method_name,
                symbol=symbol,
            )
        )
        cursor = method_end + 1
    if cursor <= class_end:
        tail = _module_block(lines, cursor, class_end)
        if tail:
            tail.kind = "class"
            tail.parent_class = node.name
            tail.symbol = node.name
            tail.structural_context = "class {}".format(node.name)
            blocks.append(tail)
    return blocks


def python_semantic_blocks(text: str) -> List[SemanticBlock]:
    """Split Python source into functions, methods, classes, and module spans."""
    lines = text.splitlines()
    if not lines:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [
            _make_block(
                list(lines),
                1,
                len(lines),
                "module",
                symbol="unparsed python module",
            )
        ]

    blocks = []
    cursor = 1
    interesting_nodes = [node for node in tree.body if _is_interesting_top_level(node)]
    for node in sorted(interesting_nodes, key=_node_start_line):
        start = _node_start_line(node)
        end = _node_end_line(node)
        if cursor < start:
            preamble = _module_block(lines, cursor, start - 1)
            if preamble:
                blocks.append(preamble)
        if isinstance(node, ast.ClassDef):
            blocks.extend(_class_blocks(node, lines))
        else:
            name = getattr(node, "name", "")
            blocks.append(
                _make_block(
                    _line_slice(lines, start, end),
                    start,
                    end,
                    "function",
                    function_name=name,
                    symbol=name,
                )
            )
        cursor = end + 1

    if cursor <= len(lines):
        tail = _module_block(lines, cursor, len(lines))
        if tail:
            blocks.append(tail)
    if not blocks:
        return [_make_block(list(lines), 1, len(lines), "module", symbol="module")]
    return blocks


def line_semantic_blocks(text: str) -> List[SemanticBlock]:
    """Fallback semantic blocks for non-Python text files."""
    lines = text.splitlines()
    if not lines:
        return []
    blocks = []
    start = 1
    current_heading = "module"
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        is_heading = stripped.startswith("#") and stripped.lstrip("#").strip()
        if is_heading and index > start:
            block_lines = _line_slice(lines, start, index - 1)
            if _nonblank_lines(block_lines):
                blocks.append(
                    _make_block(
                        block_lines,
                        start,
                        index - 1,
                        "section",
                        symbol=current_heading,
                    )
                )
            start = index
            current_heading = stripped.lstrip("#").strip()
    block_lines = _line_slice(lines, start, len(lines))
    if _nonblank_lines(block_lines):
        blocks.append(
            _make_block(
                block_lines,
                start,
                len(lines),
                "section",
                symbol=current_heading,
            )
        )
    return blocks


def semantic_blocks_for_path(path: Path, text: str) -> List[SemanticBlock]:
    """Return semantic blocks for a source file."""
    if path.suffix.lower() == ".py":
        return python_semantic_blocks(text)
    return line_semantic_blocks(text)


def _same_nonempty(values: Iterable[str]) -> str:
    normalized = {value for value in values if value}
    if len(normalized) == 1:
        return next(iter(normalized))
    return ""


def _merged_context(blocks: Sequence[SemanticBlock]) -> str:
    contexts = [
        block.structural_context for block in blocks if block.structural_context
    ]
    scoped = [
        block.structural_context
        for block in blocks
        if block.kind in {"function", "method"} and block.structural_context
    ]
    if len(set(scoped)) == 1:
        return scoped[0]
    if len(set(contexts)) == 1:
        return contexts[0]
    if not contexts:
        return "mixed"
    return "multiple semantic blocks: {}".format(", ".join(contexts[:5]))


def _merged_symbol(blocks: Sequence[SemanticBlock]) -> str:
    scoped = [
        block.symbol
        for block in blocks
        if block.kind in {"function", "method"} and block.symbol
    ]
    if len(set(scoped)) == 1:
        return scoped[0]
    symbols = []
    for block in blocks:
        if block.symbol and block.symbol not in symbols:
            symbols.append(block.symbol)
    return ", ".join(symbols[:10])


def _format_document(
    path: Path, start_line: int, end_line: int, context: str, text: str
) -> str:
    header = [
        "File: {}".format(_relative_path(path)),
        "Lines: {}-{}".format(start_line, end_line),
        "Context: {}".format(context or "module"),
        "---",
    ]
    return "\n".join(header + [text.rstrip(), ""])


def _chunk_id(path: Path, chunk_index: int) -> str:
    raw = "{}:{}".format(_relative_path(path), chunk_index)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _metadata_for_blocks(
    path: Path,
    blocks: Sequence[SemanticBlock],
    chunk_index: int,
    document: str,
    git_commit: str,
) -> Dict[str, object]:
    start_line = min(block.start_line for block in blocks)
    end_line = max(block.end_line for block in blocks)
    metadata = {
        "source_doc_id": _relative_path(path),
        "path": _relative_path(path),
        "chunk_index": chunk_index,
        "start_line": start_line,
        "end_line": end_line,
        "language": _language_for_path(path),
        "chunk_kind": blocks[0].kind if len(blocks) == 1 else "mixed",
        "parent_class": _same_nonempty(block.parent_class for block in blocks),
        "function_name": _same_nonempty(block.function_name for block in blocks),
        "method_name": _same_nonempty(block.method_name for block in blocks),
        "symbol": _merged_symbol(blocks),
        "structural_context": _merged_context(blocks),
        "git_commit": git_commit,
        "content_hash": hashlib.sha256(document.encode("utf-8")).hexdigest(),
    }
    return metadata


def _make_chunk(
    path: Path,
    blocks: Sequence[SemanticBlock],
    chunk_index: int,
    git_commit: str,
) -> ChromaChunk:
    start_line = min(block.start_line for block in blocks)
    end_line = max(block.end_line for block in blocks)
    text = "\n".join("\n".join(block.lines).rstrip() for block in blocks if block.lines)
    context = _merged_context(blocks)
    document = _format_document(path, start_line, end_line, context, text)
    metadata = _metadata_for_blocks(path, blocks, chunk_index, document, git_commit)
    return ChromaChunk(
        chunk_id=_chunk_id(path, chunk_index),
        document=document,
        metadata=metadata,
    )


def _block_document_bytes(path: Path, blocks: Sequence[SemanticBlock]) -> int:
    start_line = min(block.start_line for block in blocks)
    end_line = max(block.end_line for block in blocks)
    text = "\n".join("\n".join(block.lines).rstrip() for block in blocks if block.lines)
    return len(
        _format_document(
            path, start_line, end_line, _merged_context(blocks), text
        ).encode("utf-8")
    )


def _line_overlap_count(line_count: int, overlap_fraction: float) -> int:
    if line_count <= 1:
        return 0
    return max(1, int(round(line_count * overlap_fraction)))


def _clone_block_with_lines(
    block: SemanticBlock,
    lines: Sequence[str],
    start_line: int,
) -> SemanticBlock:
    return _make_block(
        lines,
        start_line,
        start_line + len(lines) - 1,
        block.kind,
        parent_class=block.parent_class,
        function_name=block.function_name,
        method_name=block.method_name,
        symbol=block.symbol,
    )


def _split_large_block(
    path: Path,
    block: SemanticBlock,
    next_chunk_index: int,
    git_commit: str,
    max_document_bytes: int,
    overlap_fraction: float,
) -> List[ChromaChunk]:
    chunks = []
    start_offset = 0
    while start_offset < len(block.lines):
        end_offset = start_offset
        candidate_lines = []
        while end_offset < len(block.lines):
            candidate_lines.append(block.lines[end_offset])
            candidate = _make_block(
                candidate_lines,
                block.start_line + start_offset,
                block.start_line + end_offset,
                block.kind,
                parent_class=block.parent_class,
                function_name=block.function_name,
                method_name=block.method_name,
                symbol=block.symbol,
            )
            size = _block_document_bytes(path, [candidate])
            if size > max_document_bytes:
                if len(candidate_lines) == 1:
                    line_number = block.start_line + end_offset
                    raise ValueError(
                        "single source line at {}:{} exceeds max_document_bytes".format(
                            path, line_number
                        )
                    )
                candidate_lines.pop()
                break
            end_offset += 1

        if not candidate_lines:
            candidate_lines = [block.lines[start_offset]]
            end_offset = start_offset + 1
        else:
            end_offset = start_offset + len(candidate_lines)

        chunk_block = _make_block(
            candidate_lines,
            block.start_line + start_offset,
            block.start_line + end_offset - 1,
            block.kind,
            parent_class=block.parent_class,
            function_name=block.function_name,
            method_name=block.method_name,
            symbol=block.symbol,
        )
        chunks.append(_make_chunk(path, [chunk_block], next_chunk_index, git_commit))
        next_chunk_index += 1

        if end_offset >= len(block.lines):
            break
        overlap = _line_overlap_count(len(candidate_lines), overlap_fraction)
        next_start = max(start_offset + 1, end_offset - overlap)
        start_offset = next_start
    return chunks


def _overlap_blocks(
    path: Path,
    blocks: Sequence[SemanticBlock],
    target_document_bytes: int,
    overlap_fraction: float,
) -> List[SemanticBlock]:
    if not blocks:
        return []
    budget = max(1, int(target_document_bytes * overlap_fraction))
    selected = []
    for block in reversed(blocks):
        tentative = [block] + selected
        if _block_document_bytes(path, tentative) <= budget:
            selected = tentative
            continue
        if not selected:
            partial = _tail_overlap_block(path, block, budget)
            if partial is not None:
                selected = [partial]
            break
        break
    return list(selected)


def _tail_overlap_block(
    path: Path,
    block: SemanticBlock,
    budget_bytes: int,
) -> Optional[SemanticBlock]:
    selected_lines = []
    selected_start = block.end_line + 1
    for offset in range(len(block.lines) - 1, -1, -1):
        candidate_lines = [block.lines[offset]] + selected_lines
        candidate_start = block.start_line + offset
        candidate = _clone_block_with_lines(
            block,
            candidate_lines,
            candidate_start,
        )
        if _block_document_bytes(path, [candidate]) > budget_bytes:
            if selected_lines:
                break
            return None
        selected_lines = candidate_lines
        selected_start = candidate_start
    if not selected_lines:
        return None
    return _clone_block_with_lines(block, selected_lines, selected_start)


def _fit_overlap_before_block(
    path: Path,
    overlap: Sequence[SemanticBlock],
    block: SemanticBlock,
    max_document_bytes: int,
) -> List[SemanticBlock]:
    fitted = list(overlap)
    while fitted and _block_document_bytes(path, fitted + [block]) > max_document_bytes:
        if len(fitted) > 1:
            fitted = fitted[1:]
            continue
        trimmed = _tail_overlap_that_fits_with_block(
            path,
            fitted[0],
            block,
            max_document_bytes,
        )
        fitted = [trimmed] if trimmed is not None else []
    return fitted


def _tail_overlap_that_fits_with_block(
    path: Path,
    overlap_block: SemanticBlock,
    block: SemanticBlock,
    max_document_bytes: int,
) -> Optional[SemanticBlock]:
    selected_lines = []
    selected_start = overlap_block.end_line + 1
    for offset in range(len(overlap_block.lines) - 1, -1, -1):
        candidate_lines = [overlap_block.lines[offset]] + selected_lines
        candidate_start = overlap_block.start_line + offset
        candidate = _clone_block_with_lines(
            overlap_block,
            candidate_lines,
            candidate_start,
        )
        if _block_document_bytes(path, [candidate, block]) > max_document_bytes:
            if selected_lines:
                break
            return None
        selected_lines = candidate_lines
        selected_start = candidate_start
    if not selected_lines:
        return None
    return _clone_block_with_lines(overlap_block, selected_lines, selected_start)


def _pack_semantic_blocks(
    path: Path,
    blocks: Sequence[SemanticBlock],
    git_commit: str,
    max_document_bytes: int,
    target_document_bytes: int,
    overlap_fraction: float,
) -> List[ChromaChunk]:
    chunks = []
    current = []

    def flush_current() -> None:
        nonlocal current
        if not current:
            return
        chunks.append(_make_chunk(path, current, len(chunks), git_commit))
        current = _overlap_blocks(
            path, current, target_document_bytes, overlap_fraction
        )

    for block in blocks:
        if _block_document_bytes(path, [block]) > max_document_bytes:
            if current:
                chunks.append(_make_chunk(path, current, len(chunks), git_commit))
                current = []
            chunks.extend(
                _split_large_block(
                    path,
                    block,
                    len(chunks),
                    git_commit,
                    max_document_bytes,
                    overlap_fraction,
                )
            )
            continue

        tentative = current + [block]
        flushed_for_target = False
        if current and _block_document_bytes(path, tentative) > target_document_bytes:
            flush_current()
            flushed_for_target = True
            current = _fit_overlap_before_block(
                path, current, block, max_document_bytes
            )
            tentative = current + [block]
        if _block_document_bytes(path, tentative) > max_document_bytes:
            if not flushed_for_target:
                flush_current()
            current = _fit_overlap_before_block(
                path, current, block, max_document_bytes
            )
            tentative = current + [block]
        current = tentative

    if current:
        chunks.append(_make_chunk(path, current, len(chunks), git_commit))
    return chunks


def chunk_text(
    path: Path,
    text: str,
    *,
    git_commit: str = "",
    max_document_bytes: int = MAX_DOCUMENT_BYTES,
    target_document_bytes: int = TARGET_DOCUMENT_BYTES,
    overlap_fraction: float = OVERLAP_FRACTION,
) -> List[ChromaChunk]:
    """Chunk source text into Chroma-safe documents with structural metadata."""
    if max_document_bytes <= 0:
        raise ValueError("max_document_bytes must be positive")
    if target_document_bytes <= 0:
        raise ValueError("target_document_bytes must be positive")
    target_document_bytes = min(target_document_bytes, max_document_bytes)
    overlap_fraction = max(0.10, min(overlap_fraction, 0.20))
    blocks = semantic_blocks_for_path(path, text)
    return _pack_semantic_blocks(
        path,
        blocks,
        git_commit,
        max_document_bytes,
        target_document_bytes,
        overlap_fraction,
    )


def _is_excluded_repo_path(path: Path) -> bool:
    rel_posix = path.as_posix()
    for excluded in EXCLUDED_DIRS:
        if "/" in excluded:
            if rel_posix == excluded or rel_posix.startswith("{}/".format(excluded)):
                return True
        elif excluded in path.parts:
            return True
    return False


def _is_excluded_dir(root: Path, path: Path) -> bool:
    return _is_excluded_repo_path(path.relative_to(root))


def _is_text_candidate(path: Path) -> bool:
    if path.name in EXCLUDED_NAMES:
        return False
    if path.name in TEXT_NAMES:
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def iter_repo_files(root: Path) -> Iterable[Path]:
    """Yield tracked, indexable repo files."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            "Unable to list tracked files with git ls-files for {}".format(root)
        ) from error

    paths = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        rel = Path(raw_path.decode("utf-8", errors="replace"))
        if rel.is_absolute() or ".." in rel.parts:
            continue
        if _is_excluded_repo_path(rel.parent):
            continue
        if not _is_text_candidate(rel):
            continue
        paths.append(rel)
    yield from sorted(paths)


def read_text_file(path: Path) -> Optional[str]:
    """Read a UTF-8-ish text file, returning None for binary files."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def current_git_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()


def collect_chunks(root: Path) -> List[ChromaChunk]:
    """Collect Chroma chunks for the repository."""
    git_commit = current_git_commit(root)
    chunks = []
    for rel_path in iter_repo_files(root):
        full_path = root / rel_path
        if full_path.is_symlink():
            continue
        text = read_text_file(full_path)
        if text is None:
            continue
        chunks.extend(chunk_text(rel_path, text, git_commit=git_commit))
    return chunks


def build_hybrid_search_payload(
    query: str,
    *,
    limit: int = 10,
    candidates: int = 200,
    group_by_source: bool = True,
) -> Dict[str, object]:
    """Build the raw Search API payload for dense+sparse RRF search."""
    dense_rank = {
        "$knn": {
            "query": query,
            "key": "#embedding",
            "limit": candidates,
            "return_rank": True,
        }
    }
    sparse_rank = {
        "$knn": {
            "query": query,
            "key": SPARSE_EMBEDDING_KEY,
            "limit": candidates,
            "return_rank": True,
        }
    }
    rank = {
        "$mul": [
            {"$val": -1},
            {
                "$sum": [
                    {
                        "$div": {
                            "left": {"$val": 0.65},
                            "right": {"$sum": [{"$val": 60}, dense_rank]},
                        }
                    },
                    {
                        "$div": {
                            "left": {"$val": 0.35},
                            "right": {"$sum": [{"$val": 60}, sparse_rank]},
                        }
                    },
                ]
            },
        ]
    }
    search = {
        "rank": rank,
        "limit": {"limit": limit, "offset": 0},
        "select": {"keys": ["#document", "#metadata", "#score"]},
    }
    if group_by_source:
        search["group_by"] = {
            "keys": ["source_doc_id"],
            "aggregate": {"$min_k": {"keys": ["#score"], "k": 1}},
        }
    return {"searches": [search], "read_level": "index_and_wal"}


def _require_chromadb():
    try:
        import chromadb  # pylint: disable=import-outside-toplevel
    except ImportError as error:
        raise SystemExit(
            "chromadb is required for Chroma dev search. "
            "Install it with: python3.14 -m pip install -r "
            "requirements-dev-chroma.txt"
        ) from error
    return chromadb


def chroma_config_from_env() -> Dict[str, str]:
    required = {
        "host": os.environ.get("CHROMA_HOST") or DEFAULT_CHROMA_HOST,
        "api_key": os.environ.get("CHROMA_API_KEY", ""),
        "tenant": os.environ.get("CHROMA_TENANT") or DEFAULT_CHROMA_TENANT,
        "database": os.environ.get("CHROMA_DATABASE") or DEFAULT_CHROMA_DATABASE,
        "collection": os.environ.get("CHROMA_COLLECTION") or DEFAULT_COLLECTION_NAME,
    }
    missing = [name for name in ("api_key", "tenant", "database") if not required[name]]
    if missing:
        raise SystemExit(
            "Missing Chroma configuration: {}. Set these in .env or the shell.".format(
                ", ".join("CHROMA_{}".format(name.upper()) for name in missing)
            )
        )
    return required


def create_chroma_schema():
    """Create the dense Qwen + sparse Splade collection schema."""
    _require_chromadb()
    from chromadb import K, Schema, SparseVectorIndexConfig, VectorIndexConfig
    from chromadb.utils.embedding_functions import (
        ChromaCloudQwenEmbeddingFunction,
        ChromaCloudSpladeEmbeddingFunction,
    )
    from chromadb.utils.embedding_functions import (
        chroma_cloud_qwen_embedding_function as qwen_module,
    )
    from chromadb.utils.embedding_functions import (
        chroma_cloud_splade_embedding_function as splade_module,
    )

    qwen = ChromaCloudQwenEmbeddingFunction(
        model=qwen_module.ChromaCloudQwenEmbeddingModel.QWEN3_EMBEDDING_0p6B,
        task="nl_to_code",
    )
    splade = ChromaCloudSpladeEmbeddingFunction(
        model=splade_module.ChromaCloudSpladeEmbeddingModel.SPLADE_PP_EN_V1
    )
    return (
        Schema()
        .create_index(
            config=VectorIndexConfig(
                space="cosine",
                source_key=K.DOCUMENT,
                embedding_function=qwen,
            )
        )
        .create_index(
            config=SparseVectorIndexConfig(
                source_key=K.DOCUMENT,
                embedding_function=splade,
            ),
            key=SPARSE_EMBEDDING_KEY,
        )
    )


def chroma_client(config: Dict[str, str]):
    chromadb = _require_chromadb()
    return chromadb.CloudClient(
        cloud_host=config["host"],
        cloud_port=443,
        tenant=config["tenant"],
        database=config["database"],
        api_key=config["api_key"],
    )


def _is_chroma_not_found_error(error: Exception) -> bool:
    if error.__class__.__name__ == "NotFoundError":
        return True
    code = getattr(error, "code", None)
    if callable(code):
        try:
            return code() == 404
        except Exception:  # pylint: disable=broad-except
            return False
    return False


def get_or_create_collection(client, config: Dict[str, str], *, reset: bool = False):
    """Get the configured collection, creating it with the hybrid schema."""
    name = config["collection"]
    if reset:
        try:
            client.delete_collection(name=name)
        except Exception as exc:  # pylint: disable=broad-except
            if not _is_chroma_not_found_error(exc):
                raise
    schema = create_chroma_schema()
    metadata = {
        "project": "nzbdavkodi",
        "purpose": "codex_dev_search",
        "dense_embedding": "Chroma Cloud Qwen Qwen3-Embedding-0.6B",
        "sparse_embedding": "Chroma Cloud Splade PP en v1",
    }
    return client.get_or_create_collection(
        name=name,
        schema=schema,
        metadata=metadata,
    )


def batched(items: Sequence[T], batch_size: int) -> Iterable[Sequence[T]]:
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def _existing_indexed_chunk_ids(collection) -> Set[str]:
    existing_ids = set()
    offset = 0
    while True:
        results = collection.get(
            include=["metadatas"],
            limit=COLLECTION_GET_PAGE_SIZE,
            offset=offset,
        )
        ids = results.get("ids") or []
        metadatas = results.get("metadatas") or []
        for index, row_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            if isinstance(metadata, dict) and metadata.get("source_doc_id"):
                existing_ids.add(row_id)
        if len(ids) < COLLECTION_GET_PAGE_SIZE:
            break
        offset += len(ids)
    return existing_ids


def _delete_stale_indexed_chunks(collection, chunks: Sequence[ChromaChunk]) -> int:
    current_ids = {chunk.chunk_id for chunk in chunks}
    stale_ids = sorted(_existing_indexed_chunk_ids(collection) - current_ids)
    for batch in batched(stale_ids, COLLECTION_GET_PAGE_SIZE):
        collection.delete(ids=list(batch))
    return len(stale_ids)


def index_repo(args: argparse.Namespace) -> int:
    apply_env_file(Path(args.env_file))
    root = Path(args.root).resolve()
    collection_name = os.environ.get("CHROMA_COLLECTION", DEFAULT_COLLECTION_NAME)
    chunks = collect_chunks(root)
    if args.dry_run:
        print(
            "Prepared {} chunks from {} files for collection {!r}".format(
                len(chunks),
                len(list(iter_repo_files(root))),
                collection_name,
            )
        )
        return 0

    config = chroma_config_from_env()
    client = chroma_client(config)
    collection = get_or_create_collection(client, config, reset=args.reset)
    deleted_count = 0
    if not args.reset:
        deleted_count = _delete_stale_indexed_chunks(collection, chunks)
    for batch in batched(chunks, args.batch_size):
        collection.upsert(
            ids=[chunk.chunk_id for chunk in batch],
            documents=[chunk.document for chunk in batch],
            metadatas=[chunk.metadata for chunk in batch],
        )
    if deleted_count:
        print(
            "Deleted {} stale chunks from Chroma collection {!r}".format(
                deleted_count, collection_name
            )
        )
    print(
        "Indexed {} chunks into Chroma collection {!r}".format(
            len(chunks), collection_name
        )
    )
    return 0


def _sdk_search_object(query: str, limit: int, candidates: int, group_by_source: bool):
    from chromadb import K, Knn, Rrf, Search
    from chromadb.execution.expression.operator import GroupBy, MinK

    rank = Rrf(
        ranks=[
            Knn(query=query, key=K.EMBEDDING, return_rank=True, limit=candidates),
            Knn(
                query=query,
                key=SPARSE_EMBEDDING_KEY,
                return_rank=True,
                limit=candidates,
            ),
        ],
        weights=[0.65, 0.35],
        k=60,
    )
    search = Search().rank(rank).limit(limit).select(K.DOCUMENT, K.METADATA, K.SCORE)
    if group_by_source:
        search = search.group_by(
            GroupBy(keys=K("source_doc_id"), aggregate=MinK(keys=K.SCORE, k=1))
        )
    return search


def _rows_from_results(results) -> List[Dict[str, object]]:
    if hasattr(results, "rows"):
        rows = results.rows()
        return list(rows[0]) if rows else []
    ids = (results.get("ids") or [[]])[0]
    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    scores = (results.get("scores") or [[]])[0]
    rows = []
    for index, row_id in enumerate(ids):
        rows.append(
            {
                "id": row_id,
                "document": documents[index] if index < len(documents) else "",
                "metadata": metadatas[index] if index < len(metadatas) else {},
                "score": scores[index] if index < len(scores) else None,
            }
        )
    return rows


def _rows_from_get_results(results) -> List[Dict[str, object]]:
    ids = results.get("ids") or []
    documents = results.get("documents") or []
    metadatas = results.get("metadatas") or []
    rows = []
    for index, row_id in enumerate(ids):
        rows.append(
            {
                "id": row_id,
                "document": documents[index] if index < len(documents) else "",
                "metadata": metadatas[index] if index < len(metadatas) else {},
                "score": None,
            }
        )
    return rows


def _contains_text(value) -> str:
    if isinstance(value, str):
        return value
    return " ".join(str(part) for part in value if str(part)).strip()


def search_repo(args: argparse.Namespace) -> int:
    contains = _contains_text(args.contains)
    if not contains and not args.query:
        raise SystemExit("query is required unless --contains is used")
    apply_env_file(Path(args.env_file))
    config = chroma_config_from_env()
    client = chroma_client(config)
    collection = client.get_collection(name=config["collection"])
    if contains:
        results = collection.get(
            where_document={"$contains": contains},
            include=["documents", "metadatas"],
            limit=args.limit,
        )
        rows = _rows_from_get_results(results)
    else:
        search = _sdk_search_object(
            args.query,
            limit=args.limit,
            candidates=args.candidates,
            group_by_source=not args.no_group_by,
        )
        results = collection.search(search)
        rows = _rows_from_results(results)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for index, row in enumerate(rows, start=1):
        metadata = row.get("metadata") or {}
        path = metadata.get("path", "")
        start_line = metadata.get("start_line", "")
        end_line = metadata.get("end_line", "")
        context = metadata.get("structural_context", "")
        score = row.get("score")
        print(
            "{}. {}:{}-{} score={}".format(
                index,
                path,
                start_line,
                end_line,
                "{:.6f}".format(score) if isinstance(score, float) else score,
            )
        )
        if context:
            print("   {}".format(context))
        document = str(row.get("document") or "").rstrip()
        if document:
            print(document)
            print()
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_index_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index this repo into Chroma Cloud")
    parser.add_argument("--root", default=".", help="Repository root to index")
    parser.add_argument("--env-file", default=".env", help="Local env file")
    parser.add_argument("--batch-size", type=_positive_int, default=64)
    parser.add_argument(
        "--reset", action="store_true", help="Delete/recreate collection first"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Chunk only; do not write to Chroma"
    )
    return parser


def build_search_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Search the Chroma repo index")
    parser.add_argument("query", nargs="?", default=None)
    parser.add_argument("--env-file", default=".env", help="Local env file")
    parser.add_argument("--limit", type=_positive_int, default=10)
    parser.add_argument("--candidates", type=_positive_int, default=200)
    parser.add_argument(
        "--contains",
        nargs="+",
        default=[],
        metavar="TEXT",
        help="Return chunks whose indexed document contains this exact text",
    )
    parser.add_argument("--no-group-by", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _normalize_search_argv(
    argv: Optional[Sequence[str]],
) -> Optional[List[str]]:
    if argv is None:
        return None
    normalized = list(argv)
    try:
        contains_index = normalized.index("--contains")
    except ValueError:
        return normalized
    sentinel_index = contains_index + 1
    if sentinel_index >= len(normalized) or normalized[sentinel_index] != "--":
        return normalized
    return normalized[:sentinel_index] + [
        " ".join(normalized[sentinel_index + 1 :]).strip()
    ]


def build_check_config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Chroma Cloud dev config")
    parser.add_argument("--env-file", default=".env", help="Local env file")
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="Prompt for missing required Chroma values when attached to a TTY",
    )
    return parser


def build_agent_check_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Chroma dev config plus local Codex MCP/skill wiring"
    )
    parser.add_argument("--env-file", default=".env", help="Local env file")
    parser.add_argument(
        "--soft",
        action="store_true",
        help="Print diagnostics but return success so bootstrap can continue",
    )
    return parser


def main_index(argv: Optional[Sequence[str]] = None) -> int:
    return index_repo(build_index_parser().parse_args(argv))


def main_search(argv: Optional[Sequence[str]] = None) -> int:
    return search_repo(build_search_parser().parse_args(_normalize_search_argv(argv)))


def main_check_config(argv: Optional[Sequence[str]] = None) -> int:
    args = build_check_config_parser().parse_args(argv)
    return ensure_chroma_config(Path(args.env_file), prompt=args.prompt)


def main_agent_check(argv: Optional[Sequence[str]] = None) -> int:
    args = build_agent_check_parser().parse_args(argv)
    status = check_agent_chroma_setup(Path(args.env_file))
    return 0 if args.soft else status


if __name__ == "__main__":
    raise SystemExit(main_index(sys.argv[1:]))
