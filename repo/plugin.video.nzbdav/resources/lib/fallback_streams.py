# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Conservative grouping for duplicate releases usable as fallback streams."""

import copy
import hashlib
import os
import re
import threading
import time
from collections import namedtuple
from functools import lru_cache
from queue import Empty, Queue
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

try:
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover - Kodi installs may not bundle defusedxml
    from xml.etree import ElementTree as ET

import xbmc
import xbmcaddon

from resources.lib import telemetry
from resources.lib.http_util import pubdate_to_epoch
from resources.lib.nzb_manifest import fetch_nzb_video_manifest, make_empty_manifest


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every HTTP redirect so origin pinning isn't bypassed.

    The fingerprint probes pin the request URL to the configured-origin
    allow-list via ``_validated_probe_url``, but a vanilla
    ``urlopen`` opener follows up to 10 redirects — a 302 to a
    different origin would silently bypass the allow-list, and on
    Python <3.11 the Authorization header even leaks across redirects.
    Raising ``HTTPError`` on the 3xx surfaces the redirect as a probe
    failure (None / 0 / empty digest) at every call site.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, msg, headers, fp)


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def _no_redirect_urlopen(req, timeout):
    """Open ``req`` without following any HTTP redirect.

    Centralized helper so the fingerprint probe path
    (``fetch_content_length`` / ``fetch_range_bytes`` /
    ``fetch_range_digest``) always uses the same allow-list-respecting
    opener instead of the redirect-following module-level ``urlopen``.

    Routes through the module-level ``urlopen`` symbol so existing
    tests that patch ``resources.lib.fallback_streams.urlopen`` keep
    working unchanged. In production the symbol is rebound below to
    the no-redirect opener's ``open`` method, so production traffic
    gets redirect rejection while tests intercept as before.
    """
    # nosemgrep
    return urlopen(req, timeout=timeout)  # nosec B310


# Rebind ``urlopen`` to the no-redirect opener's ``open`` so production
# code refuses HTTP redirects on every fingerprint probe. Tests that
# ``patch("resources.lib.fallback_streams.urlopen", ...)`` swap this
# rebound symbol — patch shape is unchanged. The rebinding is at
# import time so there is no thread-safety concern.
urlopen = _NO_REDIRECT_OPENER.open  # noqa: F811

_SAFE_JOB_RE = re.compile(r"^[A-Za-z0-9._ \[\]-]+$")
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")
_NON_WORD_RE = re.compile(r"[\W_]+")
# Conjunction words that name the same work whether spelled out, written as
# "&", or omitted entirely ("Friends & Neighbors" / "Friends and Neighbors" /
# "Friends Neighbors", "Jules et Jim" / "Jules Jim"). ``_normalize_title`` drops
# these as standalone tokens so all spellings share one content identity. Kept
# to the few high-confidence forms: English "and", French "et", German "und".
# Single-letter conjunctions (Spanish "y"/"e", Polish "i") are excluded -- they
# collide with stray single-character junk tokens and would over-collapse.
_CONJUNCTION_TOKENS = frozenset(("and", "et", "und"))
_INVALID_TITLE_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_FINGERPRINT_SAMPLE_COUNT = 100
_FINGERPRINT_SMALL_SAMPLE_COUNT = 20
_FINGERPRINT_DENSE_SAMPLE_MIN_BYTES = 1024 * 1024 * 1024
_FINGERPRINT_BYTES = 4096

_MAX_FALLBACKS = 5
# Two fallback candidates whose Usenet post dates fall within this window are
# treated as the same upload re-listed and collapsed to one (highest tier kept).
# A candidate within this window of the primary's post date is the same upload
# as the primary and is dropped — the primary cannot be its own backup. The
# bound is inclusive: exactly 1h apart counts as the same article.
_SAME_POST_WINDOW_SECONDS = 3600
_FALLBACK_MANIFEST_STALL_SPECULATION_SECONDS = 0.05
_FALLBACK_MANIFEST_OPTIONAL_TAIL_WAIT_SECONDS = 0.1
_FALLBACK_MANIFEST_CACHE_TTL_SECONDS = 10.0
_FALLBACK_MANIFEST_CACHE_MAX_ENTRIES = 128
_ALLOWED_STREAM_SCHEMES = frozenset(("http", "https"))
_METADATA_ONLY_MANIFEST_REASONS = frozenset(("too_large",))
_ADDON_SETTINGS_SCHEMA = (
    "special://home/addons/plugin.video.nzbdav/resources/settings.xml"
)
_INDEXER_SIZE_SYNTHETIC_MANIFEST_REASONS = frozenset(("invalid_xml", "no_video_file"))
_INDEXER_SIZE_SYNTHETIC_MIN_BYTES = 100 * 1024 * 1024
_PrecomputedProbeBase = namedtuple("_PrecomputedProbeBase", "parts origin path")
_FALLBACK_MANIFEST_CACHE = {}
_FALLBACK_MANIFEST_CACHE_LOCK = threading.Lock()


def _fallback_manifest_cache_now():
    return time.monotonic()


def clear_fallback_manifest_cache():
    """Clear the short-lived fallback manifest cache."""
    with _FALLBACK_MANIFEST_CACHE_LOCK:
        _FALLBACK_MANIFEST_CACHE.clear()


def _fallback_manifest_cache_key(link):
    return fetch_nzb_video_manifest, link


def _copy_manifest(manifest):
    return copy.deepcopy(manifest) if isinstance(manifest, dict) else manifest


def _cached_fallback_manifest(link, now):
    key = _fallback_manifest_cache_key(link)
    with _FALLBACK_MANIFEST_CACHE_LOCK:
        cached = _FALLBACK_MANIFEST_CACHE.get(key)
        if cached is None:
            return None
        expires_at, manifest = cached
        if expires_at <= now:
            _FALLBACK_MANIFEST_CACHE.pop(key, None)
            return None
        return _copy_manifest(manifest)


def _store_fallback_manifest(link, manifest, now):
    if not isinstance(manifest, dict):
        return
    key = _fallback_manifest_cache_key(link)
    expires_at = now + _FALLBACK_MANIFEST_CACHE_TTL_SECONDS
    with _FALLBACK_MANIFEST_CACHE_LOCK:
        if len(_FALLBACK_MANIFEST_CACHE) >= _FALLBACK_MANIFEST_CACHE_MAX_ENTRIES:
            expired = [
                cache_key
                for cache_key, (cached_expires_at, _manifest) in (
                    _FALLBACK_MANIFEST_CACHE.items()
                )
                if cached_expires_at <= now
            ]
            for cache_key in expired:
                _FALLBACK_MANIFEST_CACHE.pop(cache_key, None)
            if len(_FALLBACK_MANIFEST_CACHE) >= _FALLBACK_MANIFEST_CACHE_MAX_ENTRIES:
                oldest_key = min(
                    _FALLBACK_MANIFEST_CACHE,
                    key=lambda cache_key: _FALLBACK_MANIFEST_CACHE[cache_key][0],
                )
                _FALLBACK_MANIFEST_CACHE.pop(oldest_key, None)
        _FALLBACK_MANIFEST_CACHE[key] = (expires_at, _copy_manifest(manifest))


def _fetch_fallback_manifest(link):
    now = _fallback_manifest_cache_now()
    manifest = _cached_fallback_manifest(link, now)
    if isinstance(manifest, dict):
        return manifest
    try:
        manifest = fetch_nzb_video_manifest(
            link, health_check=_manifest_candidate_message_ids_are_healthy
        )
    except Exception:  # pylint: disable=broad-except
        manifest = _manifest_error("fetch_error")
    if not isinstance(manifest, dict):
        manifest = _manifest_error("fetch_error")
    _store_fallback_manifest(link, manifest, _fallback_manifest_cache_now())
    return _copy_manifest(manifest)


def _setting_bool(addon, key, default=False):
    """Read a Kodi boolean setting with a safe fallback."""
    raw = addon.getSetting(key)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized == "":
            return bool(default)
        if normalized in ("false", "0", "no", "off"):
            return False
    return bool(default)


def _setting_int(addon, key, default=0):
    """Read a Kodi integer setting with a safe fallback."""
    raw = addon.getSetting(key)
    try:
        return int(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return int(default)


def _split_http_url(url):
    """Parse a URL and return parts only for simple HTTP(S) URLs.

    Returns the parsed ``SplitResult`` on accept, ``None`` on reject —
    NOT ``False``. Returning a bool here was a contract drift; callers
    use both truthiness (``if parts:``) and identity checks
    (``parts is None``) and the latter silently miss-classified rejected
    URLs as valid.
    """
    if not isinstance(url, str) or any(ord(char) < 0x20 for char in url):
        return None
    # IDN homograph / RTL-override / full-width digit attacks: any byte
    # > 0x7F could let an eyeball-identical hostname slip past the
    # configured-origin allow-list. Reject high-bit input outright;
    # operators who need IDN should pre-IDNA-encode the stream base.
    if any(ord(char) > 0x7F for char in url):
        return None
    try:
        parts = urlsplit(url)
        if not _http_url_parts_are_valid(parts):
            return None
    except ValueError:
        return None
    return parts


def _http_url_parts_are_valid(parts):
    """Return whether parsed URL parts are a simple, safe HTTP(S) location.

    Accessing ``parts.port`` may raise ``ValueError`` for a malformed port;
    that propagates to the caller's ``except ValueError`` exactly as before.
    """
    if parts.scheme.lower() not in _ALLOWED_STREAM_SCHEMES:
        return False
    if not parts.netloc or not parts.hostname:
        return False
    # urlsplit accepts whitespace inside netloc (e.g. "http:// host /")
    # — `parts.hostname` silently strips it, masking a malformed URL
    # that would never resolve. Reject any whitespace in the raw
    # netloc explicitly.
    if any(ch.isspace() for ch in parts.netloc):
        return False
    if parts.username or parts.password:
        return False
    # Accessing .port validates that any explicit port is numeric/ranged.
    _port = parts.port
    return True


def _origin_key(parts):
    """Return the normalized origin tuple for parsed URL parts."""
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parts.hostname.lower(), port


def _path_is_under_base(path, base_path):
    """Return whether a URL path is within the configured base path."""
    prefix = (base_path or "").rstrip("/")
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


def _schema_setting_default(setting_id):
    """Read a default from resources/settings.xml without Kodi settings APIs."""
    try:
        import xbmcvfs

        schema_path = xbmcvfs.translatePath(_ADDON_SETTINGS_SCHEMA)
    except Exception:  # pylint: disable=broad-except
        schema_path = ""
    if not isinstance(schema_path, str):
        schema_path = ""
    candidate_paths = []
    if schema_path:
        candidate_paths.append(schema_path)
    candidate_paths.append(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "settings.xml"))
    )
    for path in candidate_paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        default = _setting_default_from_root(root, setting_id)
        if default is not None:
            return default
    return ""


