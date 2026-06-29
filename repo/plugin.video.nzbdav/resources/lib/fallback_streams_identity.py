# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Release content-identity and title/metadata matching helpers."""

import re

import resources.lib.fallback_streams as _fs


def _normalize_title(value):
    """Normalize release titles for conservative duplicate grouping."""
    if not isinstance(value, str):
        return ""
    # "&amp;" is the XML/HTML escape for "&". XML parsing normally decodes it,
    # but double-escaped feeds leave a literal entity in the title; rewrite it to
    # "&" so it collapses to nothing (like a bare "&") instead of leaving a stray
    # "amp" token. Decode REPEATEDLY so even a double-escaped "&amp;amp;" fully
    # resolves -- each pass replaces "&amp;" (5 chars) with "&" (1 char), so the
    # string strictly shrinks and the loop terminates. The rewrite is exact, so a
    # genuine "amp" word (e.g. "Marshall Amp") is left untouched.
    lowered = value.lower()
    while "&amp;" in lowered:
        lowered = lowered.replace("&amp;", "&")
    normalized = _fs._NON_WORD_RE.sub(" ", lowered)
    tokens = normalized.split()
    # Treat "&", the conjunction words ("and" plus the common foreign forms
    # "et"/"und"), and an omitted conjunction as one identity: drop a conjunction
    # token so "Friends & Neighbors" (the "&" is already stripped by the non-word
    # sub), "Friends and Neighbors", "Friends Neighbors", and "Jules et Jim"/
    # "Jules Jim" all normalize equal and peer as fallbacks. Fold ONLY an
    # INTERIOR conjunction (operand on both sides) -- that is the only true
    # conjunction position. A leading/trailing token is content-bearing ("And
    # Just Like That" is not "Just Like That"), and a lone token is never a
    # conjunction ("ET"). Keeping boundary tokens also guarantees a non-empty
    # title never folds to empty (an empty core title would match anything in the
    # corroborated identity paths). Whole-token only, so substrings stay intact
    # ("Andromeda"/"Planet"/"Underworld"), and ordinal words like Part
    # "One"/"Two" are not conjunctions, so part/chapter discrimination is
    # unaffected.
    last = len(tokens) - 1
    return " ".join(
        token
        for index, token in enumerate(tokens)
        if not (0 < index < last and token in _fs._CONJUNCTION_TOKENS)
    )


_PART_ORDINAL_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
}


