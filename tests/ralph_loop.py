"""Ralph loop — adversarial fuzzer for the addon's hot helpers.

Each round generates a deliberately-nasty input (huge file size, weird
URLs, locale-confusion strings) and feeds it to a focused addon helper.
Failures = either an unhandled exception or a contract violation
(non-deterministic output, leak, off-by-one). When that happens, the
loop prints the offending input + the helper's failure mode so the
operator can write a regression test against it.

Runs as a plain pytest module so it integrates with the existing
xbmc-mocking conftest. Set ``RALPH_ROUNDS`` to bump iteration count;
set ``RALPH_DEBUG=1`` to print every probe.
"""

from __future__ import annotations

import os
import random
import string
import sys
from typing import Callable

import pytest
from resources.lib.fallback_streams import (
    _origin_key,
    _PrecomputedProbeBase,
    _split_http_url,
    _validated_probe_url,
    fingerprint_ranges,
)

RALPH_ROUNDS = int(os.environ.get("RALPH_ROUNDS", "200"))
DEBUG = bool(int(os.environ.get("RALPH_DEBUG", "0")))


def _log(msg: str):
    if DEBUG:
        sys.stderr.write("[ralph] " + msg + "\n")


def _rand_str(rng: random.Random, *, allow_ctrl: bool = False) -> str:
    pool = string.ascii_letters + string.digits + "-._~/?#[]@!$&'()*+,;="
    if allow_ctrl:
        # CR, LF, NUL, tab — the URL-injection garnishes _validate_url
        # has to reject. Including these so we can prove rejection.
        pool += "\x00\r\n\t"
    n = rng.randint(0, 80)
    return "".join(rng.choice(pool) for _ in range(n))


def _rand_url(rng: random.Random) -> str:
    schemes = ("http", "https", "file", "ftp", "javascript", "data", "")
    scheme = rng.choice(schemes)
    if scheme == "":
        return _rand_str(rng)
    host_pool = string.ascii_lowercase + string.digits + "-."
    host = "".join(rng.choice(host_pool) for _ in range(rng.randint(0, 40)))
    if rng.random() < 0.3:
        host = "[::1]"  # IPv6 literal
    elif rng.random() < 0.2:
        host = "127.0.0.1"
    port_part = ""
    if rng.random() < 0.5:
        port_part = ":" + str(rng.randint(0, 70_000))
    auth = ""
    if rng.random() < 0.4:
        auth_user = _rand_str(rng).split("/", maxsplit=1)[0][:20]
        auth_pass = _rand_str(rng).split("/", maxsplit=1)[0][:20]
        auth = "{}:{}@".format(auth_user, auth_pass)
    path = "/" + _rand_str(rng).strip("/")
    return "{}://{}{}{}{}".format(scheme, auth, host, port_part, path)


# Each invariant is one assertion we expect to ALWAYS hold, regardless
# of input. A regression that breaks the invariant fails the test
# immediately so we can capture and ship a regression test.
def _invariant_fingerprint_count(rng: random.Random) -> tuple[Callable, dict]:
    content_length = rng.choice(
        [
            -(2**63),  # overflow domain
            -1,
            0,
            1,
            4095,
            4096,
            4097,
            100 * 4096 - 1,
            100 * 4096,
            100 * 4096 + 1,
            rng.randint(1, 2**40),
            2**63 - 1,
        ]
    )

    def check():
        ranges = fingerprint_ranges(content_length)
        # Invariant: ranges is a list of (start, end) pairs, all non-overlapping
        # within a single call, and bounded by content_length.
        assert isinstance(ranges, list), repr(ranges)
        if content_length <= 0:
            assert not ranges, "non-empty for non-positive: {}".format(content_length)
            return
        for start, end in ranges:
            assert (
                0 <= start <= end < content_length
            ), "range out of bounds for cl={}: ({}, {})".format(
                content_length, start, end
            )
        # Determinism: same input → same output.
        assert ranges == fingerprint_ranges(content_length)
        # No duplicate starts within one call (set semantics enforced upstream).
        starts = [s for s, _ in ranges]
        assert len(starts) == len(set(starts)), "duplicate starts at cl={}".format(
            content_length
        )

    return check, {"content_length": content_length}


def _invariant_validated_probe_url(rng: random.Random) -> tuple[Callable, dict]:
    base_url = (
        "http://" + rng.choice(["nzbdav", "host.example", "127.0.0.1:8080"]) + "/"
    )
    probe_url = _rand_url(rng)
    parts = _split_http_url(base_url.rstrip("/"))
    bases = (_PrecomputedProbeBase(parts, _origin_key(parts), "/"),) if parts else ()

    def check():
        # Invariant: must NEVER raise on any input string. None or a URL
        # string is the only acceptable return shape.
        result = _validated_probe_url(probe_url, probe_bases=bases)
        assert result is None or isinstance(
            result, str
        ), "non-str/non-None: {!r}".format(result)
        # If accepted, the result must start with the base origin.
        if result is not None and parts:
            origin_prefix = "{}://{}".format(parts.scheme.lower(), parts.netloc)
            assert result.startswith(
                origin_prefix
            ), "accepted off-origin: probe={!r} → {!r}".format(probe_url, result)

    return check, {"probe_url": probe_url, "base": base_url}


def _invariant_split_http_url(rng: random.Random) -> tuple[Callable, dict]:
    url = rng.choice(
        [
            "",
            "/",
            "//",
            "http://",
            "http:///",
            "http://[::1]:65535/",
            "http://user:pass@host/",
            "http://:pass@host/",
            "http://user:@host/",
            "http://user:p%3Aass@host/path?q=1#f",
            "ftp://server/",
            "javascript:alert(1)",
            _rand_url(rng),
        ]
    )

    def check():
        # Invariant: must NEVER raise. None or an SplitResult-like is OK.
        result = _split_http_url(url)
        if result is not None:
            # If parse succeeded, scheme must be http or https.
            assert result.scheme.lower() in (
                "http",
                "https",
            ), "non-http scheme accepted: {!r} from {!r}".format(result.scheme, url)

    return check, {"url": url}


# The Ralph loop: each round picks one invariant at random and probes it.
# Bug discovery is signalled by an AssertionError or unhandled exception
# during ``check()``. Pytest's traceback gives us the exact input that
# tripped the invariant.
INVARIANTS = (
    _invariant_fingerprint_count,
    _invariant_validated_probe_url,
    _invariant_split_http_url,
)


@pytest.mark.parametrize("seed", range(RALPH_ROUNDS))
def test_ralph_round(seed):
    rng = random.Random(seed)
    invariant_factory = rng.choice(INVARIANTS)
    check, ctx = invariant_factory(rng)
    try:
        check()
    except AssertionError as exc:
        pytest.fail(
            "ralph: invariant {} broken at seed={} ctx={!r}\n  detail: {}".format(
                invariant_factory.__name__, seed, ctx, exc
            )
        )
    except Exception as exc:  # pylint: disable=broad-except
        pytest.fail(
            "ralph: invariant {} raised at seed={} ctx={!r}\n  unexpected: {!r}".format(
                invariant_factory.__name__, seed, ctx, exc
            )
        )