def _setting_default_from_root(root, setting_id):
    """Return a setting's default value from a parsed settings.xml root, or None."""
    for setting in root.findall(".//setting"):
        if setting.get("id") == setting_id:
            return setting.get("default", "") or ""
    return None


def _configured_stream_bases():
    """Return configured WebDAV/nzbdav bases that fallback probes may hit.

    Reads from settings.xml on disk first — calling addon.getSetting()
    from a background prevalidation thread (script-mode invocation)
    SIGSEGVs in the Kodi C++ binding the same way webdav.py /
    stream_proxy.py did before we switched their reads to disk."""
    raw_bases = ()
    try:
        from resources.lib.router import _get_script_setting

        raw_bases = (
            _get_script_setting("webdav_url", ""),
            _get_script_setting("nzbdav_url", ""),
        )
    except Exception:  # pylint: disable=broad-except
        raw_bases = ()
    # If the script-mode settings.xml read returned nothing (no profile
    # yet, or addon hasn't saved settings), fall back to the addon API.
    # In script-mode this is the very SIGSEGV path we're trying to avoid,
    # but with raw_bases populated from disk above we never get here in
    # the script-mode case — only on a fresh GUI invocation where the
    # binding is safe.
    if not any(raw_bases):
        try:
            addon = xbmcaddon.Addon("plugin.video.nzbdav")
            raw_bases = (
                addon.getSetting("webdav_url"),
                addon.getSetting("nzbdav_url"),
            )
        except Exception:  # pylint: disable=broad-except
            raw_bases = ()
    if not any(raw_bases):
        raw_bases = (
            _schema_setting_default("webdav_url"),
            _schema_setting_default("nzbdav_url"),
        )

    bases = []
    for raw_base in raw_bases:
        # .strip() (not just .rstrip("/")): a stray trailing space in the
        # configured nzbdav_url/webdav_url (a common copy-paste artifact) lands
        # inside the netloc, and _split_http_url rejects any whitespace there —
        # silently emptying the probe-base allow-list so fallback content-length
        # probes all return 0 and byte-identical fallbacks fail validation.
        parts = _split_http_url(str(raw_base or "").strip().rstrip("/"))
        if parts:
            bases.append(parts)
    return bases


def configured_stream_probe_bases():
    """Return configured stream bases with reusable origin/path checks."""
    return tuple(
        _PrecomputedProbeBase(parts, _origin_key(parts), parts.path or "/")
        for parts in _configured_stream_bases()
    )


def _probe_base_components(base):
    """Return parsed URL parts plus cached origin/path data for one base."""
    if isinstance(base, _PrecomputedProbeBase):
        return base.parts, base.origin, base.path
    return base, _origin_key(base), base.path or "/"


def _validated_probe_url(url, probe_bases=None):
    """Return a probe URL constrained to the configured WebDAV origin."""
    candidate = _split_http_url(url)
    if not candidate:
        return None
    bases = _configured_stream_bases() if probe_bases is None else probe_bases
    candidate_origin = _origin_key(candidate)
    for base in bases:
        base_parts, base_origin, base_path = _probe_base_components(base)
        if candidate_origin != base_origin:
            continue
        if not _path_is_under_base(candidate.path or "/", base_path):
            continue
        return urlunsplit(
            (
                base_parts.scheme.lower(),
                base_parts.netloc,
                candidate.path or "/",
                candidate.query,
                "",
            )
        )
    return None


@lru_cache(maxsize=256)
def _cached_validated_probe_url(url, probe_bases):
    """Return a configured-origin probe URL for immutable base snapshots."""
    return _validated_probe_url(url, probe_bases=probe_bases)


def _validated_probe_url_for_fetch(url, probe_bases=None):
    """Return a probe URL, caching validation when bases are immutable."""
    if probe_bases is None:
        return _validated_probe_url(url)
    try:
        return _cached_validated_probe_url(url, tuple(probe_bases))
    except TypeError:
        return _validated_probe_url(url, probe_bases=probe_bases)


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
    normalized = _NON_WORD_RE.sub(" ", lowered)
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
        if not (0 < index < last and token in _CONJUNCTION_TOKENS)
    )


# Ordinal words PTT keeps inside a movie title (e.g. "Dune Part Two",
# "John Wick Chapter 4"). A movie's part/chapter number is a content
# discriminator, not an encode attribute, so the content-identity gate
# must treat "Part Two" and "Part One" as different content.
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
# Trailing sequel discriminators PTT leaves inside the title ("Rocky IV",
# "Rambo III", "Iron Man Three"). Mirror the numeric-tail reject for these.
# MULTI-CHARACTER tokens ONLY: single-letter romans i/v/x collide with junk
# suffixes (a stray "v"/"x"), and "one" is never a real sequel tail, so they
# are deliberately excluded to avoid false-rejecting legit junk-suffix reposts.
# Ordinal WORDS stop at "ten" (no "eleven"+) so titles like "Ocean's Eleven"
# are not affected.
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
    match = _PART_LABEL_RE.search(title)
    if not match:
        return 0
    token = match.group(1).strip().lower()
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return 0
    return _PART_ORDINAL_WORDS.get(token, 0)


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
        return (_normalize_title(raw), 0, (), (), 0)
    title = result.get("title", "")
    cached = result.get(_RELEASE_IDENTITY_CACHE_VALUE_KEY)
    if result.get(_RELEASE_IDENTITY_CACHE_TITLE_KEY) == title and isinstance(
        cached, tuple
    ):
        return cached
    parsed = _parsed_title_fields(title)
    norm_title = _normalize_title(parsed.get("title") or title)
    seasons = _sorted_int_tuple(parsed.get("seasons"))
    episodes = _sorted_int_tuple(parsed.get("episodes"))
    part = _part_number_from_title(title)
    identity = (norm_title, parsed["year"], seasons, episodes, part)
    result[_RELEASE_IDENTITY_CACHE_TITLE_KEY] = title
    result[_RELEASE_IDENTITY_CACHE_VALUE_KEY] = identity
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
        if extra == 1 and (longer[-1].isdigit() or longer[-1] in _SEQUEL_TAIL_TOKENS):
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
        return _subset_titles_related(
            left, right, left_tokens, right_tokens, corroborated
        )
    return _disjoint_titles_related(left, right, corroborated)


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
    return _title_token_sets_look_related(left, right)


