#!/usr/bin/env bash
# Installs Claude Privacy Check for the current user via run.py --setup.
# No root, no pip, no dependencies. Undo with ./install.sh --uninstall
set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--uninstall" ]; then
  exec python3 "$APPDIR/run.py" --setup-uninstall
fi

command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required." >&2; exit 1; }
python3 -c 'import tkinter' 2>/dev/null || \
  echo "Note: Tkinter is missing — the command line works, the window does not."

exec python3 "$APPDIR/run.py" --setup
