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
    python3 -m pip install ${pip_flags+"${pip_flags[@]}"} -r requirements-test.txt "ruff>=0.15" "black>=24"

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

# Run all tests (excluding integration, functional, and extreme tests)
test:
    python3 -m pytest tests/ -v --tb=short -m "not integration and not functional and not extreme"

# Run tests with coverage
test-verbose:
    python3 -m pytest tests/ -v --tb=long -m "not integration and not functional and not extreme"

# Run integration tests against a real ffmpeg binary. Spawns the
# actual fmp4 HLS producer pipeline against a tiny test MKV
# generated on the fly via ffmpeg lavfi sources, validates that
# init.mp4 + segments are produced and well-formed. Catches every
# class of bug we've hit on this spike (absolute path, -strict -2,
# analyzeduration, delay_moov, codec frame size) at PR time. Skips
# automatically if no ffmpeg is on PATH.
test-integration:
    python3 -m pytest tests/ -v --tb=long -m integration

# Run dev-box functional tests against live configured services.
# Requires local .env credentials and may use real Hydra/indexer responses.
functional-test:
    python3 -m pytest tests/test_functional_fallback_playback.py -v --tb=long -m functional

# Run a heavier dev-box fallback sample across random IMDb Top 50 movies.
# Prefer FrameStor/FraMeSToR releases; otherwise use the most duplicated group.
functional-test-top-imdb:
    python3 -m pytest tests/test_functional_fallback_playback.py::test_functional_imdb_top50_random_sample_fallback_playback -v -s --tb=long -m functional

