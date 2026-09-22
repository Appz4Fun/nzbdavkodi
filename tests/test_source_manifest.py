from __future__ import annotations

from unittest.mock import patch

from resources.lib.source_manifest import load_source_manifest


@patch("resources.lib.source_manifest.http_get")
def test_load_source_manifest_returns_primary_and_all_sources(mock_get) -> None:
    mock_get.return_value = (
        '{"title":"The Road","primary_url":"https://h/one",'
        '"source_urls":["https://h/one","https://h/two"]}'
    )

    manifest = load_source_manifest("http://btad/manifest")

    assert manifest == ("The Road", "https://h/one", ["https://h/one", "https://h/two"])


@patch("resources.lib.source_manifest.http_get")
def test_load_source_manifest_rejects_non_http_source(mock_get) -> None:
    mock_get.return_value = (
        '{"title":"x","primary_url":"file:///tmp/x","source_urls":["file:///tmp/x"]}'
    )

    assert load_source_manifest("http://btad/manifest") is None


@patch("resources.lib.source_manifest.http_get")
def test_load_source_manifest_rejects_non_http_manifest_url(mock_get) -> None:
    """The addon must not fetch arbitrary local or plugin URLs as manifests."""
    assert load_source_manifest("file:///tmp/manifest.json") is None
    mock_get.assert_not_called()
