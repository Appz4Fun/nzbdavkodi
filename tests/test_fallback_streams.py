# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import ANY, MagicMock, patch
from urllib.error import URLError
from xml.sax.saxutils import quoteattr

from resources.lib.fallback_streams import (
    _SAFE_JOB_RE,
    _fallback_settings,
    attach_fallback_candidates,
    build_fallback_job_name,
    build_prepare_fallback_payload,
    fetch_content_length,
    fetch_range_digest,
    fingerprint_ranges,
)


def _mock_range_response(body, status=206, headers=None):
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body)
    resp.status = status
    resp.getcode = MagicMock(return_value=status)
    header_map = {str(key).lower(): value for key, value in (headers or {}).items()}
    resp.headers.get = MagicMock(
        side_effect=lambda key, default=None: header_map.get(str(key).lower(), default)
    )
    return resp


def _nzb_xml(files):
    body = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">',
    ]
    body.extend(files)
    body.append("</nzb>")
    return "\n".join(body).encode("utf-8")


def _nzb_file(subject, segments):
    segment_xml = "\n".join(
        '<segment bytes="{}" number="{}">{}</segment>'.format(size, number, msgid)
        for number, size, msgid in segments
    )
    return """
    <file poster="poster" date="1777937305" subject={}>
      <groups><group>alt.binaries.test</group></groups>
      <segments>{}</segments>
    </file>
    """.format(quoteattr(subject), segment_xml)


def _result(title, link, size, meta=None):
    return {
        "title": title,
        "link": link,
        "size": size,
        "_meta": meta
        or {
            "resolution": "1080p",
            "quality": "WEB-DL",
            "codec": "x265/HEVC",
            "group": "GROUP",
            "container": "mkv",
        },
    }


def _fallback_setting(key):
    return {
        "webdav_url": "http://webdav/content",
        "nzbdav_url": "http://nzbdav:3000",
    }.get(key, "")


