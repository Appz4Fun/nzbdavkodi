# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Focused-ListItem episode/InfoLabel recovery helpers split out of ``router``.

These read Kodi InfoLabels to backfill (title, season, episode) for plays that
arrive with only series ids. None of them are test-patched or imported by name,
so they move cleanly; ``router`` re-exports them for callers.
"""

import xbmc

_PLAY_INFOLABEL_SOURCES = [
    ("ListItem", "ListItem.Season", "ListItem.Episode", "ListItem.TVShowTitle"),
    (
        "Container.ListItem",
        "Container.ListItem.Season",
        "Container.ListItem.Episode",
        "Container.ListItem.TVShowTitle",
    ),
    (
        "VideoPlayer",
        "VideoPlayer.Season",
        "VideoPlayer.Episode",
        "VideoPlayer.TVShowTitle",
    ),
    (
        "Container(50).ListItem",
        "Container(50).ListItem.Season",
        "Container(50).ListItem.Episode",
        "Container(50).ListItem.TVShowTitle",
    ),
]


def _episode_info_from_listitem(expected_title=""):
    """Read (show, season, episode) from the currently focused Kodi ListItem.

    TMDBHelper Next-Up / widget / home-screen plays frequently invoke the
    player with only the *series* ids and empty season/episode, which
    broadens an episode search to the whole show. The focused widget item
    still exposes the episode numbers via InfoLabels, so they can be
    recovered here as a fallback. Returns ``(show, season, episode)`` strings;
    season/episode are blanked unless numeric, and any failure degrades to
    empty strings.

    When ``expected_title`` is given, prefer the InfoLabel root whose show
    matches it: a stale/bare root that happens to carry S/E for a *different*
    show must not short-circuit the probe (the caller's same-show guard would
    reject it and the correct later root — e.g. ``Container.ListItem.*`` —
    would never be read).
    """
    # Widget/RunScript plays expose the focused item through different
    # InfoLabel roots depending on the skin/window (bare ``ListItem.*`` vs
    # ``Container.ListItem.*`` vs ``VideoPlayer.*``), so probe the same set the
    # handle-based ``_handle_play`` does — reading only bare ``ListItem.*``
    # returns blank for many widget sources and broadens the search.
    label_sources = (
        ("ListItem.TVShowTitle", "ListItem.Season", "ListItem.Episode"),
        (
            "Container.ListItem.TVShowTitle",
            "Container.ListItem.Season",
            "Container.ListItem.Episode",
        ),
        ("VideoPlayer.TVShowTitle", "VideoPlayer.Season", "VideoPlayer.Episode"),
        (
            "Container(50).ListItem.TVShowTitle",
            "Container(50).ListItem.Season",
            "Container(50).ListItem.Episode",
        ),
    )
    want = (expected_title or "").strip().casefold()
    try:
        return _scan_listitem_episode_sources(label_sources, want)
    except Exception:  # pylint: disable=broad-except
        return "", "", ""


def _scan_listitem_episode_sources(label_sources, want):
    """Probe InfoLabel roots for the (show, season, episode) matching ``want``.

    Treats each root as an ATOMIC candidate so a stale show title from one root
    is never paired with numbers from another. With a title set and no root
    matching, returns blanks rather than inject a different show's numbers; with
    no title, the first complete root is the only (and best) signal.
    """
    fallback = ("", "", "")
    for labels in label_sources:
        candidate = _listitem_episode_candidate(labels)
        if candidate is None:
            continue
        show = candidate[0]
        if not want or show.strip().casefold() == want:
            return candidate
        if fallback == ("", "", ""):
            fallback = candidate
    return ("", "", "") if want else fallback


def _listitem_episode_candidate(labels):
    """Read an atomic (show, season, episode) from one InfoLabel root.

    Returns ``None`` when the root lacks numeric season AND episode digits.
    """
    t_label, s_label, e_label = labels
    show = (xbmc.getInfoLabel(t_label) or "").strip()
    season = (xbmc.getInfoLabel(s_label) or "").strip()
    episode = (xbmc.getInfoLabel(e_label) or "").strip()
    if not (season.isdigit() and episode.isdigit()):
        return None
    return show, season, episode


def _numeric_infolabel(label):
    """Read an InfoLabel that carries a season/episode number, else "".

    "0" is a real season (specials) and episode (pilot/E0) value — only
    "" / "-1" mean Kodi has no selection. The previous filter dropped specials
    entirely. TODO.md §H.2-M30.
    """
    value = xbmc.getInfoLabel(label)
    if value and value not in ("", "-1"):
        return value
    return ""


def _infolabel_backfill_from_source(source, title, season, episode):
    """Backfill (title, season, episode) from one InfoLabel root."""
    _src_name, s_label, e_label, t_label = source
    season = season or _numeric_infolabel(s_label)
    episode = episode or _numeric_infolabel(e_label)
    il_t = xbmc.getInfoLabel(t_label)
    if il_t and not title:
        title = il_t
    return title, season, episode


def _episode_info_from_infolabels(title, season, episode):
    """Backfill (title, season, episode) for a play from focused-item InfoLabels.

    Probes every known InfoLabel root; the first that completes the missing
    season AND episode wins (and is the only source logged). Mirrors the prior
    inline ``_handle_play`` behaviour exactly.
    """
    for source in _PLAY_INFOLABEL_SOURCES:
        title, season, episode = _infolabel_backfill_from_source(
            source, title, season, episode
        )
        if season and episode:
            # Only log the winning source; logging every probed source in the
            # success path made a noisy 4-line log entry per play.
            xbmc.log(
                "NZB-DAV: InfoLabel resolved: '{}' S{}E{} (from {})".format(
                    title, season, episode, source[0]
                ),
                xbmc.LOGINFO,
            )
            break
    return title, season, episode