_PART_LABEL_RE = re.compile(
    r"\b(?:part|chapter|vol(?:ume)?|book)\b[\s._-]*([0-9]{1,3}|[ivx]{1,4}|"
    r"one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)


_SEQUEL_TAIL_TOKENS = frozenset(
    {
        "ii",
        "iii",
        "iv",
        "vi",
        "vii",
        "viii",
        "ix",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
    }
)


_RELEASE_IDENTITY_CACHE_TITLE_KEY = "_fallback_identity_title"  # nosec B105


_RELEASE_IDENTITY_CACHE_VALUE_KEY = "_fallback_identity"  # nosec B105


def _part_number_from_title(title):
    """Return the part/chapter/volume number embedded in a title, or 0."""
    if not isinstance(title, str) or not title:
        return 0
    match = _fs._PART_LABEL_RE.search(title)
    if not match:
        return 0
    token = match.group(1).strip().lower()
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return 0
    return _fs._PART_ORDINAL_WORDS.get(token, 0)


def _sorted_int_tuple(values):
    """Return a sorted, de-duplicated tuple of the int-coercible values."""
    if isinstance(values, (int, str)):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return ()
    out = []
    for value in values:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(sorted(set(out)))


def _release_identity(result):
    """Return PTT-derived content identity for a release.

    Returns a tuple ``(title, year, seasons, episodes, part)`` where
    ``title`` is the normalized PTT show/movie title, ``year`` is the
    parsed year (0 when absent), ``seasons``/``episodes`` are sorted
    tuples, and ``part`` is a part/chapter/volume number (0 when absent).
    This is the authoritative content fingerprint used by the fallback
    content-identity gate. Falls back to the normalized raw title when
    PTT cannot parse.
    """
    if not isinstance(result, dict):
        raw = result if isinstance(result, str) else ""
        return (_fs._normalize_title(raw), 0, (), (), 0)
    title = result.get("title", "")
    cached = result.get(_fs._RELEASE_IDENTITY_CACHE_VALUE_KEY)
    if result.get(_fs._RELEASE_IDENTITY_CACHE_TITLE_KEY) == title and isinstance(
        cached, tuple
    ):
        return cached
    parsed = _fs._parsed_title_fields(title)
    norm_title = _fs._normalize_title(parsed.get("title") or title)
    seasons = _fs._sorted_int_tuple(parsed.get("seasons"))
    episodes = _fs._sorted_int_tuple(parsed.get("episodes"))
    part = _fs._part_number_from_title(title)
    identity = (norm_title, parsed["year"], seasons, episodes, part)
    result[_fs._RELEASE_IDENTITY_CACHE_TITLE_KEY] = title
    result[_fs._RELEASE_IDENTITY_CACHE_VALUE_KEY] = identity
    return identity


def _parsed_title_fields(title):
    """Return PTT-parsed fields for a title with a coerced int ``year``.

    Always returns a dict carrying at least an int ``year`` so the caller
    never re-handles parse failures.
    """
    try:
        from resources.lib.ptt import parse_title

        parsed = parse_title(title) or {}
    except Exception:  # pylint: disable=broad-except
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    try:
        parsed["year"] = int(parsed.get("year") or 0)
    except (TypeError, ValueError):
        parsed["year"] = 0
    return parsed


def _subset_titles_related(left, right, left_tokens, right_tokens, corroborated):
    """Return whether two titles in a subset relation name the same work.

    One token set is a subset of the other (the equality case is handled by the
    caller). Accept only a junk-SUFFIX repost: the longer title's extra tokens
    are a single trailing noise token. More than one extra token, or extra
    tokens that are not a trailing tail, look like a distinguishing subtitle and
    are rejected without corroboration.
    """
    if corroborated:
        return True
    if left <= right:
        shorter, longer = left_tokens, right_tokens
    else:
        shorter, longer = right_tokens, left_tokens
    prefix_match = longer[: len(shorter)] == shorter
    extra = len(longer) - len(shorter)
    if prefix_match and extra <= 1:
        # A lone trailing numeric token ("Avatar" -> "Avatar 2") is a sequel
        # discriminator, not junk. PTT leaves the sequel number inside the
        # title (years/resolutions are already stripped into year/meta), so a
        # numeric tail that survives normalization is content-distinguishing.
        # The same holds for MULTI-CHARACTER Roman-numeral / ordinal-word
        # sequel tails ("Rocky" -> "Rocky IV", "Iron Man" -> "Iron Man
        # Three"). Single-letter romans (i/v/x) and "one"/"eleven"+ are
        # deliberately NOT in _SEQUEL_TAIL_TOKENS, so a stray "Movie x"/junk
        # tail stays a legitimate junk-suffix repost. Reject these tails
        # without corroboration; any other trailing token ("Movie" ->
        # "Movie mirror") stays a legitimate junk-suffix repost.
        if extra == 1 and (
            longer[-1].isdigit() or longer[-1] in _fs._SEQUEL_TAIL_TOKENS
        ):
            return False
        return True
    return False


def _titles_core_related(primary_title, candidate_title, corroborated=False):
    """Return whether two normalized core titles plausibly name the same work.

    Reposts often differ only by a junk suffix (e.g. "Movie" vs "Movie
    mirror"), so a single trailing extra token on one side is treated as noise
    and accepted. A multi-token extra tail looks like a distinguishing subtitle
    (e.g. "Avatar" vs "Avatar The Way Of Water") and is rejected unless
    ``corroborated`` positive identity (matching year/episode) backs it up. An
    empty token set on either side fails closed unless corroborated.
    """
    left_tokens = primary_title.split()
    right_tokens = candidate_title.split()
    left = frozenset(left_tokens)
    right = frozenset(right_tokens)
    if not left or not right:
        # Fail closed on a missing core title unless positive identity agrees.
        return corroborated
    if left == right:
        return True
    if left <= right or right <= left:
        return _fs._subset_titles_related(
            left, right, left_tokens, right_tokens, corroborated
        )
    return _fs._disjoint_titles_related(left, right, corroborated)


def _disjoint_titles_related(left, right, corroborated):
    """Return whether two title sets with mutual distinguishing tails are related.

    Neither token set is a subset of the other, so each carries its own
    distinguishing tail. A MULTI-token distinguishing tail on either side looks
    like a different work in a franchise ("Mission Impossible Fallout" vs
    "...Dead Reckoning", "Star Wars The Force Awakens" vs "...The Last Jedi")
    rather than a repost, so require corroborating positive identity (a matching
    year, or a matching season+episode set) before accepting it -- a loose
    >=2-token prefix overlap is too weak on its own. A single-token difference on
    each side stays repost noise ("Movie mirror" vs "Movie repost") and keeps the
    existing token-overlap behavior, mirroring the <=1-trailing-token junk-suffix
    rule of the subset case above.
    """
    left_extra = left - right
    right_extra = right - left
    if (len(left_extra) >= 2 or len(right_extra) >= 2) and not corroborated:
        return False
    return _fs._title_token_sets_look_related(left, right)


def _content_discriminators_match(primary, candidate):
    """Return whether two same-titled releases are the same *cut* of the work.

    Edition (Theatrical vs Extended/Director's) and PROPER/REPACK status are
    content discriminators: a Theatrical encode is not a valid fallback for an
    Extended encode even though title/year match. Resolution, codec, group,
    HDR, and audio are deliberately *not* checked here — those only affect the
    fallback tier, not whether the candidate is the same content.
    """
    primary_meta = _fs._result_meta(primary)
    candidate_meta = _fs._result_meta(candidate)
    left_edition = _fs._normalize_title(
        _fs._meta_value_from_meta(primary_meta, "edition")
    )
    right_edition = _fs._normalize_title(
        _fs._meta_value_from_meta(candidate_meta, "edition")
    )
    if left_edition != right_edition:
        return False
    for key in ("proper", "repack", "upscaled"):
        if _fs._meta_bool_from_meta(primary_meta, key) != _fs._meta_bool_from_meta(
            candidate_meta, key
        ):
            return False
    return True


def _identity_corroborated(primary_identity, candidate_identity):
    """Return whether matching year or season+episode sets corroborate identity."""
    _, primary_year, primary_seasons, primary_episodes, _ = primary_identity
    _, candidate_year, candidate_seasons, candidate_episodes, _ = candidate_identity
    if primary_year and candidate_year and primary_year == candidate_year:
        return True
    seasons_match = bool(primary_seasons) and primary_seasons == candidate_seasons
    episodes_match = bool(primary_episodes) and primary_episodes == candidate_episodes
    return seasons_match and episodes_match


def _collapse_phantom_season(primary_identity, candidate_identity):
    """Return season tuples with a phantom (mis-parsed) lone season collapsed.

    A movie whose release-group suffix mis-parses as a season (e.g.
    "...HEVC-REMUX-ALT01" -> seasons=[1], episodes=[]) would otherwise look
    episodic, and the season-presence parity check would then reject the same
    movie posted by a normal group (seasons=[]). When BOTH sides have no
    episode, the season PRESENCE differs, and both parsed the SAME year, treat
    the lone "season" as the phantom it is and collapse it away. The
    matching-year gate is required: genuine TV is always season-tagged on both
    sides (presence matches), so it never reaches this collapse and stays
    subject to season equality. NOTE: extending this collapse to the
    both-years-ABSENT case was attempted and reverted -- a yearless phantom
    season and a yearless REAL season pack ("Show.S01"/"Show.Сезон.1") share an
    identical parsed identity, and no heuristic could tell them apart without
    re-implementing (and forever chasing) PTT's multilingual season vocabulary,
    so every loosening leaked a dangerous false-ACCEPT of different content. The
    yearless-phantom-movie false-reject is an accepted fail-safe limitation
    (never serves wrong content), per the "never loosen _same_content to win
    coverage" rule.
    """
    _, primary_year, primary_seasons, primary_episodes, _ = primary_identity
    _, candidate_year, candidate_seasons, candidate_episodes, _ = candidate_identity
    same_year = bool(primary_year) and primary_year == candidate_year
    if (
        not primary_episodes
        and not candidate_episodes
        and bool(primary_seasons) != bool(candidate_seasons)
        and same_year
    ):
        return (), ()
    return primary_seasons, candidate_seasons


def _episode_set_pair_matches(primary_set, candidate_set):
    """Return whether two season/episode sets share presence and value.

    Rejects when only one side carries the evidence (presence parity) and
    when both sides carry it but the sets differ.
    """
    if bool(primary_set) != bool(candidate_set):
        return False
    if primary_set and candidate_set and primary_set != candidate_set:
        return False
    return True


def _episode_content_matches(primary_identity, candidate_identity):
    """Return whether two episodic releases are the same episode content."""
    _, primary_year, primary_seasons, primary_episodes, _ = primary_identity
    _, candidate_year, candidate_seasons, candidate_episodes, _ = candidate_identity
    if not _fs._episode_set_pair_matches(primary_seasons, candidate_seasons):
        return False
    # An episode must not peer with a different episode that simply omitted
    # its SxxExx tokens; require both to carry the same episode evidence.
    if not _fs._episode_set_pair_matches(primary_episodes, candidate_episodes):
        return False
    # A differing parsed year marks a distinct production sharing the same
    # SxxExx (a reboot/remake, e.g. "Doctor Who 2005 S01E01" vs the 2023
    # reboot). Mirror the movie-path year reject. Only rejects when BOTH
    # sides parsed a year and they differ, so same-episode reposts where one
    # side omits the year are unaffected.
    if primary_year and candidate_year and primary_year != candidate_year:
        return False
    return True


def _same_content(primary, candidate):
    """Return whether two releases are the SAME content (content-identity gate).

    Movies: same core title and same year (when both parsed a year).
    Episodes: same show title, season, and episode set.
    Any parsed part/chapter/volume number must match. Edition and
    PROPER/REPACK status (the same-cut discriminators) must also match. This
    is the authoritative hard gate that prevents falling back to a different
    release (different movie part, year, episode, edition, etc.).
    """
    if not _fs._content_discriminators_match(primary, candidate):
        return False
    primary_identity = _fs._release_identity(primary)
    candidate_identity = _fs._release_identity(candidate)
    primary_title, primary_year, _, _, primary_part = primary_identity
    candidate_title, candidate_year, _, _, candidate_part = candidate_identity

    corroborated = _fs._identity_corroborated(primary_identity, candidate_identity)
    if not _fs._titles_core_related(
        primary_title, candidate_title, corroborated=corroborated
    ):
        return False

    # Part/chapter number is a content discriminator (Part One vs Part Two).
    if primary_part and candidate_part and primary_part != candidate_part:
        return False

    primary_seasons, candidate_seasons = _fs._collapse_phantom_season(
        primary_identity, candidate_identity
    )
    return _fs._same_content_seasonal_tail(
        (
            primary_title,
            primary_year,
            primary_seasons,
            primary_identity[3],
            primary_part,
        ),
        (
            candidate_title,
            candidate_year,
            candidate_seasons,
            candidate_identity[3],
            candidate_part,
        ),
    )


def _identity_is_episodic(identity):
    """Return whether an identity tuple carries any season or episode evidence."""
    _, _, seasons, episodes, _ = identity
    return bool(seasons or episodes)


def _same_content_seasonal_tail(primary_identity, candidate_identity):
    """Return whether the episode-or-movie tail of the content gate accepts.

    ``*_identity`` tuples are ``(title, year, seasons, episodes, part)`` after
    phantom-season collapse.
    """
    _, primary_year, _, _, primary_part = primary_identity
    _, candidate_year, _, _, candidate_part = candidate_identity
    if _fs._identity_is_episodic(primary_identity) or _fs._identity_is_episodic(
        candidate_identity
    ):
        return _fs._episode_content_matches(primary_identity, candidate_identity)

    if bool(primary_part) != bool(candidate_part):
        # PTT keeps the part word inside the title ("Dune Part Two"), so the
        # core titles never compare equal to the bare original. One side naming
        # an explicit part while the other names none is a sequel-vs-original
        # mismatch (e.g. "Dune Part Two" vs "Dune"); treat it as different
        # content. A differing explicit part is already rejected above. This is
        # the movie discriminator only: episodes routinely keep an episode-title
        # token ("Chapter One") that PTT leaves in the title, so the same SxxExx
        # posted with and without that token must still peer (handled above).
        return False

    # Movies: a differing year is different content.
    if primary_year and candidate_year and primary_year != candidate_year:
        return False
    return True


def _release_similarity(primary, candidate):
    """Return a fallback tier for ``candidate`` vs ``primary``, or None.

    None means different content (hard reject). Otherwise:
      0  same resolution + codec + group, size within ~3%
      1  same resolution + codec, size within ~10%
      2  same resolution, different codec
      3  same content, anything else (last resort)
    Lower tiers are tried first.
    """
    if not _fs._same_content(primary, candidate):
        return None
    same_res = _fs._same_meta_value(primary, candidate, "resolution")
    same_codec = _fs._same_meta_value(primary, candidate, "codec")
    same_group = _fs._same_meta_value(primary, candidate, "group")

    if same_res and same_codec:
        if same_group and _fs._release_size_within(
            primary, candidate, _fs._TIER0_SIZE_FRACTION
        ):
            return 0
        return 1
    if same_res:
        return 2
    return 3


def _same_meta_value(primary, candidate, key):
    """Return whether both releases share the same non-empty metadata value."""
    value = _fs._meta_value(primary, key)
    return bool(value) and value == _fs._meta_value(candidate, key)


def _release_size_bytes(result):
    """Return the best-known release size: manifest group bytes or indexer size."""
    manifest_bytes = _fs._manifest_group_bytes(result)
    if manifest_bytes > 0:
        return manifest_bytes
    return _fs._result_indexer_size(result)


def _release_size_within(primary, candidate, fraction):
    """Return whether two releases' known sizes are within ``fraction``."""
    primary_size = _fs._release_size_bytes(primary)
    candidate_size = _fs._release_size_bytes(candidate)
    if primary_size <= 0 or candidate_size <= 0:
        return True
    return abs(primary_size - candidate_size) <= primary_size * fraction


def _result_meta(result):
    """Return parsed title metadata, deriving it when the caller has raw results."""
    if not isinstance(result, dict):
        return {}
    meta = result.get("_meta")
    if isinstance(meta, dict):
        return meta
    try:
        from resources.lib.filter import parse_title_metadata

        meta = parse_title_metadata(result.get("title", ""))
    except Exception:  # pylint: disable=broad-except
        meta = {}
    if isinstance(meta, dict):
        result["_meta"] = meta
        return meta
    return {}


def _meta_value(result, key):
    """Return a normalized metadata string from a result."""
    return _fs._meta_value_from_meta(_fs._result_meta(result), key)


def _meta_value_from_meta(meta, key):
    """Return a normalized metadata string from an existing metadata dict."""
    if not isinstance(meta, dict):
        return ""
    value = meta.get(key, "")
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _meta_values_from_meta(meta, key):
    """Return normalized metadata list values from an existing metadata dict."""
    if not isinstance(meta, dict):
        return []
    value = meta.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _meta_bool_from_meta(meta, key):
    """Return a normalized boolean flag from an existing metadata dict."""
    if not isinstance(meta, dict):
        return False
    value = meta.get(key, False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


_QUALITY_FAMILY_MARKERS = (
    (("remux",), "remux"),
    (("web dl", "webdl"), "web-dl"),
    (("webrip", "web rip"), "webrip"),
    (("hdtv",), "hdtv"),
    (("bluray", "blu ray", "bdrip", "uhd"), "bluray"),
)


def _quality_family(value):
    """Collapse source labels that describe the same fallback-safe family."""
    text = _fs._normalize_title(value)
    for markers, family in _fs._QUALITY_FAMILY_MARKERS:
        if any(marker in text for marker in markers):
            return family
    return text


_TITLE_STOP_TOKENS = frozenset(
    (
        "2160p",
        "1080p",
        "720p",
        "480p",
        "ac3",
        "a",
        "an",
        "and",
        "atmos",
        "avc",
        "bluray",
        "ddp",
        "dovi",
        "dts",
        "dv",
        "group",
        "grp",
        "hdr",
        "hdr10",
        "hevc",
        "h264",
        "h265",
        "remux",
        "the",
        "uhd",
        "web",
        "webdl",
        "x264",
        "x265",
    )
)


_TITLE_TOKEN_CACHE_TITLE_KEY = "_fallback_title_tokens_title"  # nosec B105 — cache key


_TITLE_TOKEN_CACHE_VALUE_KEY = "_fallback_title_tokens"  # nosec B105 — cache key


def _is_content_title_token(token):
    """Return whether a normalized title token identifies content (not noise)."""
    if token in _fs._TITLE_STOP_TOKENS:
        return False
    return len(token) > 1 or token.isdigit()


def _title_tokens(result):
    """Return content-identifying title tokens for lenient fallback matching."""
    title = result.get("title", "") if isinstance(result, dict) else ""
    cached = _fs._cached_title_tokens(result)
    if cached is not None:
        return cached

    token_set = frozenset(
        token
        for token in _fs._normalize_title(title).split()
        if _fs._is_content_title_token(token)
    )
    if isinstance(result, dict):
        result[_fs._TITLE_TOKEN_CACHE_TITLE_KEY] = title
        result[_fs._TITLE_TOKEN_CACHE_VALUE_KEY] = token_set
    return token_set


def _cached_title_tokens(result):
    """Return already-computed title tokens for this exact title, if present."""
    if not isinstance(result, dict):
        return None
    title = result.get("title", "")
    cached_tokens = result.get(_fs._TITLE_TOKEN_CACHE_VALUE_KEY)
    if result.get(_fs._TITLE_TOKEN_CACHE_TITLE_KEY) == title and isinstance(
        cached_tokens, (frozenset, set)
    ):
        return cached_tokens
    return None


def _titles_look_related(primary, candidate):
    """Return whether release titles overlap enough to be plausible reposts."""
    left = _fs._title_tokens(primary)
    right = _fs._title_tokens(candidate)
    return _fs._title_token_sets_look_related(left, right)


def _title_token_sets_look_related(left, right):
    """Return whether two precomputed title-token sets look related."""
    if not left or not right:
        return True
    overlap = left.intersection(right)
    needed = 1 if min(len(left), len(right)) <= 2 else 2
    return len(overlap) >= needed
