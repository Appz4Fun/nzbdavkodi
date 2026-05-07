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


def test_manifest_without_health_check_skips_message_id_materialization():
    class RowWithoutMessageIdListAccess:  # pylint: disable=too-few-public-methods
        def __init__(self, number, size):
            self.number = number
            self.size = size

        def __getitem__(self, index):
            if index == 0:
                return self.number
            if index == 1:
                return self.size
            if index == 2:
                raise AssertionError(
                    "message_ids should not be materialized without health_check"
                )
            raise IndexError(index)

    xml = _nzb_xml(
        [_file('"Movie.Name.2026.2160p-GROUP.mkv" yEnc (1/2)', [(1, 1, "a@id")])]
    )
    rows = [
        RowWithoutMessageIdListAccess(1, 1000),
        RowWithoutMessageIdListAccess(2, 2000),
    ]

    with patch("resources.lib.nzb_manifest._segment_rows", return_value=rows), patch(
        "resources.lib.nzb_manifest._digest_articles", return_value="digest"
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["video_name"] == "Movie.Name.2026.2160p-GROUP.mkv"
    assert manifest["video_bytes"] == 3000
    assert manifest["article_count"] == 2
    assert manifest["article_digest"] == "digest"
    assert "message_ids" not in manifest


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


def test_skips_segment_parsing_for_non_candidate_subjects():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file('"Movie.Name.2026-GROUP.nfo" yEnc (1/1)', [(1, 100, "nfo@id")]),
            _file('"Movie.Name.2026-GROUP.par2" yEnc (1/1)', [(1, 200, "par2@id")]),
            _file(
                '"Sample.Movie.Name.2026-GROUP.mkv" yEnc (1/1)',
                [(1, 300, "sample@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "a@id"), (2, 5000, "b@id")],
            ),
        ]
    )
    parsed_subjects = []
    original_segment_rows = nzb_manifest._segment_rows

    def counted_segment_rows(file_elem):
        parsed_subjects.append(file_elem.attrib.get("subject", ""))
        return original_segment_rows(file_elem)

    with patch(
        "resources.lib.nzb_manifest._segment_rows", side_effect=counted_segment_rows
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert parsed_subjects == ['"Movie.Name.2026-GROUP.mkv" yEnc (1/2)']


def test_extract_nzb_manifest_does_not_scan_segments_from_non_candidate_files():
    from resources.lib import nzb_manifest

    non_candidates = []
    for file_index in range(20):
        segments = [
            (segment_index, 100, "nfo-{}-{}@id".format(file_index, segment_index))
            for segment_index in range(1, 41)
        ]
        non_candidates.append(
            _file(
                '"Movie.Name.2026-GROUP.extra{}.nfo" yEnc'.format(file_index),
                segments,
            )
        )
    xml = _nzb_xml(
        non_candidates
        + [
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "video-a@id"), (2, 5000, "video-b@id")],
            )
        ]
    )
    stripped_tags = []
    original_strip_namespace = nzb_manifest._strip_namespace

    def counted_strip_namespace(tag):
        name = original_strip_namespace(tag)
        stripped_tags.append(name)
        return name

    with patch(
        "resources.lib.nzb_manifest._strip_namespace",
        side_effect=counted_strip_namespace,
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "video"
    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert stripped_tags.count("segment") == 2


def test_skips_video_filename_regex_for_subjects_without_video_extensions():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file('"Movie.Name.2026-GROUP.nfo" yEnc (1/1)', [(1, 100, "nfo@id")]),
            _file('"Movie.Name.2026-GROUP.par2" yEnc (1/1)', [(1, 200, "par2@id")]),
            _file('"Movie.Name.2026-GROUP.sfv" yEnc (1/1)', [(1, 300, "sfv@id")]),
            _file('"Artwork.Release.2026-GROUP.jpg" yEnc (1/1)', [(1, 400, "jpg@id")]),
            _file(
                '"Sample.Movie.Name.2026-GROUP.mkv" yEnc (1/1)',
                [(1, 500, "sample@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "a@id"), (2, 5000, "b@id")],
            ),
        ]
    )
    checked_subjects = []
    original_find_video_name = nzb_manifest._find_video_name

    def counted_find_video_name(subject):
        checked_subjects.append(subject)
        return original_find_video_name(subject)

    with patch(
        "resources.lib.nzb_manifest._find_video_name",
        side_effect=counted_find_video_name,
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "video"
    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert checked_subjects == [
        '"Sample.Movie.Name.2026-GROUP.mkv" yEnc (1/1)',
        '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
    ]


def test_video_hint_returns_before_regex_for_subjects_without_dot_marker():
    from resources.lib import nzb_manifest

    class CountingVideoExtensionRegex:  # pylint: disable=too-few-public-methods
        def __init__(self):
            self.calls = []

        def search(self, subject):
            self.calls.append(subject)

    regex = CountingVideoExtensionRegex()

    with patch("resources.lib.nzb_manifest._VIDEO_EXTENSION_TOKEN_RE", regex):
        assert (
            nzb_manifest._subject_may_be_video("Movie Name 2026 yEnc 1 of 1") is False
        )

    assert not regex.calls


def test_bare_video_subject_skips_quoted_filename_regex():
    from resources.lib import nzb_manifest

    class QuotedFilenameRegex:  # pylint: disable=too-few-public-methods
        def __init__(self):
            self.calls = []

        def search(self, subject):
            self.calls.append(subject)

    quoted_regex = QuotedFilenameRegex()
    subject = "Movie.Name.2026.2160p.BluRay.x265-GROUP.mkv yEnc (1/80)"

    with patch("resources.lib.nzb_manifest._FILENAME_RE", quoted_regex):
        name = nzb_manifest._find_video_name(subject)

    assert name == "Movie.Name.2026.2160p.BluRay.x265-GROUP.mkv"
    assert not quoted_regex.calls


def test_rejects_video_extension_substrings_before_segment_parsing():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file(
                '"Movie.Name.2026-GROUP.mkvette.nfo" yEnc (1/1)',
                [(1, 50000, "false-video@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/1)',
                [(1, 1000, "real-video@id")],
            ),
        ]
    )
    parsed_subjects = []
    original_segment_rows = nzb_manifest._segment_rows

    def counted_segment_rows(file_elem):
        parsed_subjects.append(file_elem.attrib.get("subject", ""))
        return original_segment_rows(file_elem)

    with patch(
        "resources.lib.nzb_manifest._segment_rows", side_effect=counted_segment_rows
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "video"
    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert parsed_subjects == ['"Movie.Name.2026-GROUP.mkv" yEnc (1/1)']


def test_defers_archive_segment_parsing_when_video_candidate_is_healthy():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "video-a@id"), (2, 5000, "video-b@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.part001.rar" yEnc (1/1)',
                [(1, 100, "rar-a@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.part002.rar" yEnc (1/1)',
                [(1, 100, "rar-b@id")],
            ),
        ]
    )
    parsed_subjects = []
    original_segment_rows = nzb_manifest._segment_rows

    def counted_segment_rows(file_elem):
        parsed_subjects.append(file_elem.attrib.get("subject", ""))
        return original_segment_rows(file_elem)

    with patch(
        "resources.lib.nzb_manifest._segment_rows", side_effect=counted_segment_rows
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "video"
    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert parsed_subjects == ['"Movie.Name.2026-GROUP.mkv" yEnc (1/2)']


def test_defers_archive_subject_classification_when_video_candidate_is_healthy():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "video-a@id"), (2, 5000, "video-b@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.part001.rar" yEnc (1/1)',
                [(1, 100, "rar-a@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.part002.rar" yEnc (1/1)',
                [(1, 100, "rar-b@id")],
            ),
        ]
    )
    archive_subjects = []
    original_find_archive_base = nzb_manifest._find_archive_base

    def counted_find_archive_base(subject):
        archive_subjects.append(subject)
        return original_find_archive_base(subject)

    with patch(
        "resources.lib.nzb_manifest._find_archive_base",
        side_effect=counted_find_archive_base,
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "video"
    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert not archive_subjects


def test_defers_archive_hint_scanning_when_video_candidate_is_healthy():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file(
                '"Movie.Name.2026-GROUP.mkv" yEnc (1/2)',
                [(1, 5000, "video-a@id"), (2, 5000, "video-b@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.part001.rar" yEnc (1/1)',
                [(1, 100, "rar-a@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.part002.rar" yEnc (1/1)',
                [(1, 100, "rar-b@id")],
            ),
        ]
    )
    archive_hint_subjects = []
    original_subject_may_be_archive = nzb_manifest._subject_may_be_archive

    def counted_subject_may_be_archive(subject):
        archive_hint_subjects.append(subject)
        return original_subject_may_be_archive(subject)

    with patch(
        "resources.lib.nzb_manifest._subject_may_be_archive",
        side_effect=counted_subject_may_be_archive,
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "video"
    assert manifest["video_name"] == "Movie.Name.2026-GROUP.mkv"
    assert not archive_hint_subjects


def test_archive_fallback_skips_classification_for_non_archive_subjects():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file('"Movie.Name.2026-GROUP.nfo" yEnc (1/1)', [(1, 100, "nfo@id")]),
            _file('"Movie.Name.2026-GROUP.par2" yEnc (1/1)', [(1, 200, "par2@id")]),
            _file('"Movie.Name.2026-GROUP.sfv" yEnc (1/1)', [(1, 300, "sfv@id")]),
            _file('"Artwork.Release.2026-GROUP.jpg" yEnc (1/1)', [(1, 400, "jpg@id")]),
            _file(
                '"Movie.Name.2026-GROUP.part001.rar" yEnc (1/1)',
                [(1, 1000, "rar-a@id")],
            ),
            _file(
                '"Movie.Name.2026-GROUP.part002.rar" yEnc (1/1)',
                [(1, 1000, "rar-b@id")],
            ),
        ]
    )
    archive_subjects = []
    original_find_archive_base = nzb_manifest._find_archive_base

    def counted_find_archive_base(subject):
        archive_subjects.append(subject)
        return original_find_archive_base(subject)

    with patch(
        "resources.lib.nzb_manifest._find_archive_base",
        side_effect=counted_find_archive_base,
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "archive"
    assert manifest["archive_base_name"] == "movie name 2026 group"
    assert archive_subjects == [
        '"Movie.Name.2026-GROUP.part001.rar" yEnc (1/1)',
        '"Movie.Name.2026-GROUP.part002.rar" yEnc (1/1)',
    ]


def test_archive_hint_returns_before_specific_scans_without_r_marker():
    from resources.lib import nzb_manifest

    class CountingLower(str):
        def __new__(cls, value, find_calls):
            obj = str.__new__(cls, value)
            obj.find_calls = find_calls
            return obj

        def find(self, substring, *args):
            self.find_calls.append(substring)
            return super().find(substring, *args)

    class CountingSubject(str):
        def __new__(cls, value):
            obj = str.__new__(cls, value)
            obj.find_calls = []
            return obj

        def lower(self):
            return CountingLower(str(self).lower(), self.find_calls)

    subject = CountingSubject('"Movie.Name.2026-GROUP.nfo" yEnc (1/1)')

    assert nzb_manifest._subject_may_be_archive(subject) is False
    assert not subject.find_calls


def test_archive_hint_rejects_rdigit_false_positive_boundaries():
    from resources.lib import nzb_manifest

    false_positive_files = [
        _file(
            '"Movie.Name.2026-GROUP.r12x.extra{:02d}.nfo" yEnc (1/1)'.format(index),
            [(1, 100, "false-{}@id".format(index))],
        )
        for index in range(5)
    ]
    xml = _nzb_xml(
        false_positive_files
        + [
            _file('"Movie.Name.2026-GROUP.r00" yEnc (1/1)', [(1, 1000, "r00@id")]),
            _file('"Movie.Name.2026-GROUP.r01" yEnc (1/1)', [(1, 1000, "r01@id")]),
        ]
    )
    archive_subjects = []
    original_find_archive_base = nzb_manifest._find_archive_base

    def counted_find_archive_base(subject):
        archive_subjects.append(subject)
        return original_find_archive_base(subject)

    with patch(
        "resources.lib.nzb_manifest._find_archive_base",
        side_effect=counted_find_archive_base,
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "archive"
    assert manifest["archive_base_name"] == "movie name 2026 group"
    assert archive_subjects == [
        '"Movie.Name.2026-GROUP.r00" yEnc (1/1)',
        '"Movie.Name.2026-GROUP.r01" yEnc (1/1)',
    ]


def test_single_file_archive_group_skips_redundant_group_sort_key_pass():
    class ArchiveRow:  # pylint: disable=too-few-public-methods
        def __init__(self, number, size, msgid, key_accesses):
            self.number = number
            self.size = size
            self.msgid = msgid
            self.key_accesses = key_accesses

        def __getitem__(self, index):
            if index == 0:
                self.key_accesses.append(index)
                return self.number
            if index == 1:
                return self.size
            if index == 2:
                self.key_accesses.append(index)
                return self.msgid
            raise IndexError(index)

    key_accesses = []
    rows = [
        ArchiveRow(index, 100, "archive-{}@id".format(index), key_accesses)
        for index in range(1, 11)
    ]
    xml = _nzb_xml(
        [_file('"Movie.Name.2026-GROUP.rar" yEnc (1/10)', [(1, 100, "stub@id")])]
    )

    with patch("resources.lib.nzb_manifest._segment_rows", return_value=rows), patch(
        "resources.lib.nzb_manifest._digest_articles", return_value="digest"
    ):
        manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == "archive"
    assert manifest["article_count"] == len(rows)
    assert not key_accesses


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


def test_ordered_segment_rows_skip_sort_key_pass():
    from resources.lib import nzb_manifest

    xml = _nzb_xml(
        [
            _file(
                '"Movie.mkv" yEnc (1/4)',
                [
                    (1, 1000, "a@id"),
                    (2, 1000, "b@id"),
                    (3, 1000, "c@id"),
                    (4, 1000, "d@id"),
                ],
            )
        ]
    )
    sort_key_calls = []

    def counted_sort_key(row):
        sort_key_calls.append(row)
        return row[0], row[2]

    with patch(
        "resources.lib.nzb_manifest._segment_row_sort_key",
        side_effect=counted_sort_key,
    ):
        manifest = nzb_manifest.extract_nzb_video_manifest(xml)

    assert manifest["article_count"] == 4
    assert not sort_key_calls


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
        timeout=20,
        max_bytes=100 * 1024 * 1024,
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


def test_extracts_obfuscated_dominant_file_as_video_manifest():
    """Heavily obfuscated uploads strip the .mkv extension off every file but
    still ship a single dominant payload that holds the actual movie.
    Recognize that as a video manifest so duplicate-release peers can match
    via the size-tolerant peer matcher.
    """
    xml = _nzb_xml(
        [
            _file(
                '"abc123def456.par2" yEnc (1/1)',
                [(1, 50000, "par2-a@id")],
            ),
            _file(
                '"abc123def456" yEnc (1/3)',
                [
                    (1, 950000000, "blob-a@id"),
                    (2, 950000000, "blob-b@id"),
                    (3, 100000000, "blob-c@id"),
                ],
            ),
            _file(
                '"abc123def456.nfo" yEnc (1/1)',
                [(1, 1000, "nfo-a@id")],
            ),
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["unsupported_reason"] == ""
    assert manifest["payload_kind"] == "video"
    assert manifest["group_bytes"] == 2000000000
    assert manifest["video_bytes"] == 2000000000
    assert manifest["article_count"] == 3
    assert manifest["article_digest"]


def test_obfuscated_blob_below_threshold_stays_unsupported_when_uniform_count_low():
    """A handful of similarly-sized files are not enough to infer the payload.
    Real split-payload obfuscation runs into many dozens of pieces; reject
    short uniform-size collections as ambiguous.
    """
    xml = _nzb_xml(
        [
            _file(
                '"part.{:03d}" yEnc (1/1)'.format(i),
                [(1, 1000000, "p{}@id".format(i))],
            )
            for i in range(5)
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == ""
    assert manifest["unsupported_reason"] == "no_video_file"


def test_extracts_split_payload_obfuscation_as_video_manifest():
    """Heavily obfuscated uploads split the payload across many quasi-uniform
    files (numeric extensions or .7z.NNN). Recognize them as a video-kind
    manifest using the summed payload bytes so duplicate-release peers can
    match via the size-tolerant peer matcher.
    """
    xml = _nzb_xml(
        [
            _file('"abc123.par2" yEnc (1/1)', [(1, 50000, "par2-a@id")]),
            _file('"abc123.sfv" yEnc (1/1)', [(1, 1000, "sfv-a@id")]),
        ]
        + [
            _file(
                '"abc123.{:03d}" yEnc (1/1)'.format(i),
                [(1, 471800000, "blob-{}@id".format(i))],
            )
            for i in range(50)
        ]
        + [
            _file(
                '"abc123.050" yEnc (1/1)',
                [(1, 200000000, "blob-tail@id")],
            )
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["unsupported_reason"] == ""
    assert manifest["payload_kind"] == "video"
    assert manifest["group_bytes"] == 50 * 471800000 + 200000000
    assert manifest["video_bytes"] == manifest["group_bytes"]
    assert manifest["article_count"] == 51
    assert manifest["article_digest"]


def test_synthetic_video_manifest_rejects_tiny_stub_nzb():
    """Stub uploads with kilobyte-scale payloads should not classify as video.
    A 4K REMUX peer band built from a stub will mismatch every real release.
    """
    xml = _nzb_xml(
        [
            _file('"obfuscated.7z.001" yEnc (1/1)', [(1, 36916, "stub-a@id")]),
            _file('"obfuscated.par2" yEnc (1/1)', [(1, 456, "par2-a@id")]),
            _file(
                '"obfuscated.vol00+01.par2" yEnc (1/8)',
                [(i, 655425, "par2-{}@id".format(i)) for i in range(1, 9)],
            ),
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == ""
    assert manifest["unsupported_reason"] == "no_video_file"


def test_split_payload_detection_rejects_high_size_variance():
    """Releases where payload-file sizes vary wildly are not the obfuscation
    pattern we are inferring — could be a multi-file pack with extras. Stay
    unsupported instead of guessing a single grouped payload.
    """
    sizes = [10000000, 50000000, 200000000, 1000000000, 5000000000] * 3
    xml = _nzb_xml(
        [
            _file(
                '"mixed.{:03d}" yEnc (1/1)'.format(i),
                [(1, size, "blob-{}@id".format(i))],
            )
            for i, size in enumerate(sizes)
        ]
    )

    manifest = extract_nzb_video_manifest(xml)

    assert manifest["payload_kind"] == ""
    assert manifest["unsupported_reason"] == "no_video_file"
