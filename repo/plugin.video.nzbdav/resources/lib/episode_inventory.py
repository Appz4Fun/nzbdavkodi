# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Pure episode-aware video selection and pack classification helpers."""

import os
import re
from typing import NamedTuple, Optional

from resources.lib.webdav_match import _resolve_file_episode_tags

_AUXILIARY_RE = re.compile(
    r"(?:^|[. _-])(samples?|trailers?|featurettes?|extras?)(?:[. _-]|$)",
    re.IGNORECASE,
)
_AUXILIARY_TOKEN_RE = re.compile(
    r"(?:samples?|trailers?|featurettes?|extras?)", re.IGNORECASE
)
_AMBIGUOUS_LEADING_MARKERS = frozenset(("extra", "extras", "trailer", "trailers"))


class VideoFile(NamedTuple):
    path: str
    size: int
    episode_tags: frozenset
    auxiliary: bool


class VideoInventory(NamedTuple):
    selected_path: Optional[str]
    selected_size: int
    files: tuple
    pack_season: Optional[int]
    episodes: tuple
    has_tagged_files: bool


def _coerce_size(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _is_auxiliary(name, parent):
    """Classify conservative filename and directory auxiliary markers."""
    parent_names = tuple(item for item in re.split(r"[/\\]+", parent) if item)
    auxiliary_parents = tuple(
        item for item in parent_names if _AUXILIARY_TOKEN_RE.fullmatch(item)
    )
    leading_name = re.split(r"[. _-]+", name, maxsplit=1)[0]
    different_auxiliary_parent = any(
        leading_name.casefold() != item.casefold() for item in auxiliary_parents
    )
    show_folder_exception = bool(auxiliary_parents) and not different_auxiliary_parent
    if different_auxiliary_parent:
        return True

    stem = os.path.splitext(name)[0]
    if not show_folder_exception and _AUXILIARY_TOKEN_RE.fullmatch(stem):
        return True

    return any(
        _resolve_file_episode_tags(name[: match.start()], "")
        for match in _AUXILIARY_RE.finditer(name)
    )


def _video_file(path, size):
    name = os.path.basename(path)
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    return VideoFile(
        path=path,
        size=_coerce_size(size),
        episode_tags=_resolve_file_episode_tags(name, parent),
        auxiliary=_is_auxiliary(name, parent),
    )


def _largest(files):
    return max(files, key=lambda item: item.size) if files else None


def _leading_auxiliary_context(path):
    """Return ``(marker, has_title_prefix)`` for a parseable leading marker.

    Prefixes are parsed incrementally by the authoritative episode parser. Two
    tokens before the first recognized episode are conservative title evidence
    (``Trailer.Park.Boys.S01E01``); a direct marker remains auxiliary.
    """
    name = os.path.basename(path)
    match = _AUXILIARY_RE.match(name)
    if not match:
        return None
    remainder = name[match.end() :].lstrip(". _-")
    parts = tuple(item for item in re.split(r"[. _-]+", remainder) if item)
    for count in range(1, len(parts) + 1):
        probe = ".".join(parts[:count])
        if _resolve_file_episode_tags(probe, ""):
            return (match.group(1).casefold(), count >= 3)
    return None


def _release_folder_matches_marker(path, marker):
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    for segment in (item for item in re.split(r"[/\\]+", parent) if item):
        leading_segment = re.split(r"[. _-]+", segment, maxsplit=1)[0]
        if leading_segment.casefold() == marker:
            return True
    return False


def _leading_group_is_show(files, indexes, marker):
    if marker not in _AMBIGUOUS_LEADING_MARKERS or len(indexes) < 2:
        return False
    episode_tags = {tag for index in indexes for tag in files[index].episode_tags}
    return len(episode_tags) >= 2


def _classify_leading_auxiliary(files):
    candidates = {}
    groups = {}
    main_indexes = set()
    for index, item in enumerate(files):
        if item.auxiliary:
            continue
        context = _leading_auxiliary_context(item.path)
        if not context:
            continue
        marker, has_title_prefix = context
        candidates[index] = marker
        groups.setdefault(marker, []).append(index)
        if marker in _AMBIGUOUS_LEADING_MARKERS and (
            has_title_prefix or _release_folder_matches_marker(item.path, marker)
        ):
            main_indexes.add(index)

    for marker, indexes in groups.items():
        if _leading_group_is_show(files, indexes, marker):
            main_indexes.update(indexes)

    return tuple(
        (
            item._replace(auxiliary=True)
            if index in candidates and index not in main_indexes
            else item
        )
        for index, item in enumerate(files)
    )


def build_video_inventory(rows, requested=None):
    """Build an inventory and select an exact requested episode when possible."""
    files = _classify_leading_auxiliary(
        tuple(_video_file(path, size) for path, size in rows if path)
    )
    main_files = tuple(item for item in files if not item.auxiliary)
    tagged = tuple(item for item in main_files if item.episode_tags)
    selected = None

    if requested is not None:
        exact = tuple(item for item in tagged if requested in item.episode_tags)
        if exact:
            selected = _largest(exact)
        elif not tagged:
            selected = _largest(main_files or files)
    else:
        selected = _largest(files)

    seasons = {season for item in tagged for season, _episode in item.episode_tags}
    pack_season = next(iter(seasons)) if len(seasons) == 1 else None
    episodes = ()
    if pack_season is not None:
        episodes = tuple(
            sorted(
                {
                    episode
                    for item in tagged
                    for season, episode in item.episode_tags
                    if season == pack_season
                }
            )
        )
        if len(episodes) < 2:
            pack_season = None

    return VideoInventory(
        selected_path=selected.path if selected else None,
        selected_size=selected.size if selected else 0,
        files=files,
        pack_season=pack_season,
        episodes=episodes,
        has_tagged_files=bool(tagged),
    )
