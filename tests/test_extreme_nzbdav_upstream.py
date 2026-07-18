from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTREME = REPO_ROOT / "tests-extensive" / "extreme"
COMPOSE_FILE = EXTREME / "compose" / "docker-compose.yml"
SEED_SCRIPT = EXTREME / "scripts" / "seed_nzbdav.sh"
ADDON_SETTINGS = EXTREME / "fixtures" / "addon-settings-template.xml"
STORAGE_DISCOVERY = REPO_ROOT / "tests" / "extreme_harness" / "_storage_discovery.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def test_extreme_compose_wraps_live_lan_nzbdav_not_a_container():
    """The stack must not containerise nzbdav or NZBHydra2: the fault
    proxy wraps the live LAN nzbdav from .env's NZBDAV_URL, and the
    addon settings template points at the same URL."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "  nzbdav-rs:" not in compose
    assert "  hydra2:" not in compose
    assert "FAULT_PROXY_UPSTREAM: ${NZBDAV_URL:?" in compose

    template = ADDON_SETTINGS.read_text(encoding="utf-8")
    assert '<setting id="nzbdav_url">${NZBDAV_URL}</setting>' in template

    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "NZBDAV_URL=" in env_example


def test_seed_script_targets_upstream_config_api_not_legacy_servers_api():
    script = SEED_SCRIPT.read_text(encoding="utf-8")

    assert "/api/update-config" in script
    assert "usenet.providers" in script
    assert "webdav.user" in script
    assert "webdav.pass" in script
    assert "-o /dev/null" in script
    assert "NNTP_USE_SSL" in script
    assert "/api/servers" not in script


def test_extreme_addon_fixture_uses_upstream_webdav_root():
    template = ADDON_SETTINGS.read_text(encoding="utf-8")

    assert '<setting id="webdav_url">http://fault-proxy:8280</setting>' in template
    assert "http://fault-proxy:8280/dav" not in template


def test_extreme_addon_fixture_prefers_same_x264_encode_profile_as_preflight():
    template = ADDON_SETTINGS.read_text(encoding="utf-8")
    harness = (REPO_ROOT / "tests-extensive" / "test_extreme_functional.py").read_text(
        encoding="utf-8"
    )

    for expected in [
        '<setting id="filter_1080p">true</setting>',
        '<setting id="filter_720p">false</setting>',
        '<setting id="filter_avc">true</setting>',
        '<setting id="filter_av1">false</setting>',
        '<setting id="filter_require_keywords">1080p,bluray,x264</setting>',
        '<setting id="max_results">250</setting>',
    ]:
        assert expected in template

    assert '<setting id="filter_release_group">' not in template

    for expected in [
        '"filter_1080p": "true"',
        '"filter_720p": "false"',
        '"filter_avc": "true"',
        '"filter_av1": "false"',
        '"filter_require_keywords": "1080p,bluray,x264"',
        '"filter_release_group": ""',
        '"max_results": "250"',
    ]:
        assert expected in harness


def test_extreme_storage_discovery_uses_upstream_content_root():
    script = STORAGE_DISCOVERY.read_text(encoding="utf-8")

    assert "/content/" in script
    assert "/dav/content/" not in script
    assert 'len("/dav")' not in script


def test_extreme_env_example_defaults_to_plain_nntp_for_upstream_image():
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "NNTP_USE_SSL=false" in env_example
    assert "NNTP_PORT=119" in env_example
