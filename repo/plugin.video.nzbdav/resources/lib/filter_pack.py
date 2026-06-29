# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Multi-episode / season pack detection for the #282 stub size-guard.

Split out of :mod:`resources.lib.filter` to keep that module under the
file-size guard. ``release_is_pack`` is consumed via
``resources.lib.filter.release_is_pack`` (resolver.py + tests), so ``filter``
re-exports it. These helpers lazily import ``webdav`` / ``ptt`` and call no
name that tests patch on ``resources.lib.filter``; this module must not import
``filter`` (no cycle).
"""

import re

# Whole-collection / complete-series PHRASING that marks a season-tag-less
# release as a multi-item pack: the word "complete" ADJACENT to a
# collection/series/saga/set keyword (either order), or "box set" / "mini
# series". Matching the phrase -- not PTT's bare ``complete`` flag, nor a lone
# "collection"/"series" word -- is what keeps the #282 stub guard ACTIVE for a
# movie whose title merely contains "Complete" (``Complete.Unknown.2024``) or
# "Collection" (``The.Collection.2012.COMPLETE`` -- the year separates the
# words, so no phrase matches). PTT exposes no collection flag, so the raw
# title is the only signal (#340 review).
_PACK_KEYWORD = (
    r"collections?|series|saga|seasons?|sets?|pack|anthology|"
    r"trilogy|duology|quadrilogy|filmography"
)
# An optional ordinal between "complete" and the keyword: "Complete First
# Season", "Complete 2nd Season", "Complete Final Season" are all packs. PTT
# leaves seasons/episodes empty for these ordinal forms, so the phrase is the
# only signal (#340 review).
_PACK_ORDINAL = (
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"final|last|[0-9]{1,2}(?:st|nd|rd|th)"
)
# A spelled cardinal that can sit between the keyword and "Complete" in the
# reversed phrasing ("Season One Complete"): PTT does not parse spelled numbers,
# so without this the reversed branch only matches "Season Complete" and misses
# the numbered form (#340 Codex review).
_PACK_CARDINAL = r"one|two|three|four|five|six|seven|eight|nine|ten"
# Ordinals safe to pair with a BARE "Season" (no "Complete"): numeric/positional
# forms only. ``final``/``last`` are deliberately EXCLUDED here because
# "The Last Season" / "Final Season" are real single-movie titles -- treating
# those as packs would skip the #282 stub guard for them. With an adjacent
# "Complete" the full _PACK_ORDINAL (incl. final/last) is fine, since "Complete
# Final Season" is unambiguously a whole-season pack (#340 Codex review).
_PACK_SEASON_ORDINAL = (
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"[0-9]{1,2}(?:st|nd|rd|th)"
)
# Unambiguous multi-item collection words that mark a pack ON THEIR OWN, without
# an adjacent "Complete": a "Trilogy"/"Quadrilogy"/"Anthology"/"Filmography"
# release bundles several films, so its advertised size spans them all and one
# picked film is legitimately a fraction of it -- without this the single-file
# floor would reject a real 20 GB movie out of a 60 GB trilogy as a stub (#340
# Codex review). Deliberately excludes "collection"/"series"/"saga"/"season"/
# "set"/"pack", which occur in single-movie titles ("The.Collection.2012") and
# must stay guarded -- those remain pack signals only in the "Complete <kw>" /
# "<kw> Complete" phrasing below.
_PACK_STANDALONE_KEYWORD = (
    r"trilogy|duology|quadrilogy|pentalogy|hexalogy|anthology|filmography"
)
_PACK_PHRASE_RE = re.compile(
    r"(?<![a-z])(?:"
    r"complete[ ._-]+(?:(?:" + _PACK_ORDINAL + r")[ ._-]+)?(?:" + _PACK_KEYWORD + r")"
    # Reversed phrasing, optionally numbered: "Series Complete", "Season One
    # Complete", "Season First Complete".
    r"|(?:" + _PACK_KEYWORD + r")[ ._-]+"
    r"(?:(?:" + _PACK_ORDINAL + r"|" + _PACK_CARDINAL + r")[ ._-]+)?complete"
    # Spelled ordinal/cardinal adjacent to "season(s)", either order, with no
    # explicit "Complete": "First Season", "The Second Season", "3rd Season",
    # "Season Two". PTT does not parse the spelled number, so it leaves
    # seasons=[]/episodes=[] and the bare-season check below misses these. The
    # adjacency requirement keeps a bare ordinal in a movie title
    # ("First.Blood", "First.Man", "Second.Act") from matching (#340 Codex
    # review). Still gated on ``not episode_tags`` in release_is_pack: a single
    # "First.Season.S01E05" keeps the stub guard.
    r"|(?:" + _PACK_SEASON_ORDINAL + r"|" + _PACK_CARDINAL + r")[ ._-]+seasons?"
    r"|seasons?[ ._-]+(?:" + _PACK_SEASON_ORDINAL + r"|" + _PACK_CARDINAL + r")"
    r"|box[ ._-]?sets?"
    # "Mini Series" / "Limited Series" season-tag-less TV packs.
    r"|(?:mini|limited)[ ._-]?series"
    r"|(?:" + _PACK_STANDALONE_KEYWORD + r")"
    r")(?![a-z])",
    re.IGNORECASE,
)


def release_is_pack(title):
    """Return True when a release name denotes a multi-episode / season pack.

    A pack's advertised size covers many episodes, so the single episode a
    picker selects out of it is legitimately a fraction of that size. The #282
    stub size-guard (resolver) must therefore SKIP packs, or it would reject a
    real episode for being far smaller than the whole-pack advertised size.

    A title is a pack when it spans more than one episode (including
    ``S01E01E02E03`` multi-tags and ``1x01-1x10`` / ``S01E01-E10`` ranges,
    which PTT collapses to a single episode but ``_episode_tags`` expands),
    more than one season, a whole season with no single episode (e.g. ``S01``,
    ``S01-S05 COMPLETE``), carries whole-collection PHRASING ("Complete
    Collection / Series", "Box Set", "Mini/Limited Series"), or names an
    unambiguous multi-film collection on its own ("Trilogy", "Quadrilogy",
    "Anthology", "Filmography"). A movie, a single ``SxxExx`` (even one tagged
    ``COMPLETE`` or carrying a collection word), and a movie whose title merely
    contains "Complete" or "Collection" are NOT packs -- they keep the stub
    guard (the single episode tag overrides any pack phrase).

    On a missing title or a PTT parse error this returns ``False`` ("not a
    pack"), which only ever leaves the caller's size-guard *active* (never
    weaker); that guard's own conservative fraction floor is the second safety
    net, so a rare parse glitch cannot turn the protection off.
    """
    if not isinstance(title, str) or not title:
        return False
    # NxN-range packs ("1x01-1x10") and multi-episode tags ("S01E01E02E03")
    # collapse in PTT to a single (season, episode), so the season/episode-count
    # checks below miss them. webdav._episode_tags expands NxN ranges, SxxExx
    # ranges, and multi-episode tags to one (season, episode) tuple each, so
    # more than one tag is a pack. (webdav does not import filter -> no cycle.)
    from resources.lib.webdav import _episode_tags

    episode_tags = _episode_tags(title)
    if len(episode_tags) > 1:
        return True
    # Season-tag-less collection/complete-series packs, matched as a phrase so a
    # "Complete"-titled movie or a "Collection" in a movie title does not skip
    # the stub guard (#340 review). Gate on having NO single episode tag: a real
    # single-episode release whose name happens to contain a pack phrase (e.g.
    # "Chernobyl.Miniseries.S01E01", "Some.Show.Box.Set.S01E05") advertises a
    # one-episode size, so it must keep the #282 stub guard -- the episode tag
    # overrides the phrase. Whole-season miniseries (no episode tag) stay packs
    # via this branch or the bare-season PTT check below (PR #340 Codex review).
    if not episode_tags and _PACK_PHRASE_RE.search(title):
        return True
    return _ptt_season_episode_is_pack(title)


def _as_count_list(value):
    """Coerce a PTT season/episode field to a list (empty when falsy)."""
    if not value:
        return []
    if not isinstance(value, list):
        return [value]
    return value


def _ptt_season_episode_is_pack(title):
    """True when PTT's season/episode counts denote a multi-item pack."""
    try:
        from resources.lib.ptt import parse_title

        parsed = parse_title(title)
    except Exception:  # pylint: disable=broad-except
        return False
    seasons = _as_count_list(parsed.get("seasons"))
    episodes = _as_count_list(parsed.get("episodes"))
    if len(episodes) > 1 or len(seasons) > 1:
        return True
    # A season tag with no single episode is a whole-season pack.
    return bool(seasons) and not episodes
