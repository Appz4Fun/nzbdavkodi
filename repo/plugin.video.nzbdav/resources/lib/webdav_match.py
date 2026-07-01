# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Episode/title tagging and match scoring for WebDAV video discovery.

Pure helpers split out of ``webdav.py``: they turn release names into
``(season, episode)`` tag sets and loose token sets, and score how strongly a
discovered file name matches a requested title/episode hint. This module
imports nothing from ``webdav`` (no import cycle); ``webdav`` re-exports the
public names (e.g. ``_episode_tags``, used by ``webdav_discovery``).
"""

import re
from urllib.parse import unquote

# Capture a season followed by one or more episode numbers so a multi-episode
# file like "S01E01E02E03" yields every episode it contains, not just the
# first. The episode run allows light separators (e.g. "E01-E02", "E01.E02")
# between consecutive episode numbers within the same Sxx tag.
_EPISODE_TAG_RE = re.compile(r"s(\d{1,3})[. _-]*((?:e\d{1,4}[. _-]*)+)", re.IGNORECASE)
_EPISODE_NUM_RE = re.compile(r"e(\d{1,4})", re.IGNORECASE)
# Episode RANGE notation "SxxEaa-Ebb" / "SxxEaa-bb" (e.g. S01E01-E03,
# S01E01-03). The base _EPISODE_TAG_RE only records the literal endpoints
# (E01 and E03) and drops a bare "-03" half entirely, so a request for a
# covered middle episode (E02) would be scored as a different episode. We
# expand the inclusive span below. _EPISODE_RANGE_MAX_SPAN caps expansion so
# a malformed/absurd range (e.g. S01E01-E999) can't balloon the tag set --
# beyond the cap we leave the literal endpoints from _EPISODE_TAG_RE intact.
# The optional `(?:s\1[. _-]*)?` before the final episode number accepts the
# repeated full-season form "S01E01-S01E03" in addition to "S01E01-E03" /
# "S01E01-03". The `\1` backreference forces the SAME season, so a cross-season
# range like "S01E10-S02E02" does NOT satisfy the repeated-season branch and
# only the literal endpoints (E10, E02) survive via _EPISODE_TAG_RE.
_EPISODE_RANGE_RE = re.compile(
    r"s(\d{1,3})[. _-]*e(\d{1,4})[. _-]*-[. _-]*(?:s\1[. _-]*)?e?(\d{1,4})",
    re.IGNORECASE,
)
_EPISODE_RANGE_MAX_SPAN = 64
# Older/scene-alternate "NxNN" / "NNxNN" episode notation (e.g. 2x05,
# 02x05) that PTT's parse_title recognizes (ptt/handlers.py:1444 season
# handler) but the SxxExx regex above does not. Zero-width `(?<!\d)` /
# `(?!\d)` digit-lookarounds anchor the tag without CONSUMING the
# surrounding separator, so adjacent tags ("2x05.2x06") both register
# instead of the boundary eating the gap and dropping the second (and the
# middle of a 3-pack). The `\d{1,2}` season cap (unchanged) is what keeps
# resolutions like 1920x1080 / 1280x720 / 3840x2160 and codec tokens like
# x264/x265 from registering as episodes. Accepts the Cyrillic 'х' the PTT
# handler also allows.
_EPISODE_NXN_RE = re.compile(r"(?<!\d)(\d{1,2})[xх](\d{1,3})(?!\d)", re.IGNORECASE)
# Well-known display aspect ratios that share the NxN shape (16x9, 4x3, ...)
# but are NOT episodes. The season cap on _EPISODE_NXN_RE already rejects
# resolutions (1920x1080) and codecs (x264), but these small ratios slip
# through and would mis-parse as episode (16, 9) / (4, 3). We FILTER these
# exact pairs out of the NxN extraction rather than tightening the regex, so
# real un-padded single-digit episodes like 2x3 or 1x9 still register.
_ASPECT_RATIO_PAIRS = frozenset(
    {
        (16, 9),
        (4, 3),
        (21, 9),
        (16, 10),
        (2, 35),
        (2, 39),
        (2, 40),
        (1, 85),
        (1, 78),
        (1, 33),
        (2, 20),
        (1, 90),
        (2, 76),
    }
)
# NxN RANGE notation "1x01-03" / "1x01-1x03" -- the NxN sibling of
# _EPISODE_RANGE_RE (SxxEaa-Ebb). The standalone _EPISODE_NXN_RE above only
# records the literal endpoints (1x01 and 1x03) and drops a bare "-03" half
# entirely, so a request for a covered middle episode (1x02) would mis-score
# and a larger non-covering sibling could win. We expand the inclusive span
# below, capped by _EPISODE_RANGE_MAX_SPAN. The `\d{1,2}` season cap and the
# trailing `(?!\d)` are the resolution/codec guard (1920x1080, 1280x720,
# 3840x2160, 1920x1080-1920x1200 and 1x01-1080 all register nothing). The
# optional `(?:\d{1,2}[xх])?` handles both "1x01-03" and "1x01-1x03"; the
# literal '-' means dot-separated adjacent tags ("2x05.2x06") never collapse.
_EPISODE_NXN_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[xх](\d{1,3})[. _-]*-[. _-]*(?:\d{1,2}[xх])?(\d{1,3})(?!\d)",
    re.IGNORECASE,
)

_NON_WORD_RE = re.compile(r"[\W_]+")


def _hint_tokens(value):
    """Return lowercased alphanumeric tokens for loose name matching."""
    if not isinstance(value, str) or not value:
        return frozenset()
    cleaned = _NON_WORD_RE.sub(" ", value.lower())
    return frozenset(token for token in cleaned.split() if token)


def _add_span_tags(tags, season, start, end):
    """Add every (season, episode) in an inclusive span, capped by max span."""
    if start <= end <= start + _EPISODE_RANGE_MAX_SPAN:
        for episode in range(start, end + 1):
            tags.add((season, episode))


def _add_multi_episode_tags(tags, value):
    """Expand "SxxEaaEbb" multi-episode tags into individual (season, ep)."""
    for match in _EPISODE_TAG_RE.finditer(value):
        season = int(match.group(1))
        for episode in _EPISODE_NUM_RE.findall(match.group(2)):
            tags.add((season, int(episode)))


def _add_range_tags(tags, value):
    """Expand "SxxEaa-Ebb" episode-range notation into individual tags."""
    for match in _EPISODE_RANGE_RE.finditer(value):
        season = int(match.group(1))
        _add_span_tags(tags, season, int(match.group(2)), int(match.group(3)))


def _add_nxn_range_tags(tags, value):
    """Expand "1x01-03" NxN range notation, skipping aspect-ratio anchors."""
    for match in _EPISODE_NXN_RANGE_RE.finditer(value):
        season = int(match.group(1))
        start = int(match.group(2))
        end = int(match.group(3))
        # An aspect ratio (16x9) never forms a valid range start/end, so a
        # span anchored on one is spurious -- skip it (the standalone NxN loop
        # also filters the literal endpoints).
        if (season, start) in _ASPECT_RATIO_PAIRS or (
            season,
            end,
        ) in _ASPECT_RATIO_PAIRS:
            continue
        _add_span_tags(tags, season, start, end)


def _add_nxn_tags(tags, value):
    """Add standalone "NxNN" episode tags, skipping aspect ratios."""
    # Run AFTER the range expansion: it preserves the literal endpoints when
    # the range guard rejects a span (reversed 1x05-1x02 or cross-season
    # 1x10-2x02, where start<=end is False, or beyond the cap), so behavior is
    # a strict superset and never regresses. tags is a set, so there is no
    # double-count.
    for match in _EPISODE_NXN_RE.finditer(value):
        pair = (int(match.group(1)), int(match.group(2)))
        # Skip well-known display aspect ratios (16x9, 4x3, ...) that share the
        # NxN shape but are not episodes. Real un-padded episodes (2x3, 1x9)
        # are not in the set, so they still register.
        if pair in _ASPECT_RATIO_PAIRS:
            continue
        tags.add(pair)


def _episode_tags(value):
    """Return the set of (season, episode) tags found in a release name.

    A multi-episode tag such as "S01E01E02E03" expands to every episode it
    spans -- {(1, 1), (1, 2), (1, 3)} -- so a request for a middle episode
    (E02) still matches the combined file rather than only its first episode.
    """
    if not isinstance(value, str) or not value:
        return frozenset()
    tags = set()
    _add_multi_episode_tags(tags, value)
    _add_range_tags(tags, value)
    _add_nxn_range_tags(tags, value)
    _add_nxn_tags(tags, value)
    return frozenset(tags)


def _title_hint_match_score(file_path, hint_tokens, hint_episode_tags):
    """Return how strongly a video file name matches the requested title hint.

    Returns a 2-tuple ``(episode_score, token_score)`` so callers can rank
    episode identity ABOVE size but raw token overlap BELOW it:

    * ``episode_score`` is the strongest signal -- ``1000`` when the requested
      SxxExx episode is present, ``-1000`` when the file names a different
      episode, ``0`` when no episode comparison applies. An episode pack must
      pick the requested episode, not the largest file.
    * ``token_score`` is the raw token-overlap count (``0`` when there is no
      token hint). For a movie hint (no episode tag) this must NOT outrank
      size, or a small token-rich extra/trailer would hijack the feature; the
      folder/sibling sort keys therefore place size between the two scores.

    Returns ``(0, 0)`` when there is no usable hint or the name is empty.
    """
    if not hint_tokens and not hint_episode_tags:
        return (0, 0)
    name = unquote(file_path.rsplit("/", 1)[-1]) if file_path else ""
    if not name:
        return (0, 0)
    episode_score = _episode_match_score(file_path, name, hint_episode_tags)
    token_score = len(hint_tokens & _hint_tokens(name)) if hint_tokens else 0
    return (episode_score, token_score)


def _episode_match_score(file_path, name, hint_episode_tags):
    """Return +1000 for the requested episode, -1000 for a named other, else 0.

    A named different episode is never preferred over a true match, though token
    overlap may still rank it among non-episode candidates.
    """
    if not hint_episode_tags:
        return 0
    parent_path = file_path.rsplit("/", 1)[0] if file_path and "/" in file_path else ""
    file_tags = _resolve_file_episode_tags(name, parent_path)
    if not file_tags:
        return 0
    return 1000 if hint_episode_tags & file_tags else -1000


def _resolve_file_episode_tags(name, parent_path):
    """Resolve the authoritative episode tags for a file via a layered fallback.

    Basename FIRST: the file's own episode tag is authoritative. Only when the
    basename carries no episode tag (a generically-named file like "video.mkv")
    do we fall back to the directory -- this is a LAYERED fallback, NOT a union:
    a matching dir tag must never mask a wrong-episode FILENAME, or the
    wrong-episode gate would regress.

    The fallback scores the NEAREST parent SEGMENT and treats it as
    authoritative when it carries its OWN episode tag (most-specific identity),
    so a wrong nearer dir like "Show.S01E03" is correctly wrong and is NOT
    rescued by an ancestor pack folder ("Show.S01E02.Pack"). It widens to scan
    the FULL ancestor path only when the nearest dir is generic (e.g. "1080p"),
    so a grandparent's tag ("Show.S01E02/1080p/video.mkv") still supplies the
    match. A season-complete parent ("Show.S01.Complete") yields no tag, so
    largest-wins is preserved there.
    """
    file_tags = _episode_tags(name)
    if not file_tags and parent_path:
        nearest_tags = _episode_tags(unquote(parent_path.rsplit("/", 1)[-1]))
        file_tags = (
            nearest_tags if nearest_tags else _episode_tags(unquote(parent_path))
        )
    return file_tags
