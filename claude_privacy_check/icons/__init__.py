"""Application icon paths (bundled PNGs / SVG)."""

from __future__ import annotations

import os

ICON_NAME = "claude-privacy-check"
ICONS_DIR = os.path.dirname(os.path.realpath(__file__))
SIZES = (16, 22, 24, 32, 48, 64, 128, 256)


def icon_png(size=None):
    """Path to a PNG icon, or None if missing.

    Without *size*, returns the 256px master. With *size*, prefers that
    exact file and falls back to the master.
    """
    if size is not None:
        path = os.path.join(ICONS_DIR, f"{ICON_NAME}-{size}.png")
        if os.path.isfile(path):
            return path
    path = os.path.join(ICONS_DIR, f"{ICON_NAME}.png")
    return path if os.path.isfile(path) else None


def icon_svg():
    path = os.path.join(ICONS_DIR, f"{ICON_NAME}.svg")
    return path if os.path.isfile(path) else None


def icon_for_notify():
    """Absolute path for notify-send --icon (theme name after install)."""
    return icon_png(48) or icon_png()
