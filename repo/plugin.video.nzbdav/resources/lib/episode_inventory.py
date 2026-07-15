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
_UNAMBIGUOUS_AUXILIARY_MARKERS = frozenset(
    ("sample", "samples", "featurette", "featurettes")
)
_AMBIGUOUS_LEADING_MARKERS = frozenset(("extra", "extras", "trailer", "trailers"))

_PATH_SEP_RE = re.compile(r"[/\\]+")
_WORD_SEP_RE = re.compile(r"[. _-]+")


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


def _auxiliary_parent_markers(parent):
    return tuple(
        item
        for item in _PATH_SEP_RE.split(parent)
        if item and _AUXILIARY_TOKEN_RE.fullmatch(item)
    )


def _has_different_auxiliary_parent(name, auxiliary_parents):
    leading_name = _WORD_SEP_RE.split(name, 1)[0]
    return any(leading_name.casefold() != item.casefold() for item in auxiliary_parents)


def _marker_position_is_auxiliary(stem, match):
    if match.start() == 0:
        return False
    if match.group(1).casefold() in _UNAMBIGUOUS_AUXILIARY_MARKERS:
        return True
    if match.end() == len(stem):
        return True
    return bool(_resolve_file_episode_tags(stem[: match.start()], ""))


def _name_has_auxiliary_marker(name, show_folder_exception):
    """Return whether ``name`` carries a conservative auxiliary marker."""
    stem = os.path.splitext(name)[0]
    if not show_folder_exception and _AUXILIARY_TOKEN_RE.fullmatch(stem):
        return True

    leading_marker = _AUXILIARY_RE.match(name)
    if leading_marker and (
        leading_marker.group(1).casefold() in _UNAMBIGUOUS_AUXILIARY_MARKERS
    ):
        return True
    return any(
        _marker_position_is_auxiliary(stem, match)
        for match in _AUXILIARY_RE.finditer(stem)
    )


def _is_auxiliary(name, parent):
    """Classify conservative filename and directory auxiliary markers."""
    auxiliary_parents = _auxiliary_parent_markers(parent)
    different_auxiliary_parent = _has_different_auxiliary_parent(
        name, auxiliary_parents
    )
    show_folder_exception = bool(auxiliary_parents) and not different_auxiliary_parent
    if different_auxiliary_parent:
        return True
    return _name_has_auxiliary_marker(name, show_folder_exception)


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
    parts = tuple(item for item in _WORD_SEP_RE.split(remainder) if item)
    for count in range(1, len(parts) + 1):
        probe = ".".join(parts[:count])
        if _resolve_file_episode_tags(probe, ""):
            return (match.group(1).casefold(), count >= 3)
    return None


def _release_folder_matches_marker(path, marker):
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    for segment in (item for item in _PATH_SEP_RE.split(parent) if item):
        leading_segment = _WORD_SEP_RE.split(segment, 1)[0]
        if leading_segment.casefold() == marker:
            return True
    return False


def _leading_group_is_show(files, indexes, marker):
    if marker not in _AMBIGUOUS_LEADING_MARKERS or len(indexes) < 2:
        return False
    episode_tags = {tag for index in indexes for tag in files[index].episode_tags}
    return len(episode_tags) >= 2


def _leading_auxiliary_candidate(item):
    """Return ``(marker, is_main)`` for one candidate, or ``None``."""
    if item.auxiliary:
        return None
    context = _leading_auxiliary_context(item.path)
    if not context:
        return None
    marker, has_title_prefix = context
    is_main = marker in _AMBIGUOUS_LEADING_MARKERS and (
        has_title_prefix or _release_folder_matches_marker(item.path, marker)
    )
    return marker, is_main


def _leading_auxiliary_groups(files):
    """Collect leading-marker candidates, groups, and definite main files."""
    candidates = {}
    groups = {}
    main_indexes = set()
    for index, item in enumerate(files):
        candidate = _leading_auxiliary_candidate(item)
        if candidate is None:
            continue
        marker, is_main = candidate
        candidates[index] = marker
        groups.setdefault(marker, []).append(index)
        if is_main:
            main_indexes.add(index)
    return candidates, groups, main_indexes


def _classify_leading_auxiliary(files):
    candidates, groups, main_indexes = _leading_auxiliary_groups(files)

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


def _pack_episode_summary(tagged):
    seasons = {season for item in tagged for season, _episode in item.episode_tags}
    if len(seasons) != 1:
        return None, ()
    pack_season = next(iter(seasons))
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
    return pack_season, episodes


def _selected_video(files, main_files, tagged, requested):
    """Select the requested episode or preserve the legacy largest fallback."""
    if requested is None:
        return _largest(files)

    exact = tuple(item for item in tagged if requested in item.episode_tags)
    if exact:
        return _largest(exact)
    if not tagged and len(main_files) == 1:
        # A lone generic video is a defensible ordinary-release fallback.
        # Multiple untagged videos are ambiguous for an explicit episode:
        # selecting by size would silently play an arbitrary file.
        return main_files[0]
    return None


def _inventory_result(files, tagged, selected):
    """Build the immutable result after classification and selection."""
    pack_season, episodes = _pack_episode_summary(tagged)
    return VideoInventory(
        selected_path=selected.path if selected else None,
        selected_size=selected.size if selected else 0,
        files=files,
        pack_season=pack_season,
        episodes=episodes,
        has_tagged_files=bool(tagged),
    )


def build_video_inventory(rows, requested=None):
    """Build an inventory and select an exact requested episode when possible."""
    files = _classify_leading_auxiliary(
        tuple(_video_file(path, size) for path, size in rows if path)
    )
    main_files = tuple(item for item in files if not item.auxiliary)
    tagged = tuple(item for item in main_files if item.episode_tags)
    selected = _selected_video(files, main_files, tagged, requested)
    return _inventory_result(files, tagged, selected)
