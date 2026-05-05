# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import patch
from xml.sax.saxutils import escape, quoteattr

from resources.lib.http_util import HttpResponseTooLarge
from resources.lib.nzb_manifest import (
    extract_nzb_video_manifest,
    fetch_nzb_video_manifest,
    normalize_video_filename,
)


def _nzb_xml(files):
    body = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">',
    ]
    body.extend(files)
    body.append("</nzb>")
    return "\n".join(body).encode("utf-8")


def _file(subject, segments):
    segment_xml = "\n".join(
        '<segment bytes="{}" number="{}">{}</segment>'.format(
            size, number, escape(msgid)
        )
        for number, size, msgid in segments
    )
    return """
    <file poster="poster" date="1777937305" subject={}>
      <groups><group>alt.binaries.test</group></groups>
      <segments>{}</segments>
    </file>
    """.format(quoteattr(subject), segment_xml)


def test_normalize_video_filename_is_case_and_separator_insensitive():
    assert (
        normalize_video_filename("Movie.Name.2026.2160p-GROUP.mkv")
        == "movie name 2026 2160p group.mkv"
    )
    assert (
        normalize_video_filename('"Movie Name 2026 2160p-GROUP.MKV"')
        == "movie name 2026 2160p group.mkv"
    )


def test_extracts_main_video_file_manifest_from_nzb():
    xml = _nzb_xml(
        [
            _file('"Movie.Name.2026.2160p-GROUP.nfo" yEnc (1/1)', [(1, 100, "nfo@id")]),
            _file(
                '"Movie.Name.2026.2160p-GROUP.mkv" yEnc (1/2)',
                [(1, 1000, "part1@id"), (2, 2000, "part2@id")],
            ),
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "video"
    assert manifest["video_name"] == "Movie.Name.2026.2160p-GROUP.mkv"
    assert manifest["normalized_video_name"] == "movie name 2026 2160p group.mkv"
    assert manifest["group_name"] == "movie name 2026 2160p group.mkv"
    assert manifest["group_bytes"] == 3000
    assert manifest["video_bytes"] == 3000
    assert manifest["article_count"] == 2
    assert manifest["article_digest"]
    assert manifest["skipped_candidate_count"] == 0
    assert manifest["skipped_candidates"] == []
    assert "message_ids" not in manifest
    assert manifest["unsupported_reason"] == ""


def test_selects_largest_supported_video_entry_and_ignores_samples():
    xml = _nzb_xml(
        [
            _file(
                '"Sample.Movie.Name.2026-GROUP.mkv" yEnc (1/1)',
                [(1, 1000, "sample@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "a@id"), (2, 5000, "b@id")],
            ),
            _file('"Movie.Name.2026-GROUP.par2" yEnc (1/1)', [(1, 90000, "par2@id")]),
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert manifest["video_bytes"] == 10000


def test_health_check_failure_skips_first_video_candidate():
    xml = _nzb_xml(
        [
            _file(
                '"Broken.Movie.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "bad@id"), (2, 5000, "bad2@id")],
            ),
            _file(
                '"Working.Movie.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 4000, "good@id"), (2, 4000, "good2@id")],
            ),
        ]
    )
    checked = []

    def health_check(candidate):
        checked.append(candidate["video_name"] or candidate["archive_base_name"])
        return "bad@id" not in candidate["message_ids"]

    manifest = extract_nzb_video_manifest(xml, health_check=health_check)

    assert checked == ["Broken.Movie.2026-GROUP.mkv", "Working.Movie.2026-GROUP.mkv"]
    assert manifest["video_name"] == "Working.Movie.2026-GROUP.mkv"
    assert manifest["video_bytes"] == 8000
    assert manifest["skipped_candidate_count"] == 1
    assert manifest["skipped_candidates"] == [
        {"name": "Broken.Movie.2026-GROUP.mkv", "reason": "message_id_health_failed"}
    ]
    assert manifest["unsupported_reason"] == ""


def test_health_check_failure_skips_first_archive_group_candidate():
    xml = _nzb_xml(
        [
            _file('"Broken.part001.rar" yEnc (1/1)', [(1, 1000, "bad-rar@id")]),
            _file('"Working.part001.rar" yEnc (1/1)', [(1, 1000, "good-rar@id")]),
        ]
    )

    manifest = extract_nzb_video_manifest(
        xml,
        health_check=lambda candidate: "bad-rar@id" not in candidate["message_ids"],
    )

    assert manifest["payload_kind"] == "archive"
    assert manifest["archive_base_name"] == "working"
    assert manifest["skipped_candidate_count"] == 1
    assert manifest["unsupported_reason"] == ""


def test_all_candidates_failing_health_check_is_unsupported_without_raising():
    xml = _nzb_xml(
        [
            _file('"Broken.Movie.2026-GROUP.mkv" yEnc (1/1)', [(1, 1000, "bad@id")]),
            _file(
                '"Also.Broken.Movie.2026-GROUP.mkv" yEnc (1/1)',
                [(1, 1000, "bad2@id")],
            ),
        ]
    )

    manifest = extract_nzb_video_manifest(xml, health_check=lambda _candidate: False)

    assert manifest["unsupported_reason"] == "all_candidate_files_failed_health_check"
    assert manifest["skipped_candidate_count"] == 2


def test_same_articles_have_same_digest_even_if_segment_order_changes():
    first = _nzb_xml(
        [_file('"Movie.mkv" yEnc (1/2)', [(1, 1000, "<A@ID>"), (2, 1000, "<B@ID>")])]
    )
    second = _nzb_xml(
        [_file('"Movie.mkv" yEnc (1/2)', [(2, 1000, "b@id"), (1, 1000, "a@id")])]
    )

    assert (
        extract_nzb_video_manifest(first)["article_digest"]
        == extract_nzb_video_manifest(second)["article_digest"]
    )


def test_different_uploads_have_different_article_digest():
    first = _nzb_xml(
        [_file('"Movie.mkv" yEnc (1/2)', [(1, 1000, "a@id"), (2, 1000, "b@id")])]
    )
    second = _nzb_xml(
        [_file('"Movie.mkv" yEnc (1/2)', [(1, 1000, "c@id"), (2, 1000, "d@id")])]
    )

    assert (
        extract_nzb_video_manifest(first)["article_digest"]
        != extract_nzb_video_manifest(second)["article_digest"]
    )


def test_rar_only_nzb_produces_provisional_archive_manifest():
    xml = _nzb_xml(
        [
            _file('"Movie.part001.rar" yEnc (1/1)', [(1, 1000, "rar1@id")]),
            _file('"Movie.part002.rar" yEnc (1/1)', [(1, 1000, "rar2@id")]),
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "archive"
    assert manifest["archive_base_name"] == "movie"
    assert manifest["group_name"] == "movie"
    assert manifest["group_bytes"] == 0
    assert manifest["video_name"] == ""
    assert manifest["video_bytes"] == 0
    assert manifest["article_digest"]
    assert manifest["unsupported_reason"] == ""


def test_invalid_xml_is_unsupported_without_raising():
    manifest = extract_nzb_video_manifest(b"<nzb><file></nzb>")

    assert manifest["unsupported_reason"] == "invalid_xml"


@patch("resources.lib.nzb_manifest.http_get")
def test_fetch_nzb_video_manifest_fetches_and_parses_valid_nzb(mock_http_get):
    xml = _nzb_xml([_file('"Movie.mkv" yEnc (1/1)', [(1, 1000, "a@id")])])
    mock_http_get.return_value = xml.decode("utf-8")

    manifest = fetch_nzb_video_manifest("https://idx/getnzb/123")

    assert manifest["video_name"] == "Movie.mkv"
    mock_http_get.assert_called_once_with(
        "https://idx/getnzb/123",
        timeout=5,
        max_bytes=2 * 1024 * 1024,
    )


@patch("resources.lib.nzb_manifest.http_get")
def test_fetch_nzb_video_manifest_passes_candidate_health_check(mock_http_get):
    xml = _nzb_xml(
        [
            _file('"Broken.Movie.mkv" yEnc (1/1)', [(1, 1000, "bad@id")]),
            _file('"Working.Movie.mkv" yEnc (1/1)', [(1, 900, "good@id")]),
        ]
    )
    mock_http_get.return_value = xml.decode("utf-8")

    manifest = fetch_nzb_video_manifest(
        "https://idx/getnzb/123",
        health_check=lambda candidate: "bad@id" not in candidate["message_ids"],
    )

    assert manifest["video_name"] == "Working.Movie.mkv"
    assert manifest["skipped_candidate_count"] == 1


@patch("resources.lib.nzb_manifest.http_get")
def test_fetch_nzb_video_manifest_rejects_invalid_url(mock_http_get):
    manifest = fetch_nzb_video_manifest("file:///etc/passwd")

    assert manifest["unsupported_reason"] == "invalid_url"
    mock_http_get.assert_not_called()


@patch("resources.lib.nzb_manifest.http_get", side_effect=OSError("timeout"))
def test_fetch_nzb_video_manifest_returns_fetch_error(_mock_http_get):
    manifest = fetch_nzb_video_manifest("https://idx/getnzb/123")

    assert manifest["unsupported_reason"] == "fetch_error"


@patch(
    "resources.lib.nzb_manifest.http_get",
    side_effect=HttpResponseTooLarge("too large"),
)
def test_fetch_nzb_video_manifest_rejects_oversized_nzb(_mock_http_get):
    manifest = fetch_nzb_video_manifest("https://idx/getnzb/123")

    assert manifest["unsupported_reason"] == "too_large"
