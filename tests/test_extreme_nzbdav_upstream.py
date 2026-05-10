from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "tests" / "extreme" / "compose" / "docker-compose.yml"
SEED_SCRIPT = REPO_ROOT / "tests" / "extreme" / "scripts" / "seed_nzbdav.sh"
ADDON_SETTINGS = (
    REPO_ROOT / "tests" / "extreme" / "fixtures" / "addon-settings-template.xml"
)
STORAGE_DISCOVERY = (
    REPO_ROOT / "tests" / "extreme" / "scripts" / "_storage_discovery.py"
)
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def test_extreme_compose_uses_upstream_nzbdav_image_with_internal_api_key():
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    nzbdav_service = compose.split("  nzbdav-rs:", 1)[1].split("  fault-proxy:", 1)[0]

    assert "image: nzbdav/nzbdav:latest" in nzbdav_service
    assert "FRONTEND_BACKEND_API_KEY: ${NZBDAV_API_KEY}" in nzbdav_service
    assert '["CMD", "curl", "-fs", "http://localhost:8080/health"]' in nzbdav_service
    assert 'HTTP_PROXY: ""' in nzbdav_service
    assert 'ALL_PROXY: ""' in nzbdav_service


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


def test_extreme_addon_fixture_prefers_same_framestor_avc_profile_as_preflight():
    template = ADDON_SETTINGS.read_text(encoding="utf-8")
    harness = (REPO_ROOT / "tests" / "test_extreme_functional.py").read_text(
        encoding="utf-8"
    )

    for expected in [
        '<setting id="filter_1080p">true</setting>',
        '<setting id="filter_720p">false</setting>',
        '<setting id="filter_avc">true</setting>',
        '<setting id="filter_av1">false</setting>',
        '<setting id="filter_require_keywords">framestor,1080p,avc,remux</setting>',
        '<setting id="max_results">250</setting>',
    ]:
        assert expected in template

    assert '<setting id="filter_release_group">' not in template

    for expected in [
        '"filter_1080p": "true"',
        '"filter_720p": "false"',
        '"filter_avc": "true"',
        '"filter_av1": "false"',
        '"filter_require_keywords": "framestor,1080p,avc,remux"',
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
