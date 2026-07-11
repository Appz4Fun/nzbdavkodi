# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Pure episode-aware video selection and pack classification helpers."""

import os
import re
from typing import NamedTuple, Optional

from resources.lib.webdav_match import _resolve_file_episode_tags

_AUXILIARY_RE = re.compile(
    r"(?:^|[. _/\\-])(samples?|trailers?|featurettes?|extras?)(?:[. _/\\-]|$)",
    re.IGNORECASE,
)


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


def _video_file(path, size):
    name = os.path.basename(path)
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    return VideoFile(
        path=path,
        size=_coerce_size(size),
        episode_tags=_resolve_file_episode_tags(name, parent),
        auxiliary=bool(_AUXILIARY_RE.search(path)),
    )


def _largest(files):
    return max(files, key=lambda item: item.size) if files else None


def build_video_inventory(rows, requested=None):
    """Build an inventory and select an exact requested episode when possible."""
    files = tuple(_video_file(path, size) for path, size in rows if path)
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
