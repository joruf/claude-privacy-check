#!/usr/bin/env bash
# Installs Claude Privacy Check for the current user:
#   symlink in ~/.local/bin, menu entry, optional monitoring.
# No root, no pip, no dependencies. Undo with ./install.sh --uninstall
set -euo pipefail

APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/claude-privacy-check.desktop"
LINK="$BIN_DIR/claude-privacy-check"

uninstall() {
  python3 "$APPDIR/run.py" --watch-uninstall || true
  rm -f "$LINK" "$DESKTOP_FILE"
  command -v update-desktop-database >/dev/null && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
  echo "Removed. The baseline in ~/.local/share/claude-privacy-check/ was kept."
}

if [ "${1:-}" = "--uninstall" ]; then uninstall; exit 0; fi

command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "Python 3.10 or newer is required." >&2; exit 1; }
python3 -c 'import tkinter' 2>/dev/null || \
  echo "Note: Tkinter is missing — the command line works, the window does not."

mkdir -p "$BIN_DIR" "$DESKTOP_DIR"
ln -sfn "$APPDIR/run.py" "$LINK"
chmod +x "$APPDIR/run.py"
echo "Command:     $LINK"

sed "s|@APPDIR@|$APPDIR|g" "$APPDIR/packaging/claude-privacy-check.desktop" \
  > "$DESKTOP_FILE"
command -v update-desktop-database >/dev/null && \
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
echo "Menu entry:  $DESKTOP_FILE"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Note: $BIN_DIR is not in PATH." ;;
esac

if [ ! -e "${XDG_DATA_HOME:-$HOME/.local/share}/claude-privacy-check/baseline.json" ]; then
  echo
  echo "No baseline yet. Record the current state as the reference point with:"
  echo "  claude-privacy-check --init"
fi
echo
echo "Continuous monitoring (optional):  claude-privacy-check --watch-install"