def _manifest(kind, name, size, digest, article_count=2):
    manifest = {
        "payload_kind": kind,
        "group_name": name,
        "group_bytes": size,
        "video_name": name if kind == "video" else "",
        "normalized_video_name": name if kind == "video" else "",
        "video_bytes": size if kind == "video" else 0,
        "archive_base_name": name if kind == "archive" else "",
        "article_digest": digest,
        "article_count": article_count,
        "skipped_candidate_count": 0,
        "skipped_candidates": [],
        "unsupported_reason": "",
    }
    return manifest


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_manifest_grouping_uses_video_name_and_bytes_not_result_size(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 2)
    primary = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://a/nzb",
        1000,
    )
    duplicate = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://b/nzb",
        "1001",
    )
    unrelated = _result(
        "Example Movie 2026 2160p WEB-DL x265-GROUP",
        "https://c/nzb",
        1000,
        meta={
            "resolution": "2160p",
            "quality": "WEB-DL",
            "codec": "x265/HEVC",
            "group": "GROUP",
            "container": "mkv",
        },
    )
    manifests = {
        "https://a/nzb": _manifest("video", "example movie 2026 group.mkv", 8000, "a"),
        "https://b/nzb": _manifest("video", "example movie 2026 group.mkv", 8000, "b"),
        "https://c/nzb": _manifest("video", "example movie 2026 group.mkv", 9000, "c"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    results = [primary, duplicate, unrelated]

    assert attach_fallback_candidates(results) is results
    assert primary["_fallback_candidates"] == [duplicate]
    assert duplicate["_fallback_candidates"] == [primary]
    assert unrelated["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams._fallback_settings")
def test_malformed_manifest_group_bytes_fails_closed_without_aborting(mock_settings):
    mock_settings.return_value = (True, 5)
    malformed = _result("Movie bad manifest", "https://idx/a.nzb", 1)
    malformed["_fallback_manifest"] = _manifest("video", "movie.mkv", 1000, "a")
    malformed["_fallback_manifest"]["group_bytes"] = "not-a-number"
    valid = _result("Movie valid manifest", "https://idx/b.nzb", 2)
    valid["_fallback_manifest"] = _manifest("video", "movie.mkv", 1000, "b")

    attach_fallback_candidates([malformed, valid])

    assert malformed["_fallback_candidates"] == []
    assert valid["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams._fallback_settings")
def test_disabled_setting_adds_empty_fallback_lists(mock_settings):
    mock_settings.return_value = (False, 2)
    results = [
        _result(
            "Example Movie 2026 1080p WEB-DL x265-GROUP",
            "https://a/nzb",
            1000,
        ),
        _result(
            "Example Movie 2026 1080p WEB-DL x265-GROUP",
            "https://b/nzb",
            1000,
        ),
    ]

    attach_fallback_candidates(results)

    assert [result["_fallback_candidates"] for result in results] == [[], []]


def test_fallback_settings_default_to_enabled_with_five_candidates():
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        return_value="",
    ):
        assert _fallback_settings() == (True, 5)


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_same_article_mirrors_are_not_attached_as_fallbacks(mock_settings, mock_fetch):
    mock_settings.return_value = (True, 5)
    primary = _result("Movie", "https://idx/a.nzb", 1)
    mirror = _result("Movie mirror", "https://idx/b.nzb", 2)
    repost = _result("Movie repost", "https://idx/c.nzb", 3)
    manifests = {
        "https://idx/a.nzb": _manifest("video", "movie.mkv", 1000, "articles-a"),
        "https://idx/b.nzb": _manifest("video", "movie.mkv", 1000, "articles-a"),
        "https://idx/c.nzb": _manifest("video", "movie.mkv", 1000, "articles-c"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, mirror, repost])

    assert primary["_fallback_candidates"] == [repost]
    assert mirror["_fallback_candidates"] == [repost]
    assert repost["_fallback_candidates"] == [primary]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_rar_only_manifests_are_grouped_provisionally_for_runtime_validation(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = _result("Movie", "https://idx/a.nzb", 1)
    archive_fallback = _result("Movie", "https://idx/b.nzb", 2)
    manifests = {
        "https://idx/a.nzb": _manifest("archive", "movie", 0, "articles-a"),
        "https://idx/b.nzb": _manifest("archive", "movie", 0, "articles-b"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, archive_fallback])

    assert primary["_fallback_candidates"] == [archive_fallback]
    assert archive_fallback["_fallback_candidates"] == [primary]


@patch("resources.lib.nzb_manifest.http_get")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_attach_fallbacks_skips_unhealthy_manifest_file_candidates(
    mock_settings, mock_http_get
):
    mock_settings.return_value = (True, 5)
    primary = _result("Movie primary", "https://idx/a.nzb", 1)
    fallback = _result("Movie fallback", "https://idx/b.nzb", 2)
    bodies = {
        "https://idx/a.nzb": _nzb_xml(
            [
                _nzb_file(
                    '"Broken.Primary.mkv" yEnc (1/1)',
                    [(1, 10000, "malformed id")],
                ),
                _nzb_file('"Movie.mkv" yEnc (1/1)', [(1, 8000, "good-a@id")]),
            ]
        ),
        "https://idx/b.nzb": _nzb_xml(
            [
                _nzb_file(
                    '"Broken.Fallback.mkv" yEnc (1/1)',
                    [(1, 10000, "also malformed")],
                ),
                _nzb_file('"Movie.mkv" yEnc (1/1)', [(1, 8000, "good-b@id")]),
            ]
        ),
    }
    mock_http_get.side_effect = lambda url, **_kwargs: bodies[url].decode("utf-8")

    attach_fallback_candidates([primary, fallback])

    assert primary["_fallback_manifest"]["video_name"] == "Movie.mkv"
    assert fallback["_fallback_manifest"]["video_name"] == "Movie.mkv"
    assert primary["_fallback_manifest"]["skipped_candidate_count"] == 1
    assert fallback["_fallback_manifest"]["skipped_candidate_count"] == 1
    assert primary["_fallback_candidates"] == [fallback]
    assert fallback["_fallback_candidates"] == [primary]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_manifest_fetches_are_cached_per_attach_call(mock_settings, mock_fetch):
    mock_settings.return_value = (True, 5)
    first = _result("Movie", "https://idx/same.nzb", 1)
    second = _result("Movie duplicate", "https://idx/same.nzb", 2)
    mock_fetch.return_value = _manifest("video", "movie.mkv", 1000, "same-articles")

    attach_fallback_candidates([first, second])

    mock_fetch.assert_called_once_with("https://idx/same.nzb", health_check=ANY)
    assert first["_fallback_manifest_error"] == ""
    assert second["_fallback_manifest_error"] == ""


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_manifest_fetch_exception_marks_one_result_failed_without_raising(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    broken = _result("Broken", "https://idx/broken.nzb", 1)
    working = _result("Working", "https://idx/working.nzb", 2)

    def fetch(url, **_kwargs):
        if url.endswith("broken.nzb"):
            raise RuntimeError("message id failure")
        return _manifest("video", "movie.mkv", 1000, "articles-working")

    mock_fetch.side_effect = fetch

    attach_fallback_candidates([broken, working])

    assert broken["_fallback_manifest_error"] == "fetch_error"
    assert broken["_fallback_candidates"] == []
    assert working["_fallback_manifest_error"] == ""
    assert working["_fallback_candidates"] == []


def test_build_fallback_job_name_unique_traceable_and_single_line():
    first = build_fallback_job_name(
        "Example\nMovie: 2026 / 1080p WEB-DL x265-GROUP",
        "https://hydra/getnzb?id=one",
        1,
    )
    second = build_fallback_job_name(
        "Example\nMovie: 2026 / 1080p WEB-DL x265-GROUP",
        "https://hydra/getnzb?id=two",
        2,
    )

    assert first != second
    assert "Example Movie 2026 1080p WEB-DL x265-GROUP" in first
    assert first.endswith("[fallback-1-91fffc91]")
    assert second.endswith("[fallback-2-db3a3f35]")
    assert "\n" not in first
    assert "\r" not in first
    assert _SAFE_JOB_RE.match(first)
    assert len(first) <= 180 + len(" [fallback-1-8af769ea]")


def test_build_fallback_job_name_uses_fallback_title_when_clean_title_empty():
    job_name = build_fallback_job_name("\n\t:::////", "https://hydra/getnzb?id=one", 1)

    assert job_name.startswith("fallback ")


def test_build_prepare_fallback_payload_preserves_completed_and_standby_jobs():
    payload = build_prepare_fallback_payload(
        [
            {
                "title": "completed",
                "nzb_url": "https://hydra/getnzb?id=done",
                "job_name": "completed [fallback-1-11111111]",
                "nzo_id": "SABnzbd_nzo_done",
                "stream_url": "http://webdav/content/completed/movie.mkv",
                "stream_headers": {"Authorization": "Basic abc"},
                "content_length": 123456,
            },
            {
                "title": "standby",
                "nzb_url": "https://hydra/getnzb?id=standby",
                "job_name": "standby [fallback-2-22222222]",
                "nzo_id": "SABnzbd_nzo_standby",
            },
            {
                "title": "missing nzo",
                "nzb_url": "https://hydra/getnzb?id=missing",
                "job_name": "missing [fallback-3-33333333]",
            },
        ]
    )

    assert payload == [
        {
            "title": "completed",
            "nzb_url": "https://hydra/getnzb?id=done",
            "job_name": "completed [fallback-1-11111111]",
            "nzo_id": "SABnzbd_nzo_done",
            "stream_url": "http://webdav/content/completed/movie.mkv",
            "stream_headers": {"Authorization": "Basic abc"},
            "content_length": 123456,
        },
        {
            "title": "standby",
            "nzb_url": "https://hydra/getnzb?id=standby",
            "job_name": "standby [fallback-2-22222222]",
            "nzo_id": "SABnzbd_nzo_standby",
            "stream_url": "",
            "stream_headers": {},
            "content_length": 0,
        },
    ]


def test_fingerprint_ranges_uses_1000_deterministic_4096_byte_samples_for_large_files():
    content_length = 10 * 1024 * 1024 * 1024

    ranges = fingerprint_ranges(content_length)

    assert len(ranges) == 1000
    assert len(set(ranges)) == 1000
    assert ranges == fingerprint_ranges(content_length)
    assert ranges[0] == (0, 4095)
    assert ranges[-1] == (content_length - 4096, content_length - 1)
    assert all((end - start + 1) == 4096 for start, end in ranges)


def test_fingerprint_ranges_handles_small_files():
    assert fingerprint_ranges(1024) == [(0, 1023)]


def test_fingerprint_ranges_chunks_whole_file_when_smaller_than_sample_budget():
    assert fingerprint_ranges(5000) == [(0, 4095), (4096, 4999)]


@patch("resources.lib.fallback_streams.urlopen", side_effect=URLError("timeout"))
def test_fetch_range_digest_returns_none_on_probe_error(_mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest("http://webdav/content/movie.mkv", None, 0, 1023) is None
        )


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_non_http_urls(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("file:///etc/passwd", None, 0, 3) is None
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_off_origin_urls(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest("http://evil.test/content/movie.mkv", None, 0, 3) is None
        )
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_content_length_rejects_off_origin_urls(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_content_length("http://evil.test/content/movie.mkv", None) == 0
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_configured_host_outside_content_root(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("http://webdav/private/movie.mkv", None, 0, 3) is None
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_content_length_accepts_configured_stream_url(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(
        b"",
        headers={"Content-Length": "1234"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_content_length("http://webdav/content/movie.mkv", None) == 1234

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://webdav/content/movie.mkv"


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_server_that_ignores_range(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(b"A" * 4, status=200)

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("http://webdav/content/movie.mkv", None, 0, 3) is None


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_requires_matching_content_range(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(
        b"A" * 4,
        status=206,
        headers={"Content-Range": "bytes 4-7/10"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("http://webdav/content/movie.mkv", None, 0, 3) is None


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_requires_matching_content_range_total(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(
        b"A" * 4,
        status=206,
        headers={"Content-Range": "bytes 0-3/11"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest(
                "http://webdav/content/movie.mkv", None, 0, 3, content_length=10
            )
            is None
        )


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_accepts_matching_partial_content(mock_urlopen):
    body = b"A" * 4
    mock_urlopen.return_value = _mock_range_response(
        body,
        status=206,
        headers={"Content-Range": "bytes 0-3/10"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest(
                "http://webdav/content/movie.mkv", None, 0, 3, content_length=10
            )
            == "63c1dd951ffedf6f7fd968ad4efa39b8ed584f162f46e715114ee184f8de9201"
        )
