#!/usr/bin/env bash
# AgentsHive init bootstrap for Mac/Linux.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.sh | bash -s -- my-project
#
# Or, more portable (works with `sh` too):
#   sh -c "$(curl -fsSL https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/install.sh)" -- my-project
#
# Mirror of scripts/install.ps1 (PowerShell). Same UX: downloads
# init_project.py to a temp file, runs it with your args, cleans up.
#
# Why a wrapper instead of `curl ... | python -`?
# Piping multi-line Python through some shells can mangle string literals
# (the same way PowerShell did when this project started; see install.ps1
# header). Downloading to a real file preserves bytes verbatim.

set -eu

INIT_URL="https://raw.githubusercontent.com/Zecruu/ZecruAgentsHive/main/scripts/init_project.py"

# Pick a Python interpreter. Mac and most Linux distros expose python3;
# the bare `python` is uncommon. Error clearly if neither is present so
# the user sees a real fix path (install Python 3 from python.org or
# their package manager) rather than a cryptic command-not-found.
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "ERROR: neither python3 nor python found on PATH." >&2
    echo "Install Python 3 (python.org / brew install python / apt install python3) and re-run." >&2
    exit 1
fi

# Pick a downloader. curl is on every macOS install; wget is the common
# Linux fallback. Either is fine.
if command -v curl >/dev/null 2>&1; then
    DOWNLOAD="curl -fsSL -o"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOAD="wget -qO"
else
    echo "ERROR: neither curl nor wget found on PATH. Install one and re-run." >&2
    exit 1
fi

TMP=$(mktemp -t agentshive_init_XXXXXX.py 2>/dev/null || mktemp /tmp/agentshive_init_XXXXXX.py)
trap 'rm -f "$TMP"' EXIT

# Download into the temp file. The first arg to $DOWNLOAD is the URL
# for curl, but wget reverses it — handle both forms.
case "$DOWNLOAD" in
    curl*) $DOWNLOAD "$TMP" "$INIT_URL" ;;
    wget*) $DOWNLOAD "$TMP" "$INIT_URL" ;;
esac

# Pass all args through to init_project.py.
exec "$PYTHON" "$TMP" "$@"
