#!/usr/bin/env bash
# Bootstrap a project-local virtualenv and register GNOME shortcuts.
#
#   ./setup.sh              # first install or after pulling updates
#   ./setup.sh --shortcuts  # re-register shortcuts only (venv already exists)
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

if [[ "${1:-}" != "--shortcuts" ]]; then
    echo "Creating project virtualenv (.venv) and installing dependencies…"
    uv sync
    echo
    echo "Running setup check…"
    .venv/bin/python dictation.py doctor
    echo
fi

echo "Installing GNOME keyboard shortcuts…"
python3 install-shortcuts.py

echo
echo "Done. Shortcuts:"
echo "  Super+D        simple dictation"
echo "  Super+W        full (overlay + ensemble)"
echo "  Super+Shift+D  resend last recording"