def _content_discriminators_match(primary, candidate):
    """Return whether two same-titled releases are the same *cut* of the work.

    Edition (Theatrical vs Extended/Director's) and PROPER/REPACK status are
    content discriminators: a Theatrical encode is not a valid fallback for an
    Extended encode even though title/year match. Resolution, codec, group,
    HDR, and audio are deliberately *not* checked here — those only affect the
    fallback tier, not whether the candidate is the same content.
    """
    primary_meta = _result_meta(primary)
    candidate_meta = _result_meta(candidate)
    left_edition = _normalize_title(_meta_value_from_meta(primary_meta, "edition"))
    right_edition = _normalize_title(_meta_value_from_meta(candidate_meta, "edition"))
    if left_edition != right_edition:
        return False
    for key in ("proper", "repack", "upscaled"):
        if _meta_bool_from_meta(primary_meta, key) != _meta_bool_from_meta(
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
    if not _episode_set_pair_matches(primary_seasons, candidate_seasons):
        return False
    # An episode must not peer with a different episode that simply omitted
    # its SxxExx tokens; require both to carry the same episode evidence.
    if not _episode_set_pair_matches(primary_episodes, candidate_episodes):
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
    if not _content_discriminators_match(primary, candidate):
        return False
    primary_identity = _release_identity(primary)
    candidate_identity = _release_identity(candidate)
    primary_title, primary_year, _, _, primary_part = primary_identity
    candidate_title, candidate_year, _, _, candidate_part = candidate_identity

    corroborated = _identity_corroborated(primary_identity, candidate_identity)
    if not _titles_core_related(
        primary_title, candidate_title, corroborated=corroborated
    ):
        return False

    # Part/chapter number is a content discriminator (Part One vs Part Two).
    if primary_part and candidate_part and primary_part != candidate_part:
        return False

    primary_seasons, candidate_seasons = _collapse_phantom_season(
        primary_identity, candidate_identity
    )
    return _same_content_seasonal_tail(
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
    if _identity_is_episodic(primary_identity) or _identity_is_episodic(
        candidate_identity
    ):
        return _episode_content_matches(primary_identity, candidate_identity)

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
    if not _same_content(primary, candidate):
        return None
    same_res = _same_meta_value(primary, candidate, "resolution")
    same_codec = _same_meta_value(primary, candidate, "codec")
    same_group = _same_meta_value(primary, candidate, "group")

    if same_res and same_codec:
        if same_group and _release_size_within(
            primary, candidate, _TIER0_SIZE_FRACTION
        ):
            return 0
        return 1
    if same_res:
        return 2
    return 3


def _same_meta_value(primary, candidate, key):
    """Return whether both releases share the same non-empty metadata value."""
    value = _meta_value(primary, key)
    return bool(value) and value == _meta_value(candidate, key)


def _release_size_bytes(result):
    """Return the best-known release size: manifest group bytes or indexer size."""
    manifest_bytes = _manifest_group_bytes(result)
    if manifest_bytes > 0:
        return manifest_bytes
    return _result_indexer_size(result)


def _release_size_within(primary, candidate, fraction):
    """Return whether two releases' known sizes are within ``fraction``."""
    primary_size = _release_size_bytes(primary)
    candidate_size = _release_size_bytes(candidate)
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
    return _meta_value_from_meta(_result_meta(result), key)


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
    text = _normalize_title(value)
    for markers, family in _QUALITY_FAMILY_MARKERS:
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
_PREFETCH_PROOF_KEY = "_fallback_prefetch_gate_proof"
_SELECTION_POOL_FIRST_PEER_KEY = "_fallback_selection_pool_first_peer"
FALLBACK_CANDIDATES_DISABLED = object()


def _is_content_title_token(token):
    """Return whether a normalized title token identifies content (not noise)."""
    if token in _TITLE_STOP_TOKENS:
        return False
    return len(token) > 1 or token.isdigit()


def _title_tokens(result):
    """Return content-identifying title tokens for lenient fallback matching."""
    title = result.get("title", "") if isinstance(result, dict) else ""
    cached = _cached_title_tokens(result)
    if cached is not None:
        return cached

    token_set = frozenset(
        token
        for token in _normalize_title(title).split()
        if _is_content_title_token(token)
    )
    if isinstance(result, dict):
        result[_TITLE_TOKEN_CACHE_TITLE_KEY] = title
        result[_TITLE_TOKEN_CACHE_VALUE_KEY] = token_set
    return token_set


def _cached_title_tokens(result):
    """Return already-computed title tokens for this exact title, if present."""
    if not isinstance(result, dict):
        return None
    title = result.get("title", "")
    cached_tokens = result.get(_TITLE_TOKEN_CACHE_VALUE_KEY)
    if result.get(_TITLE_TOKEN_CACHE_TITLE_KEY) == title and isinstance(
        cached_tokens, (frozenset, set)
    ):
        return cached_tokens
    return None


def _titles_look_related(primary, candidate):
    """Return whether release titles overlap enough to be plausible reposts."""
    left = _title_tokens(primary)
    right = _title_tokens(candidate)
    return _title_token_sets_look_related(left, right)


def _title_token_sets_look_related(left, right):
    """Return whether two precomputed title-token sets look related."""
    if not left or not right:
        return True
    overlap = left.intersection(right)
    needed = 1 if min(len(left), len(right)) <= 2 else 2
    return len(overlap) >= needed


def _metadata_profile_signature(meta):
    """Return the title/profile fields covered by the fallback prefetch gate."""
    return (
        _meta_value_from_meta(meta, "resolution"),
        _meta_value_from_meta(meta, "codec"),
        _meta_value_from_meta(meta, "container"),
        _normalize_title(_meta_value_from_meta(meta, "edition")),
        _meta_bool_from_meta(meta, "proper"),
        _meta_bool_from_meta(meta, "repack"),
        _meta_bool_from_meta(meta, "upscaled"),
        _quality_family(_meta_value_from_meta(meta, "quality")),
        tuple(sorted(set(_meta_values_from_meta(meta, "hdr")))),
        tuple(sorted(set(_meta_values_from_meta(meta, "audio")))),
        _meta_value_from_meta(meta, "channels"),
    )


def _prefetch_gate_proof(primary, candidate, primary_meta=None, candidate_meta=None):
    """Return a stable proof key for an already-passed title/profile gate."""
    if not isinstance(primary, dict) or not isinstance(candidate, dict):
        return None
    if primary_meta is None:
        primary_meta = primary.get("_meta")
    if candidate_meta is None:
        candidate_meta = candidate.get("_meta")
    if not isinstance(primary_meta, dict) or not isinstance(candidate_meta, dict):
        return None
    return (
        primary.get("link", ""),
        primary.get("title", ""),
        candidate.get("link", ""),
        candidate.get("title", ""),
        _metadata_profile_signature(primary_meta),
        _metadata_profile_signature(candidate_meta),
    )


def _remember_prefetch_gate_match(
    primary, candidate, primary_meta=None, candidate_meta=None
):
    """Store proof that the candidate passed the fallback title/profile gate."""
    proof = _prefetch_gate_proof(
        primary, candidate, primary_meta=primary_meta, candidate_meta=candidate_meta
    )
    if proof is not None:
        candidate[_PREFETCH_PROOF_KEY] = proof


def _has_prefetch_gate_match(primary, candidate):
    """Return whether a candidate still matches a prior prefetch-gate proof."""
    if not isinstance(primary, dict) or not isinstance(candidate, dict):
        return False
    proof = candidate.get(_PREFETCH_PROOF_KEY)
    if not isinstance(proof, tuple) or len(proof) != 6:
        return False
    current_identity = (
        primary.get("link", ""),
        primary.get("title", ""),
        candidate.get("link", ""),
        candidate.get("title", ""),
    )
    if proof[:4] != current_identity:
        return False
    return proof == _prefetch_gate_proof(primary, candidate)


def _remember_selection_pool_first_peer(selected, results, peer):
    """Store the first distinct peer found during a selection-pool scan."""
    if isinstance(selected, dict) and isinstance(peer, dict):
        selected[_SELECTION_POOL_FIRST_PEER_KEY] = (
            id(results),
            selected.get("link", ""),
            peer,
        )


def cached_selection_pool_first_peer(selected, results):
    """Return the first distinct peer found by a matching pool scan."""
    if not isinstance(selected, dict):
        return None
    cached = selected.get(_SELECTION_POOL_FIRST_PEER_KEY)
    if not isinstance(cached, tuple) or len(cached) != 3:
        return None
    results_id, selected_link, peer = cached
    if results_id != id(results) or selected_link != selected.get("link", ""):
        return None
    if not isinstance(peer, dict):
        return None
    return peer


def _multi_result_pool_has_no_distinct_peer(selected, results):
    """Return True when a multi-result pool has no distinct fallback peer."""
    if not isinstance(selected, dict):
        return False
    selected_link = selected.get("link", "")
    try:
        for result in results:
            if result is selected or not isinstance(result, dict):
                continue
            result_link = result.get("link", "")
            if result_link and result_link != selected_link:
                _remember_selection_pool_first_peer(selected, results, result)
                return False
    except TypeError:
        return False
    return True


def _single_result_pool_has_no_distinct_peer(selected, results):
    """Return True when a one-result pool has no distinct fallback peer."""
    try:
        only_result = results[0]
    except (IndexError, KeyError, TypeError):
        return False
    if only_result is selected:
        return True
    if not isinstance(selected, dict) or not isinstance(only_result, dict):
        return False
    only_link = only_result.get("link", "")
    selected_link = selected.get("link", "")
    if only_link and (not selected_link or only_link != selected_link):
        _remember_selection_pool_first_peer(selected, results, only_result)
        return False
    return True


def _sized_pool_has_no_distinct_peer(selected, results):
    """Return True when a sized pool cannot contain any fallback peer."""
    try:
        result_count = len(results)
    except TypeError:
        return False
    if result_count == 0:
        return True
    if result_count != 1:
        return _multi_result_pool_has_no_distinct_peer(selected, results)
    return _single_result_pool_has_no_distinct_peer(selected, results)


def _prefetch_peer_state(selected):
    """Return mutable [tokens, meta, meta_ready] scan state for a selected result."""
    selected_meta = (
        selected.get("_meta") if isinstance(selected.get("_meta"), dict) else None
    )
    return [None, selected_meta, selected_meta is not None]


def _prefetch_peer_tokens(selected, state):
    """Return cached selected title tokens, computing them once on demand."""
    if state[0] is None:
        state[0] = _title_tokens(selected)
    return state[0]


def _prefetch_peer_meta(selected, state):
    """Return the selected metadata dict, deriving + caching it on demand."""
    if not state[2]:
        state[1] = _result_meta(selected)
        state[2] = True
    return state[1]


def _prefetch_peer_match(selected, result, candidate_meta, state):
    """Return ``result`` when it passes the full prefetch gate, else ``None``.

    Mirrors the original per-candidate branch structure exactly: the cheap
    profile gate runs first only when both metadata dicts are ready; otherwise
    the title prefilter runs before the profile parse.
    """
    if state[2] and candidate_meta is not None:
        return _prefetch_peer_match_meta_ready(selected, result, candidate_meta, state)

    tokens = _prefetch_peer_tokens(selected, state)
    if not _title_token_sets_look_related(tokens, _title_tokens(result)):
        return None
    return _prefetch_peer_match_title_first(selected, result, candidate_meta, state)


def _prefetch_peer_match_meta_ready(selected, result, candidate_meta, state):
    """Cheap-profile-first prefetch gate when both metadata dicts are ready."""
    if not _metadata_profiles_match(
        selected,
        result,
        primary_meta=state[1],
        candidate_meta=candidate_meta,
        require_same_group=True,
    ):
        return None
    tokens = _prefetch_peer_tokens(selected, state)
    if _title_token_sets_look_related(tokens, _title_tokens(result)) and _same_content(
        selected, result
    ):
        _remember_prefetch_gate_match(selected, result, state[1], candidate_meta)
        return result
    return None


def _prefetch_peer_match_title_first(selected, result, candidate_meta, state):
    """Prefetch gate tail after the title prefilter already passed."""
    selected_meta = _prefetch_peer_meta(selected, state)
    if candidate_meta is None:
        resolved_meta = result.get("_meta")
        if not isinstance(resolved_meta, dict):
            resolved_meta = None
    else:
        resolved_meta = candidate_meta
    if _metadata_profiles_match(
        selected,
        result,
        primary_meta=selected_meta,
        candidate_meta=candidate_meta,
        require_same_group=True,
    ) and _same_content(selected, result):
        _remember_prefetch_gate_match(selected, result, selected_meta, resolved_meta)
        return result
    return None


def first_prefetchable_fallback_peer(
    selected, results, distinct_peer_already_checked=False
):
    """Return the first distinct result that can pass the prefetch gate."""
    if not isinstance(selected, dict):
        return None
    if not distinct_peer_already_checked and _sized_pool_has_no_distinct_peer(
        selected, results
    ):
        return None
    state = _prefetch_peer_state(selected)
    seen_links = {selected.get("link", "")}
    for result in results or []:
        if not _is_distinct_dict_peer(result, selected, seen_links):
            continue
        match = _prefetch_peer_match(
            selected, result, _result_meta_or_none(result), state
        )
        if match is not None:
            return match
    return None


def _result_meta_or_none(result):
    """Return a result's already-computed ``_meta`` dict, or None."""
    meta = result.get("_meta")
    return meta if isinstance(meta, dict) else None


def _is_distinct_dict_peer(result, selected, seen_links):
    """Return whether ``result`` is a distinct dict peer with a usable link."""
    if not isinstance(result, dict) or result is selected:
        return False
    candidate_link = result.get("link", "")
    return bool(candidate_link) and candidate_link not in seen_links


def _metadata_profiles_match(
    primary, candidate, primary_meta=None, candidate_meta=None, require_same_group=False
):
    """Return whether two releases are plausible same-file fallback peers.

    This is intentionally looser than manifest equality. The stream proxy still
    verifies content length and sampled byte fingerprints before switching to a
    fallback source, so this stage should gather plausible peers instead of
    rejecting reposts because their NZB subject used a different filename.

    ``require_same_group`` adds the user-requested same-release-group gate: a
    backup must come from the SAME group as the primary, because a different
    group's encode is a different file that can never byte-match for a seamless
    cutover. Both groups must be parsed and equal (fail closed on an unknown
    group). The check reuses the metadata already computed here, so it adds no
    extra title-metadata parses and runs only after the cheap title prefilter.
    """
    if primary_meta is None:
        primary_meta = _result_meta(primary)
    if candidate_meta is None:
        candidate_meta = _result_meta(candidate)
    if require_same_group and not _same_group_resolution_gate(
        primary_meta, candidate_meta
    ):
        return False
    if not _shared_profile_fields_match(primary_meta, candidate_meta):
        return False
    return _hdr_audio_channels_match(primary_meta, candidate_meta)


def _same_group_resolution_gate(primary_meta, candidate_meta):
    """Return whether group + resolution fail-closed gates pass for both sides."""
    left_group = _meta_value_from_meta(primary_meta, "group")
    right_group = _meta_value_from_meta(candidate_meta, "group")
    if not left_group or left_group != right_group:
        return False
    # require same RESOLUTION too (user requirement): fail CLOSED like the
    # group gate -- reject when either side's resolution is unparsed or
    # differs, so an unparsed-resolution candidate can never slip a
    # different-resolution encode past the gate. The shared resolution
    # check below only fails OPEN when one side is unknown, so this stricter
    # gate is what enforces "same resolution as parsed by PTT".
    left_res = _meta_value_from_meta(primary_meta, "resolution")
    right_res = _meta_value_from_meta(candidate_meta, "resolution")
    if not left_res or left_res != right_res:
        return False
    return True


def _meta_string_fields_match(primary_meta, candidate_meta, keys):
    """Return whether each metadata string field matches when both are known."""
    for key in keys:
        left = _meta_value_from_meta(primary_meta, key)
        right = _meta_value_from_meta(candidate_meta, key)
        if left and right and left != right:
            return False
    return True


def _meta_bool_flags_match(primary_meta, candidate_meta, keys):
    """Return whether each metadata boolean flag is identical on both sides."""
    for key in keys:
        if _meta_bool_from_meta(primary_meta, key) != _meta_bool_from_meta(
            candidate_meta, key
        ):
            return False
    return True


def _shared_profile_fields_match(primary_meta, candidate_meta):
    """Return whether res/codec/container, edition, flags, and quality match."""
    if not _meta_string_fields_match(
        primary_meta, candidate_meta, ("resolution", "codec", "container")
    ):
        return False

    left_edition = _normalize_title(_meta_value_from_meta(primary_meta, "edition"))
    right_edition = _normalize_title(_meta_value_from_meta(candidate_meta, "edition"))
    if left_edition != right_edition:
        return False

    if not _meta_bool_flags_match(
        primary_meta, candidate_meta, ("proper", "repack", "upscaled")
    ):
        return False

    left_quality = _quality_family(_meta_value_from_meta(primary_meta, "quality"))
    right_quality = _quality_family(_meta_value_from_meta(candidate_meta, "quality"))
    if left_quality and right_quality and left_quality != right_quality:
        return False
    return True


def _hdr_audio_channels_match(primary_meta, candidate_meta):
    """Return whether HDR, audio, and channel metadata are compatible."""
    left_hdr = set(_meta_values_from_meta(primary_meta, "hdr"))
    right_hdr = set(_meta_values_from_meta(candidate_meta, "hdr"))
    if left_hdr != right_hdr:
        return False

    left_audio = set(_meta_values_from_meta(primary_meta, "audio"))
    right_audio = set(_meta_values_from_meta(candidate_meta, "audio"))
    if left_audio and right_audio and not left_audio.intersection(right_audio):
        return False

    left_channels = _meta_value_from_meta(primary_meta, "channels")
    right_channels = _meta_value_from_meta(candidate_meta, "channels")
    if left_channels and right_channels and left_channels != right_channels:
        return False

    return True


def _manifest_group_key(result):
    """Return the manifest grouping key used to find fallback peers."""
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return None
    kind = manifest.get("payload_kind", "")
    name = manifest.get("group_name", "")
    digest = manifest.get("article_digest", "")
    if not _manifest_group_key_fields_present(kind, name, digest):
        return None
    # Parse group_bytes before the kind branches so an unparseable size
    # rejects every kind (incl. archive), matching the original ordering.
    size = _manifest_group_size_bytes(manifest)
    if size < 0:
        return None
    if kind == "archive":
        return kind, name
    if kind == "video" and size > 0:
        return kind, name, size
    return None


def _manifest_group_key_fields_present(kind, name, digest):
    """Return whether all required manifest group-key fields are non-empty."""
    return bool(kind and name and digest)


def _manifest_group_size_bytes(manifest):
    """Return manifest group_bytes as a positive int, or -1 when unparseable."""
    try:
        return int(manifest.get("group_bytes", 0) or 0)
    except (TypeError, ValueError):
        return -1


def _article_digest(result):
    """Return the manifest article digest attached to a result."""
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return ""
    return manifest.get("article_digest", "") or ""


def _manifest_unsupported_reason(result):
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return ""
    return manifest.get("unsupported_reason", "") or ""


def _metadata_only_manifest_fallback_allowed(primary, candidate):
    """Return whether strict metadata may stand in for an oversized manifest."""
    primary_reason = _manifest_unsupported_reason(primary)
    candidate_reason = _manifest_unsupported_reason(candidate)
    if not primary_reason and not candidate_reason:
        return False
    if primary_reason and primary_reason not in _METADATA_ONLY_MANIFEST_REASONS:
        return False
    if candidate_reason and candidate_reason not in _METADATA_ONLY_MANIFEST_REASONS:
        return False
    return True


def _manifest_payload_kind(result):
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return ""
    return manifest.get("payload_kind", "") or ""


def _manifest_group_bytes(result):
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return 0
    try:
        return int(manifest.get("group_bytes", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _manifest_normalized_video_name(result):
    """Return the candidate's normalized video filename from its manifest, else "".

    Used to PREFER an exact-same-filename repost (a different upload of the
    byte-identical file, already de-duplicated from the primary by article
    digest) ahead of the looser tier/size ranking, per the user requirement to
    try exact-filename matches first.
    """
    manifest = result.get("_fallback_manifest") if isinstance(result, dict) else None
    if not isinstance(manifest, dict):
        return ""
    name = manifest.get("normalized_video_name", "")
    return name.strip().lower() if isinstance(name, str) else ""


def _fallback_peer_matches(primary, candidate):
    """Return whether candidate should be submitted as a standby fallback."""
    primary_link = primary.get("link", "")
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link == primary_link:
        return False

    primary_digest = _article_digest(primary)
    candidate_digest = _article_digest(candidate)
    if primary_digest and candidate_digest and candidate_digest == primary_digest:
        return False

    # Authoritative content-identity gate (F2): never fall back to a different
    # movie part / year / episode / edition even when release tokens overlap.
    if not _same_content(primary, candidate):
        return False

    if not _title_profile_gate_passes(primary, candidate):
        return False

    return _fallback_manifest_peer_matches(primary, candidate)


def _title_profile_gate_passes(primary, candidate):
    """Return whether the title + same-group profile gate accepts a peer.

    A prior prefetch-gate match short-circuits the recheck (the group was
    already validated there).
    """
    if _has_prefetch_gate_match(primary, candidate):
        return True
    if not _titles_look_related(primary, candidate):
        return False
    # require_same_group: a backup must come from the SAME release group as
    # the primary (user requirement) -- a different group's encode is a
    # different file that can never byte-match for a seamless cutover.
    return _metadata_profiles_match(primary, candidate, require_same_group=True)


# Per-tier size bands used for fallback ranking. The content-identity gate
# (``_same_content``) is the authoritative reject; these bands only separate a
# near-identical same-encode repost (Tier 0/1) from looser same-content peers.
_TIER0_SIZE_FRACTION = 0.03
# Manifest group-bytes tolerance for same-resolution + same-codec peers (the
# Tier 1 band). The stream proxy still fingerprint-verifies byte identity
# before cutover, so this only needs to be loose enough to absorb yEnc
# segmentation noise across two uploads of the same encode while still
# rejecting a wildly different file (different tracks/runtime).
_PEER_BYTES_TOLERANCE_FRACTION = 0.10
# Indexer-size prefilter band before fetching a manifest. Content identity is
# enforced separately; this only avoids fetching manifests for releases whose
# advertised size is implausibly far from the primary.
_PREFETCH_INDEXER_SIZE_TOLERANCE_FRACTION = 0.25


def _result_indexer_size(result):
    """Return the indexer-provided result size in bytes, or zero."""
    if not isinstance(result, dict):
        return 0
    value = result.get("size")
    if isinstance(value, bool):
        return 0
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return 0
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 0
    return size if size > 0 else 0


def _synthetic_indexer_size_manifest(result):
    """Return a video-kind manifest synthesized from indexer size metadata."""
    size = _result_indexer_size(result)
    if size < _INDEXER_SIZE_SYNTHETIC_MIN_BYTES:
        return None
    link = result.get("link", "") if isinstance(result, dict) else ""
    if not isinstance(link, str) or not link:
        return None
    digest = hashlib.sha256(link.encode("utf-8")).hexdigest()
    return {
        "payload_kind": "video",
        "group_name": "",
        "group_bytes": size,
        "video_name": "",
        "normalized_video_name": "",
        "video_bytes": size,
        "archive_base_name": "",
        "article_digest": digest,
        "article_count": 0,
        "skipped_candidate_count": 0,
        "skipped_candidates": [],
        "unsupported_reason": "",
    }


def _manifest_with_indexer_size_fallback(result, manifest):
    """Replace parser-unsupported manifests with indexer-size fallback evidence."""
    if not isinstance(manifest, dict):
        return manifest
    reason = manifest.get("unsupported_reason", "") or ""
    if reason not in _INDEXER_SIZE_SYNTHETIC_MANIFEST_REASONS:
        return manifest
    synthetic = _synthetic_indexer_size_manifest(result)
    return synthetic or manifest


def _prefetch_size_gate_match(primary, candidate):
    """Return whether indexer sizes are close enough for prefetching NZBs."""
    primary_size = _result_indexer_size(primary)
    candidate_size = _result_indexer_size(candidate)
    if primary_size <= 0 or candidate_size <= 0:
        return True
    tolerance = primary_size * _PREFETCH_INDEXER_SIZE_TOLERANCE_FRACTION
    return abs(primary_size - candidate_size) <= tolerance


def _fallback_manifest_peer_matches(primary, candidate):
    """Return whether manifest evidence allows an already-prefiltered peer."""
    # Archive group keys are (kind, archive_base_name) without group_bytes, so
    # two archive manifests that share an archive_base short-circuit here and
    # bypass the +/-20% size gate below. That is intentional: a shared
    # archive_base is strong evidence of the same upload set. Distinct
    # archive_base names (Theatrical vs Extended packaging) fall through to
    # the byte-tolerance gate.
    primary_key = _manifest_group_key(primary)
    candidate_key = _manifest_group_key(candidate)
    if primary_key is not None and primary_key == candidate_key:
        return True

    if _metadata_only_manifest_fallback_allowed(primary, candidate):
        return True

    primary_kind = _manifest_payload_kind(primary)
    candidate_kind = _manifest_payload_kind(candidate)
    if not primary_kind or not candidate_kind:
        return False
    video_kinds = ("video", "archive")
    if primary_kind not in video_kinds or candidate_kind not in video_kinds:
        return False
    return _manifest_group_bytes_within_tolerance(primary, candidate)


def _manifest_group_bytes_within_tolerance(primary, candidate):
    """Return whether two manifests' group_bytes are within the peer tolerance.

    Both kinds are plausible video payloads (direct MKV or RAR archive). The
    content-identity gate (``_same_content``) and the same-resolution/codec
    profile gate already ran upstream, so two peers reaching here are the same
    content and the same encode; their group_bytes should differ only by yEnc
    segmentation noise (+/-10%), so a large gap (different tracks/runtime) is
    rejected.
    """
    primary_bytes = _manifest_group_bytes(primary)
    candidate_bytes = _manifest_group_bytes(candidate)
    if primary_bytes <= 0 or candidate_bytes <= 0:
        return False
    tolerance = primary_bytes * _PEER_BYTES_TOLERANCE_FRACTION
    return abs(primary_bytes - candidate_bytes) <= tolerance


def _manifest_may_match_any_peer(result):
    """Return whether this manifest can still match any fetched candidate."""
    if _manifest_group_key(result) is not None:
        return True
    if _manifest_unsupported_reason(result) in _METADATA_ONLY_MANIFEST_REASONS:
        return True
    return bool(_manifest_payload_kind(result))


def _message_id_is_healthy(message_id):
    """Return whether one Message-ID is a usable, well-formed article id."""
    if not isinstance(message_id, str):
        return False
    clean = message_id.strip()
    if not clean or "@" not in clean:
        return False
    return not any(char.isspace() or ord(char) < 0x20 for char in clean)


def _manifest_candidate_message_ids_are_healthy(candidate):
    """Return whether a manifest candidate has usable article Message-IDs."""
    message_ids = candidate.get("message_ids") if isinstance(candidate, dict) else None
    if not isinstance(message_ids, list) or not message_ids:
        return False
    return all(_message_id_is_healthy(message_id) for message_id in message_ids)


def _manifest_error(reason):
    """Return an unsupported manifest for fallback grouping errors."""
    return make_empty_manifest(reason)


def _fallback_settings(settings_getter=None):
    """Return (enabled, max_candidates) from Kodi settings."""
    if settings_getter is None:
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
    else:
        addon = SimpleNamespace(getSetting=lambda key: settings_getter(key, ""))
    enabled = _setting_bool(addon, "fallback_streams_enabled", True)
    max_candidates = _setting_int(addon, "fallback_streams_max", 5)
    if max_candidates < 0 or max_candidates > _MAX_FALLBACKS:
        xbmc.log(
            "NZB-DAV: fallback_streams_max={} clamped to 0..{}".format(
                max_candidates, _MAX_FALLBACKS
            ),
            xbmc.LOGWARNING,
        )
    return enabled, max(0, min(max_candidates, _MAX_FALLBACKS))


def fallback_candidate_prefetch_settings(settings_getter=None):
    """Return fallback discovery settings for picker prefetch callers."""
    if settings_getter is None:
        return _fallback_settings()
    return _fallback_settings(settings_getter=settings_getter)


def fallback_candidate_prefetch_enabled(settings=None):
    """Return whether fallback discovery should scan picker peers."""
    if settings is None:
        settings = fallback_candidate_prefetch_settings()
    enabled, max_candidates = settings
    return enabled and max_candidates > 0


def selection_pool_may_have_fallback_peer(selected, results):
    """Return whether a selection pool can contain a distinct fallback peer."""
    return not _sized_pool_has_no_distinct_peer(selected, results)


def selected_manifest_may_have_fallback_peer(selected):
    """Return whether a selected result's manifest still allows fallback peers."""
    if not isinstance(selected, dict):
        return False
    selected_manifest = selected.get("_fallback_manifest")
    if isinstance(selected_manifest, dict):
        selected_manifest = _manifest_with_indexer_size_fallback(
            selected, selected_manifest
        )
        selected["_fallback_manifest"] = selected_manifest
        selected["_fallback_manifest_error"] = selected_manifest.get(
            "unsupported_reason", ""
        )
    return not (
        isinstance(selected_manifest, dict)
        and not _manifest_may_match_any_peer(selected)
    )


def _pool_has_distinct_nzb_links(results):
    """Return whether a full result pool has at least two usable NZB links."""
    seen_links = set()
    for result in results or []:
        if not isinstance(result, dict):
            continue
        link = result.get("link", "")
        if not link:
            continue
        if seen_links and link not in seen_links:
            return True
        seen_links.add(link)
    return False


def _ensure_fallback_manifests(results):
    """Fetch missing NZB manifests for fallback grouping."""
    started = time.monotonic()
    manifest_cache = {}
    input_count = 0
    for result in results:
        input_count += 1
        _ensure_fallback_manifest(result, manifest_cache)
    telemetry.log_timing(
        "fallback_manifests",
        (time.monotonic() - started) * 1000.0,
        input=input_count,
        fetched=len(manifest_cache),
    )
    return manifest_cache


def _safe_len(value):
    try:
        return len(value)
    except TypeError:
        return "unknown"


def _ensure_fallback_manifest(result, manifest_cache):
    """Fetch one missing NZB manifest using the attach-call cache."""
    manifest = result.get("_fallback_manifest")
    if isinstance(manifest, dict):
        manifest = _manifest_with_indexer_size_fallback(result, manifest)
        result["_fallback_manifest"] = manifest
        result["_fallback_manifest_error"] = manifest.get("unsupported_reason", "")
        return manifest
    link = result.get("link", "")
    if not isinstance(link, str) or not link.strip():
        result["_fallback_manifest_error"] = "missing_link"
        return None
    if link not in manifest_cache:
        manifest_cache[link] = _fetch_fallback_manifest(link)
    manifest = manifest_cache[link]
    if not isinstance(manifest, dict):
        manifest = _manifest_error("fetch_error")
        manifest_cache[link] = manifest
    manifest = _manifest_with_indexer_size_fallback(result, manifest)
    manifest_cache[link] = manifest
    result["_fallback_manifest"] = manifest
    result["_fallback_manifest_error"] = manifest.get("unsupported_reason", "")
    return manifest


def _candidate_pubdate_epoch(candidate):
    """Return a candidate's Usenet post date as UTC epoch seconds, or None.

    None (missing or unparseable pubdate) means "always distinct": such a
    candidate is never collapsed against another and is never suppressed by the
    primary's date — we cannot prove two undated posts are the same upload.
    """
    if not isinstance(candidate, dict):
        return None
    pubdate = candidate.get("pubdate", "")
    if not isinstance(pubdate, str) or not pubdate.strip():
        return None
    return pubdate_to_epoch(pubdate)


def _best_ranked_in_cluster(cluster):
    """Return the best fallback tuple from a same-post-date cluster.

    ``cluster`` is a list of ``(order_index, item)`` where ``item`` is a
    ``(exact_name, tier, size_delta, candidate)`` ranking tuple. Best = lowest
    ``(exact_name, tier, size_delta)`` (highest similarity tier), with original
    arrival order as a deterministic final tie-break.
    """
    return min(
        cluster,
        key=lambda entry: (entry[1][0], entry[1][1], entry[1][2], entry[0]),
    )[1]


def _partition_ranked_by_pubdate(target, ranked):
    """Split ranked tuples into (undated, dated) dropping same-as-primary posts.

    ``dated`` entries are ``(epoch, order_index, item)``; candidates within the
    same-article window of ``target``'s own post date are dropped entirely.
    """
    primary_epoch = _candidate_pubdate_epoch(target)
    undated = []
    dated = []
    for order_index, item in enumerate(ranked):
        epoch = _candidate_pubdate_epoch(item[3])
        if epoch is None:
            undated.append(item)
            continue
        if (
            primary_epoch is not None
            and abs(epoch - primary_epoch) <= _SAME_POST_WINDOW_SECONDS
        ):
            continue  # same upload as the primary -> not a real backup
        dated.append((epoch, order_index, item))
    return undated, dated


def _dedupe_candidates_by_pubdate(target, ranked):
    """Collapse ranked fallback tuples posted within the same-article window.

    ``ranked`` is a list of ``(exact_name, tier, size_delta, candidate)`` tuples.
    Candidates whose post dates fall within ``_SAME_POST_WINDOW_SECONDS`` of each
    other are the same upload re-listed; only the best-ranked member of each such
    cluster is kept. Candidates within the window of ``target``'s own post date
    are dropped (the primary cannot be its own backup). Candidates with no
    parseable post date are always kept.

    Clustering is anchor-based: a candidate joins a cluster only when it is within
    the window of that cluster's EARLIEST member, so a chain of near-posts does
    not transitively merge into one blob. The returned list is unordered with
    respect to rank; callers re-sort before clamping.
    """
    undated, dated = _partition_ranked_by_pubdate(target, ranked)
    dated.sort(key=lambda entry: (entry[0], entry[1]))
    survivors = []
    cluster = []
    anchor_epoch = None
    for epoch, order_index, item in dated:
        if anchor_epoch is None or epoch - anchor_epoch > _SAME_POST_WINDOW_SECONDS:
            if cluster:
                survivors.append(_best_ranked_in_cluster(cluster))
            cluster = [(order_index, item)]
            anchor_epoch = epoch
        else:
            cluster.append((order_index, item))
    if cluster:
        survivors.append(_best_ranked_in_cluster(cluster))

    return undated + survivors


def _candidate_is_fresh_peer(
    target,
    candidate,
    candidate_link,
    candidate_digest,
    seen_links,
    seen_article_digests,
):
    """Return whether a candidate is a new, distinct, matching fallback peer."""
    if not candidate_link or candidate_link in seen_links:
        return False
    if candidate_digest and candidate_digest in seen_article_digests:
        return False
    return _fallback_peer_matches(target, candidate)


def _ranking_tuple_for_candidate(target, candidate, target_size, target_name):
    """Return the (exact_name, tier, size_delta, candidate) tuple, or None."""
    tier = _release_similarity(target, candidate)
    if tier is None:
        return None
    candidate_size = _release_size_bytes(candidate)
    size_delta = abs(target_size - candidate_size) if target_size else 0
    candidate_name = _manifest_normalized_video_name(candidate)
    exact_name = 0 if target_name and candidate_name == target_name else 1
    return (exact_name, tier, size_delta, candidate)


def _attach_candidates_for_target(target, pool, max_candidates):
    matched = []
    seen_links = {target.get("link", "")}
    target_digest = _article_digest(target)
    seen_article_digests = {target_digest} if target_digest else set()
    target_size = _release_size_bytes(target)
    target_name = _manifest_normalized_video_name(target)
    for candidate in pool:
        if candidate is target:
            continue
        candidate_link = candidate.get("link", "")
        candidate_digest = _article_digest(candidate)
        if not _candidate_is_fresh_peer(
            target,
            candidate,
            candidate_link,
            candidate_digest,
            seen_links,
            seen_article_digests,
        ):
            continue
        ranking_tuple = _ranking_tuple_for_candidate(
            target, candidate, target_size, target_name
        )
        if ranking_tuple is None:
            continue
        matched.append(ranking_tuple)
        seen_links.add(candidate_link)
        if candidate_digest:
            seen_article_digests.add(candidate_digest)
    # Collapse same-post-date duplicates (same upload re-listed) before ranking
    # so the _MAX_FALLBACKS clamp keeps the best DISTINCT posts, not dupes.
    matched = _dedupe_candidates_by_pubdate(target, matched)
    # Exact-same-filename first (0 before 1), then tiered ranking: most-similar
    # first (lower tier), then smallest size delta. Sort is stable so equal keys
    # keep pool order.
    matched.sort(key=lambda item: (item[0], item[1], item[2]))
    target["_fallback_candidates"] = [item[3] for item in matched[:max_candidates]]


def _attach_manifest_candidate_if_matching(
    selected, candidate, candidates, seen_candidate_links, seen_article_digests
):
    """Attach a fetched candidate when manifest evidence still matches."""
    candidate_link = candidate.get("link", "")
    candidate_digest = _article_digest(candidate)
    if (
        candidate_link in seen_candidate_links
        or (candidate_digest and candidate_digest in seen_article_digests)
        or not _fallback_manifest_peer_matches(selected, candidate)
    ):
        return False
    candidates.append(candidate)
    seen_candidate_links.add(candidate_link)
    if candidate_digest:
        seen_article_digests.add(candidate_digest)
    return True


def _fetch_selection_manifest_for_queue(kind, index, target, result_queue):
    """Fetch one selection manifest target and publish it to the collector."""
    try:
        _ensure_fallback_manifest(target, {})
    except Exception:  # pylint: disable=broad-except
        target["_fallback_manifest"] = _manifest_error("fetch_error")
        target["_fallback_manifest_error"] = "fetch_error"
    finally:
        result_queue.put((kind, index, target))


def _start_selection_manifest_fetch(kind, index, target, result_queue):
    """Start one daemon manifest fetch, falling back to inline execution."""
    thread = threading.Thread(
        target=_fetch_selection_manifest_for_queue,
        args=(kind, index, target, result_queue),
        name="nzbdav-fallback-manifest",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        _fetch_selection_manifest_for_queue(kind, index, target, result_queue)


def _attach_ready_selection_candidates(
    selected,
    completed,
    next_to_attach,
    candidates,
    seen_candidate_links,
    seen_article_digests,
    max_candidates,
    misses_seen,
    consumed_indices,
):
    """Attach completed candidate manifests in result order."""
    while next_to_attach[0] in consumed_indices:
        next_to_attach[0] += 1
    while next_to_attach[0] in completed:
        ready_index = next_to_attach[0]
        ready_candidate = completed.pop(ready_index)
        consumed_indices.add(ready_index)
        attached = _attach_manifest_candidate_if_matching(
            selected,
            ready_candidate,
            candidates,
            seen_candidate_links,
            seen_article_digests,
        )
        if not attached:
            misses_seen[0] += 1
        next_to_attach[0] += 1
        while next_to_attach[0] in consumed_indices:
            next_to_attach[0] += 1
        if len(candidates) >= max_candidates:
            return True
    remaining_slots = max_candidates - len(candidates)
    if len(completed) >= remaining_slots > 0:
        for ready_index in sorted(completed):
            ready_candidate = completed.pop(ready_index)
            consumed_indices.add(ready_index)
            attached = _attach_manifest_candidate_if_matching(
                selected,
                ready_candidate,
                candidates,
                seen_candidate_links,
                seen_article_digests,
            )
            if not attached:
                misses_seen[0] += 1
            if len(candidates) >= max_candidates:
                return True
    return False


def _attach_selection_candidates_streaming(
    selected,
    candidate_iter,
    candidates,
    seen_candidate_links,
    seen_article_digests,
    include_selected_manifest,
    max_candidates,
):
    """Fetch selected fallback manifests with a rolling ordered window."""
    result_queue = Queue()
    completed = {}
    next_candidate_index = [0]
    next_to_attach = [0]
    active = [0]
    active_candidates = [0]
    candidate_iter = iter(candidate_iter)
    candidate_exhausted = [False]
    pending_to_start = []
    misses_seen = [0]
    consumed_indices = set()
    selected_ready = [not include_selected_manifest]
    selected_can_match = [True]
    optional_tail_deadline = [None]
    max_workers = min(max_candidates, _MAX_FALLBACKS)

    def _start_candidate_fetch():
        if candidate_exhausted[0]:
            return False
        if pending_to_start:
            candidate = pending_to_start.pop(0)
        else:
            try:
                candidate = next(candidate_iter)
            except StopIteration:
                candidate_exhausted[0] = True
                return False
        index = next_candidate_index[0]
        next_candidate_index[0] += 1
        active[0] += 1
        active_candidates[0] += 1
        _start_selection_manifest_fetch("candidate", index, candidate, result_queue)
        return True

    def _fill_candidate_window():
        speculative_slots = min(misses_seen[0], max_candidates - len(candidates))
        while (
            selected_can_match[0]
            and len(candidates) < max_candidates
            and active_candidates[0] < max_workers
            and len(candidates) + active_candidates[0] + len(completed)
            < max_candidates + speculative_slots
            and _start_candidate_fetch()
        ):
            speculative_slots = min(misses_seen[0], max_candidates - len(candidates))

    def _start_stall_speculation():
        active_before = active_candidates[0]
        while _can_start_stall_speculation() and _start_candidate_fetch():
            if active_candidates[0] == active_before:
                break
            active_before = active_candidates[0]

    def _can_start_stall_speculation():
        return (
            selected_ready[0]
            and selected_can_match[0]
            and len(candidates) < max_candidates
            and active_candidates[0] > 0
            and active_candidates[0] < max_workers
            and not candidate_exhausted[0]
        )

    def _optional_tail_wait_remaining():
        if not (
            selected_ready[0]
            and selected_can_match[0]
            and candidate_exhausted[0]
            and candidates
            and len(candidates) < max_candidates
            and active_candidates[0] > 0
        ):
            optional_tail_deadline[0] = None
            return None
        now = time.monotonic()
        if optional_tail_deadline[0] is None:
            optional_tail_deadline[0] = (
                now + _FALLBACK_MANIFEST_OPTIONAL_TAIL_WAIT_SECONDS
            )
        return max(0, optional_tail_deadline[0] - now)

    try:
        pending_to_start.append(next(candidate_iter))
    except StopIteration:
        candidate_exhausted[0] = True

    if not pending_to_start and candidate_exhausted[0]:
        return True

    if include_selected_manifest:
        active[0] += 1
        _start_selection_manifest_fetch("selected", -1, selected, result_queue)

    def _receive_next():
        # Returns (action, message) where action is "got" (message is the
        # (kind, index, target) tuple), "return_true", or "continue".
        try:
            tail_wait = _optional_tail_wait_remaining()
            if tail_wait is not None:
                if tail_wait <= 0:
                    return "return_true", None
                return "got", result_queue.get(timeout=tail_wait)
            if _can_start_stall_speculation():
                return "got", result_queue.get(
                    timeout=_FALLBACK_MANIFEST_STALL_SPECULATION_SECONDS
                )
            return "got", result_queue.get()
        except Empty:
            if _optional_tail_wait_remaining() is not None:
                return "return_true", None
            _start_stall_speculation()
            return "continue", None

    def _record_result(kind, index, target):
        active[0] -= 1
        if kind == "candidate":
            active_candidates[0] -= 1
            completed[index] = target
            return
        selected_ready[0] = True
        selected_digest = _article_digest(selected)
        if selected_digest:
            seen_article_digests.add(selected_digest)
        selected_can_match[0] = _manifest_may_match_any_peer(selected)

    _fill_candidate_window()

    while active[0]:
        action, message = _receive_next()
        if action == "return_true":
            return True
        if action == "continue":
            continue
        kind, index, target = message
        _record_result(kind, index, target)

        if selected_ready[0] and not selected_can_match[0]:
            return False

        if selected_ready[0] and selected_can_match[0]:
            if _attach_ready_selection_candidates(
                selected,
                completed,
                next_to_attach,
                candidates,
                seen_candidate_links,
                seen_article_digests,
                max_candidates,
                misses_seen,
                consumed_indices,
            ):
                return True

        _fill_candidate_window()

    return selected_can_match[0]


def _prefetch_candidate_matches(
    target, candidate, seen_links, target_tokens=None, target_meta=None
):
    """Return whether a candidate is worth fetching manifest evidence for."""
    if not _prefetch_candidate_passes_preconditions(target, candidate, seen_links):
        return False
    candidate_meta = candidate.get("_meta")
    if not isinstance(candidate_meta, dict):
        candidate_meta = None

    title_first = _prefetch_title_first(target_tokens, target_meta, candidate_meta)
    if title_first:
        # Title prefilter precedes the profile parse when no cheap profile gate
        # is available (one side's metadata is not yet computed).
        if not _prefetch_titles_match(target, candidate, target_tokens):
            return False
    if not _prefetch_same_group_profile_match(
        target, candidate, target_meta, candidate_meta
    ):
        return False
    if not title_first and not _prefetch_titles_match(target, candidate, target_tokens):
        return False
    # Authoritative content-identity gate after the cheap profile/title checks.
    return _same_content(target, candidate)


def _prefetch_title_first(target_tokens, target_meta, candidate_meta):
    """Return whether the title prefilter should run before the profile parse."""
    if target_tokens is None:
        return False
    return candidate_meta is None or target_meta is None


def _prefetch_candidate_passes_preconditions(target, candidate, seen_links):
    """Return whether a candidate clears the cheap distinctness + size gates."""
    if candidate is target:
        return False
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link in seen_links:
        return False
    return _prefetch_size_gate_match(target, candidate)


def _prefetch_same_group_profile_match(target, candidate, target_meta, candidate_meta):
    """Return whether the same-group profile gate accepts a prefetch candidate."""
    return _metadata_profiles_match(
        target,
        candidate,
        primary_meta=target_meta,
        candidate_meta=candidate_meta,
        require_same_group=True,
    )


def _prefetch_titles_match(target, candidate, target_tokens):
    """Return whether target/candidate titles look related for prefetch."""
    if target_tokens is None:
        return _titles_look_related(target, candidate)
    return _title_token_sets_look_related(target_tokens, _title_tokens(candidate))


def attach_fallback_candidates(results):
    """Attach duplicate fallback candidates to each result in-place.

    Every result receives ``_fallback_candidates``. When fallback streams are
    disabled, the cap is zero, or a result cannot be conservatively matched,
    the attached list is empty.
    """
    for result in results:
        result["_fallback_candidates"] = []

    if not _pool_has_distinct_nzb_links(results):
        return results

    enabled, max_candidates = _fallback_settings()
    if not enabled or max_candidates <= 0:
        return results

    prefetchable_results = _prefetchable_results(results)
    if not prefetchable_results:
        return results

    _ensure_fallback_manifests(prefetchable_results)
    for result in prefetchable_results:
        _attach_candidates_for_target(result, prefetchable_results, max_candidates)

    return results


def _prefetchable_results(results):
    """Return the results that have at least one prefetchable fallback peer."""
    return [
        result
        for result in results
        if first_prefetchable_fallback_peer(
            result, results, distinct_peer_already_checked=True
        )
    ]


def _selection_prefetch_candidate_matches(
    selected, candidate, seen_prefetch_links, tokens_ref, meta_ref
):
    """Return whether a selection-pool candidate is worth fetching a manifest for.

    ``tokens_ref`` / ``meta_ref`` are single-element lists used to lazily cache
    the selected result's title tokens and metadata across candidates, exactly
    as the original inline generator did.
    """
    if _has_prefetch_gate_match(selected, candidate):
        prefetch_match = True
    else:
        prefetch_match = _selection_prefetch_uncached_match(
            selected, candidate, seen_prefetch_links, tokens_ref, meta_ref
        )
    if meta_ref[0] is None:
        cached_selected_meta = selected.get("_meta")
        if isinstance(cached_selected_meta, dict):
            meta_ref[0] = cached_selected_meta
    return prefetch_match


def _selection_prefetch_uncached_match(
    selected, candidate, seen_prefetch_links, tokens_ref, meta_ref
):
    """Run the non-cached prefetch gate, caching selected tokens lazily."""
    candidate_meta = candidate.get("_meta")
    if not isinstance(candidate_meta, dict):
        candidate_meta = None
    prefetch_tokens = tokens_ref[0]
    if prefetch_tokens is None and (meta_ref[0] is None or candidate_meta is None):
        prefetch_tokens = _title_tokens(selected)
        tokens_ref[0] = prefetch_tokens
    prefetch_match = _prefetch_candidate_matches(
        selected,
        candidate,
        seen_prefetch_links,
        prefetch_tokens,
        meta_ref[0],
    )
    if tokens_ref[0] is None:
        tokens_ref[0] = _cached_title_tokens(selected)
    return prefetch_match


def _iter_selection_prefetch_candidates(
    selected, results, seen_prefetch_links, selected_meta
):
    """Yield selection-pool candidates worth fetching a manifest for.

    Prefiltering stays lazy so all-matching pools still stop after the cap
    instead of fetching manifests for the rest of the result list.
    """
    selected_title_tokens_ref = [None]
    selected_meta_ref = [selected_meta]
    for candidate in results or []:
        if not _selection_prefetch_candidate_eligible(
            selected, candidate, seen_prefetch_links
        ):
            continue
        if _selection_prefetch_candidate_matches(
            selected,
            candidate,
            seen_prefetch_links,
            selected_title_tokens_ref,
            selected_meta_ref,
        ):
            seen_prefetch_links.add(candidate.get("link", ""))
            yield candidate


def _selection_prefetch_candidate_eligible(selected, candidate, seen_prefetch_links):
    """Return whether a candidate clears the cheap distinctness + size gates."""
    if candidate is selected or not isinstance(candidate, dict):
        return False
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link in seen_prefetch_links:
        return False
    return _prefetch_size_gate_match(selected, candidate)


def _selection_seed_article_digests(selected, selected_manifest_ready):
    """Return the initial seen-article-digest set for a selection scan."""
    seen_article_digests = set()
    if selected_manifest_ready:
        selected_digest = _article_digest(selected)
        if selected_digest:
            seen_article_digests.add(selected_digest)
    return seen_article_digests


def _selection_pool_admits_fallback(selected, results):
    """Return whether a selection + pool can still yield a fallback peer."""
    if not selected:
        return False
    if not selected_manifest_may_have_fallback_peer(selected):
        return False
    if results is None or _sized_pool_has_no_distinct_peer(selected, results):
        return False
    return True


def _resolve_fallback_settings(fallback_settings):
    """Return (enabled, max_candidates), loading from Kodi when not supplied."""
    if fallback_settings is None:
        return _fallback_settings()
    return fallback_settings


def attach_fallback_candidates_for_selection(selected, results, fallback_settings=None):
    """Attach fallback candidates only for the result the user selected."""
    if selected:
        selected["_fallback_candidates"] = []

    if not _selection_pool_admits_fallback(selected, results):
        return selected

    enabled, max_candidates = _resolve_fallback_settings(fallback_settings)
    if not enabled or max_candidates <= 0:
        return selected

    seen_prefetch_links = {selected.get("link", "")}
    selected_meta = selected.get("_meta")
    if not isinstance(selected_meta, dict):
        selected_meta = None
    candidates = []
    seen_candidate_links = {selected.get("link", "")}
    selected_manifest_ready = isinstance(selected.get("_fallback_manifest"), dict)
    seen_article_digests = _selection_seed_article_digests(
        selected, selected_manifest_ready
    )

    started = time.monotonic()
    selected_manifest_fetch = not selected_manifest_ready
    _attach_selection_candidates_streaming(
        selected,
        _iter_selection_prefetch_candidates(
            selected, results, seen_prefetch_links, selected_meta
        ),
        candidates,
        seen_candidate_links,
        seen_article_digests,
        include_selected_manifest=selected_manifest_fetch,
        max_candidates=max_candidates,
    )
    telemetry.log_timing(
        "fallback_selection_manifests",
        (time.monotonic() - started) * 1000.0,
        attached=len(candidates),
        pool=_safe_len(results or []),
        selected_manifest_fetch=selected_manifest_fetch,
    )
    selected["_fallback_candidates"] = _rank_fallback_candidates(selected, candidates)
    return selected


def _rank_fallback_candidates(target, candidates):
    """Return candidates ordered best-first by fallback tier, then size delta.

    An exact-same-filename repost (a different upload of the byte-identical
    file) is preferred first; then tiered ranking (lower tier = tried first) so
    the most-similar release is submitted before a looser same-content peer.
    Sort is stable, preserving original arrival order within a bucket.
    """
    target_size = _release_size_bytes(target)
    target_name = _manifest_normalized_video_name(target)
    ranked = []
    for candidate in candidates:
        tier = _release_similarity(target, candidate)
        if tier is None:
            # Content gate already ran upstream; keep as last-resort if it
            # somehow lacks a tier (defensive — should not happen).
            tier = 3
        candidate_size = _release_size_bytes(candidate)
        size_delta = abs(target_size - candidate_size) if target_size else 0
        candidate_name = _manifest_normalized_video_name(candidate)
        exact_name = 0 if target_name and candidate_name == target_name else 1
        ranked.append((exact_name, tier, size_delta, candidate))
    # Collapse same-post-date duplicates (same upload re-listed) before ordering.
    ranked = _dedupe_candidates_by_pubdate(target, ranked)
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in ranked]


def build_fallback_job_name(title, nzb_url, index):
    """Return a stable, traceable nzbdav job name for a fallback candidate."""
    clean_title = title if isinstance(title, str) else ""
    clean_title = _INVALID_TITLE_RE.sub(" ", clean_title)
    clean_title = " ".join(clean_title.split())[:180].strip()
    if not clean_title:
        clean_title = "fallback"

    digest = hashlib.sha256(str(nzb_url).encode("utf-8")).hexdigest()[:8]
    job_name = "{} [fallback-{}-{}]".format(clean_title, index, digest)
    if not _SAFE_JOB_RE.match(job_name):
        job_name = _INVALID_TITLE_RE.sub(" ", job_name)
        job_name = " ".join(job_name.split())
    return job_name


def build_prepare_fallback_payload(fallback_jobs):
    """Build the service prepare manifest payload for fallback jobs."""
    payload = []
    for job in fallback_jobs:
        nzo_id = job.get("nzo_id") if isinstance(job, dict) else None
        if not nzo_id:
            continue
        payload.append(
            {
                "title": job.get("title", ""),
                "nzb_url": job.get("nzb_url", ""),
                "job_name": job.get("job_name", ""),
                "nzo_id": nzo_id,
                "stream_url": job.get("stream_url") or "",
                "stream_headers": job.get("stream_headers") or {},
                "content_length": job.get("content_length") or 0,
            }
        )
    return payload


def fingerprint_ranges(content_length):
    """Return byte ranges used to prove two stream URLs expose the same file."""
    return list(_fingerprint_ranges_for_length(content_length))


def _fingerprint_ranges_for_length(content_length):
    """Return immutable fingerprint ranges for a content length."""
    if content_length <= 0:
        return ()
    if content_length <= _FINGERPRINT_BYTES:
        return ((0, content_length - 1),)

    sample_count = _fingerprint_sample_count(content_length)
    if content_length <= sample_count * _FINGERPRINT_BYTES:
        ranges = []
        start = 0
        while start < content_length:
            end = min(content_length - 1, start + _FINGERPRINT_BYTES - 1)
            ranges.append((start, end))
            start += _FINGERPRINT_BYTES
        return tuple(ranges)

    max_start = content_length - _FINGERPRINT_BYTES
    starts = {0, max_start}
    counter = 0
    while len(starts) < sample_count:
        digest = hashlib.sha256(
            "{}:{}".format(content_length, counter).encode("utf-8")
        ).digest()
        starts.add(int.from_bytes(digest[:8], "big") % (max_start + 1))
        counter += 1
    return tuple((start, start + _FINGERPRINT_BYTES - 1) for start in sorted(starts))


def _fingerprint_sample_count(content_length):
    """Return how many sampled ranges should prove this stream length."""
    if content_length >= _FINGERPRINT_DENSE_SAMPLE_MIN_BYTES:
        return _FINGERPRINT_SAMPLE_COUNT
    return _FINGERPRINT_SMALL_SAMPLE_COUNT


def fetch_content_length(url, auth_header, timeout=10, probe_bases=None):
    """Return Content-Length for a WebDAV stream URL, or 0."""
    probe_url = _validated_probe_url_for_fetch(url, probe_bases=probe_bases)
    if not probe_url:
        return 0
    req = Request(probe_url, method="HEAD")
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with _no_redirect_urlopen(req, timeout=timeout) as resp:
            return int(resp.headers.get("Content-Length", "0") or 0)
    except (HTTPError, URLError, OSError, TypeError, ValueError):
        return 0


def _content_range_matches_request(content_range, start, end, content_length=0):
    """Return whether a Content-Range header matches a requested range."""
    if not isinstance(content_range, str):
        return False
    match = _CONTENT_RANGE_RE.match(content_range.strip())
    if not match:
        return False
    try:
        if int(match.group(1)) != start or int(match.group(2)) != end:
            return False
        if content_length:
            total = match.group(3)
            return total != "*" and int(total) == int(content_length)
        return True
    except ValueError:
        return False


def _normalized_range_content_length(start, end, content_length):
    """Return a validated content_length for a range request, or None if invalid."""
    if not (isinstance(start, int) and isinstance(end, int)):
        return None
    try:
        content_length = int(content_length or 0)
    except (TypeError, ValueError):
        return None
    if not _range_bounds_valid(start, end, content_length):
        return None
    return content_length


def _range_bounds_valid(start, end, content_length):
    """Return whether a byte range and its declared content length are coherent."""
    if start < 0 or end < start or content_length < 0:
        return False
    return not (content_length and end >= content_length)


def _read_validated_range(req, start, end, content_length, timeout):
    """Open ``req`` and return the verified range body, or None on any mismatch."""
    try:
        with _no_redirect_urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 206:
                return None
            if not _content_range_matches_request(
                resp.headers.get("Content-Range"), start, end, content_length
            ):
                return None
            body = resp.read(end - start + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return None
    if len(body) != end - start + 1:
        return None
    return body


def fetch_range_bytes(
    url,
    auth_header,
    start,
    end,
    timeout=10,
    content_length=0,
    probe_bases=None,
):
    """Read a validated byte range from a configured WebDAV stream URL."""
    content_length = _normalized_range_content_length(start, end, content_length)
    if content_length is None:
        return None

    probe_url = _validated_probe_url_for_fetch(url, probe_bases=probe_bases)
    if not probe_url:
        return None
    req = Request(probe_url)
    if auth_header:
        req.add_header("Authorization", auth_header)
    req.add_header("Range", "bytes={}-{}".format(start, end))
    return _read_validated_range(req, start, end, content_length, timeout)


def fetch_range_digest(
    url,
    auth_header,
    start,
    end,
    timeout=10,
    content_length=0,
    probe_bases=None,
):
    """Read a byte range and return a SHA-256 digest of the returned bytes."""
    body = fetch_range_bytes(
        url,
        auth_header,
        start,
        end,
        timeout=timeout,
        content_length=content_length,
        probe_bases=probe_bases,
    )
    if body is None:
        return None
    return hashlib.sha256(body).hexdigest()
