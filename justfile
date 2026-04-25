# NZB-DAV Kodi Addon

# Run all tests (excluding integration tests that require a real ffmpeg)
test:
    python3 -m pytest tests/ -v --tb=short -m "not integration"

# Run tests with coverage
test-verbose:
    python3 -m pytest tests/ -v --tb=long -m "not integration"

# Run integration tests against a real ffmpeg binary. Spawns the
# actual fmp4 HLS producer pipeline against a tiny test MKV
# generated on the fly via ffmpeg lavfi sources, validates that
# init.mp4 + segments are produced and well-formed. Catches every
# class of bug we've hit on this spike (absolute path, -strict -2,
# analyzeduration, delay_moov, codec frame size) at PR time. Skips
# automatically if no ffmpeg is on PATH.
test-integration:
    python3 -m pytest tests/ -v --tb=long -m integration

# Lint the codebase (matches GitHub CI: ruff + black + pylint)
lint:
    ruff check plugin.video.nzbdav/ tests/ --exclude="plugin.video.nzbdav/resources/lib/ptt/"
    black --check plugin.video.nzbdav/ tests/ --exclude="ptt/"
    pylint $(git ls-files '*.py')

# Auto-fix lint issues
lint-fix:
    ruff check plugin.video.nzbdav/ tests/ --exclude="plugin.video.nzbdav/resources/lib/ptt/" --fix
    black plugin.video.nzbdav/ tests/ --exclude="ptt/"

# Build the addon zip for Kodi installation
release:
    python3 scripts/build_zip.py

# Run tests then build release
ship: test release

# Generate Kodi repository in dist/
repo: release
    python3 scripts/generate_repo.py --output-dir dist

# Copy the repository zip to cwd for easy access
repo-zip: repo
    cp dist/repository.nzbdav/repository.nzbdav-*.zip .
    @ls -lh repository.nzbdav-*.zip

# Clean build artifacts
clean:
    rm -f plugin.video.nzbdav*.zip
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true

# Run the same checks as GitHub CI (lint + test)
ci: lint test

# Clean everything including dist
dist-clean: clean
    rm -rf dist/