# Interactively create the .env file consumed by `just extreme-functional-test`.
# Asks for NNTP credentials, NZBHydra2 URL+API key, WebDAV credentials, and
# TMDB API key. Defaults shown in [brackets]; press enter to accept. Secrets
# (passwords, API keys) are read silently. Writes mode 600. If the target
# already exists, prompts before overwriting. Honors EXTREME_ENV_FILE for
# alternate write locations.
setup-extreme-functional-test:
    #!/usr/bin/env bash
    set -euo pipefail

    target="${EXTREME_ENV_FILE:-.env}"

    echo "================================================================"
    echo " Extreme Functional Test - .env setup"
    echo "================================================================"
    echo "Will write to: $target"
    echo "(set EXTREME_ENV_FILE in your shell to write elsewhere)"
    echo ""

    if [[ -f "$target" ]]; then
        echo "WARNING: $target already exists."
        read -r -p "Overwrite? [y/N]: " confirm
        case "$confirm" in
            [Yy]|[Yy][Ee][Ss]) ;;
            *) echo "Aborted (existing file untouched)."; exit 1 ;;
        esac
    fi

    # ask <prompt> <default> [secret]   -> echoes the answer (default if empty)
    ask() {
        local prompt="$1" default="$2" silent="${3:-}" answer=""
        local label="$prompt"
        if [[ -n "$default" ]]; then
            label="$prompt [$default]"
        fi
        if [[ "$silent" == "secret" ]]; then
            read -r -s -p "$label: " answer
            echo "" >&2
        else
            read -r -p "$label: " answer
        fi
        if [[ -z "$answer" ]]; then
            answer="$default"
        fi
        printf '%s' "$answer"
    }

    # ask_required <prompt> [secret]   -> reprompts until non-empty
    ask_required() {
        local prompt="$1" silent="${2:-}" answer=""
        while [[ -z "$answer" ]]; do
            if [[ "$silent" == "secret" ]]; then
                read -r -s -p "$prompt (required): " answer
                echo "" >&2
            else
                read -r -p "$prompt (required): " answer
            fi
            if [[ -z "$answer" ]]; then
                echo "  (cannot be empty; please retry)" >&2
            fi
        done
        printf '%s' "$answer"
    }

    emit_env() {
        printf '%s=%q\n' "$1" "$2"
    }

    echo "--- NZBHydra2 indexer ---"
    HYDRA_URL=$(ask "Hydra URL" "http://192.168.1.93:5076")
    HYDRA_API_KEY=$(ask_required "Hydra API key" secret)

    echo ""
    echo "--- NNTP provider (Eweka, etc.) ---"
    NNTP_HOST=$(ask "NNTP host" "news.eweka.nl")
    NNTP_USE_SSL=$(ask "NNTP SSL (true/false)" "false")
    if [[ "$NNTP_USE_SSL" == "true" ]]; then
        NNTP_PORT_DEFAULT="563"
    else
        NNTP_PORT_DEFAULT="119"
    fi
    NNTP_PORT=$(ask "NNTP port" "$NNTP_PORT_DEFAULT")
    NNTP_USER=$(ask_required "NNTP username")
    NNTP_PASS=$(ask_required "NNTP password" secret)
    NNTP_CONNS=$(ask "NNTP connection count" "50")

    echo ""
    echo "--- WebDAV credentials (served by nzbdav-rs) ---"
    WEBDAV_USERNAME=$(ask "WebDAV username" "admin")
    WEBDAV_PASSWORD=$(ask "WebDAV password" "devpass" secret)

    echo ""
    echo "--- TMDB ---"
    TMDB_API_KEY=$(ask_required "TMDB API key" secret)

    echo ""
    echo "--- Misc ---"
    NZBDAV_API_KEY=$(ask "nzbdav-rs API key (any string; must match docker-compose env)" "smokekey-dev-only")

    # Build content in a tempfile, validate, then atomically move into place.
    tmpfile="$(mktemp)"
    trap 'rm -f "$tmpfile"' EXIT

    {
        echo "# Generated by \`just setup-extreme-functional-test\`"
        echo "# Mode 600. Do not commit."
        echo ""
        echo "# NZBHydra2 instance"
        emit_env "HYDRA_URL" "$HYDRA_URL"
        emit_env "HYDRA_API_KEY" "$HYDRA_API_KEY"
        echo ""
        echo "# nzbdav-rs API key (must match docker-compose env)"
        emit_env "NZBDAV_API_KEY" "$NZBDAV_API_KEY"
        echo ""
        echo "# NNTP provider"
        emit_env "NNTP_HOST" "$NNTP_HOST"
        emit_env "NNTP_USE_SSL" "$NNTP_USE_SSL"
        emit_env "NNTP_PORT" "$NNTP_PORT"
        emit_env "NNTP_USER" "$NNTP_USER"
        emit_env "NNTP_PASS" "$NNTP_PASS"
        emit_env "NNTP_CONNS" "$NNTP_CONNS"
        echo ""
        echo "# WebDAV credentials served by nzbdav-rs"
        emit_env "WEBDAV_USERNAME" "$WEBDAV_USERNAME"
        emit_env "WEBDAV_PASSWORD" "$WEBDAV_PASSWORD"
        echo ""
        echo "# TMDB API key (TMDBHelper requires this)"
        emit_env "TMDB_API_KEY" "$TMDB_API_KEY"
        echo ""
        echo "# Optional knobs (uncomment to use):"
        echo "# EXTREME_SEED=42"
        echo "# EXTREME_MAX_RESUME_SECONDS=30"
        echo "# EXTREME_MAX_FREEZE_SECONDS=10"
        echo "# EXTREME_ENV_FILE=/path/to/your/.env"
    } > "$tmpfile"

    # Validate the file is sourceable
    if ! ( set -a; source "$tmpfile" >/dev/null 2>&1; set +a ); then
        echo "Internal error: generated env file failed to source. Aborting." >&2
        exit 1
    fi

    mv "$tmpfile" "$target"
    chmod 600 "$target"
    trap - EXIT

    echo ""
    echo "================================================================"
    echo " OK  Wrote $target (mode 600, $(wc -l < "$target" | tr -d ' ') lines)"
    echo "================================================================"
    echo ""
    echo "Next steps:"
    echo "  1. Verify the file: cat $target"
    echo "  2. Run the test:    just extreme-functional-test"

# Run the extreme end-to-end fault-recovery test (20+ minutes, real Eweka, real Hydra).
# Brings up a self-contained docker-compose stack, installs TMDBHelper from the
# jurialmunkey repository and the nzbdav addon from `just repo-zip`, picks a random
# IMDb Top 50 movie with a wide fallback pool, drives playback through TMDBHelper
# for 20 minutes while injecting 5 distinct fault types on a random schedule,
# measures user-visible interruption, writes reports to docs/reports/run-<ts>/,
# and tears down. Requires .env with HYDRA_URL/HYDRA_API_KEY/NNTP_USER/NNTP_PASS/
# TMDB_API_KEY (or run `just setup-extreme-functional-test` first).
extreme-functional-test:
    #!/usr/bin/env bash
    set -euo pipefail
    env_file="${EXTREME_ENV_FILE:-.env}"
    if [[ ! -f "$env_file" ]]; then
        echo "FATAL: env file not found: $env_file" >&2
        echo "Copy .env.example to .env and fill in real values." >&2
        exit 2
    fi
    env_snapshot="$(mktemp)"
    trap 'rm -f "$env_snapshot"' EXIT
    export -p > "$env_snapshot"
    set -a; source "$env_file"; set +a
    source "$env_snapshot"
    python3 -m pytest tests/test_extreme_functional.py -v -s --tb=long -m extreme

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
