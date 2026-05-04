# NZB-DAV Kodi Addon

# Install local development dependencies needed by the other recipes
make-dev:
    #!/usr/bin/env bash
    set -euo pipefail

    echo "Installing Python test and lint dependencies..."
    pip_flags=()
    if python3 -m pip install --help | grep -q -- "--break-system-packages"; then
        pip_flags+=(--break-system-packages)
    fi
    python3 -m pip install "${pip_flags[@]}" -r requirements-test.txt "ruff>=0.15" "black>=24"

    if [[ "$(uname -s)" == "Darwin" ]]; then
        if ! command -v brew >/dev/null 2>&1; then
            echo "Homebrew is required on macOS to install ffmpeg/x265." >&2
            echo "Install it from https://brew.sh/ and rerun: just make-dev" >&2
            exit 1
        fi

        echo "Installing Homebrew tools used by just recipes..."
        brew install just x265
        if ! command -v ffmpeg >/dev/null 2>&1; then
            brew install ffmpeg
        fi

        ffmpeg_formula="$(brew list --formula --full-name | grep -E '(^|/)ffmpeg$' | head -n 1 || true)"
        brew upgrade just x265 || true
        if [[ -n "${ffmpeg_formula}" ]]; then
            brew upgrade "${ffmpeg_formula}" || true
        fi

        if ! ffmpeg -version >/dev/null 2>&1; then
            echo "ffmpeg failed to start; reinstalling ffmpeg to refresh dylib links..."
            brew reinstall "${ffmpeg_formula:-ffmpeg}"
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        echo "Installing ffmpeg with apt-get..."
        sudo apt-get update
        sudo apt-get install -y ffmpeg
    elif command -v dnf >/dev/null 2>&1; then
        echo "Installing ffmpeg with dnf..."
        sudo dnf install -y ffmpeg
    elif command -v pacman >/dev/null 2>&1; then
        echo "Installing ffmpeg with pacman..."
        sudo pacman -Sy --needed ffmpeg
    elif ! command -v ffmpeg >/dev/null 2>&1; then
        echo "ffmpeg is required for just test-integration; install it and rerun make-dev." >&2
        exit 1
    fi

    echo "Verifying required command-line tools..."
    python3 -m pytest --version >/dev/null
    ruff --version >/dev/null
    black --version >/dev/null
    pylint --version >/dev/null
    ffmpeg -version >/dev/null

    echo "Development dependencies are installed."

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
