"""About information and the data behind the About dialog.

Kept separate from the interface so the values can be asserted in a test
without starting Tk, the same way the other projects here do it.
"""

from __future__ import annotations

from . import __version__

APP_NAME = "Claude Privacy Check"
APP_VERSION = __version__
APP_LICENSE = "Apache-2.0"

ABOUT_AUTHOR = "Joachim Ruf"
ABOUT_WEBSITE = "loresoft.de"
ABOUT_GITHUB = "https://github.com/joruf/claude-privacy-check"


def normalize_about_url(url: str) -> str:
    """Give an About URL an http(s) scheme so a browser can open it.

    Args:
        url: Website or repository URL, with or without scheme.

    Returns:
        An absolute URL.
    """
    cleaned = str(url).strip()
    if not cleaned:
        return ""
    if cleaned.startswith(("http://", "https://")):
        return cleaned
    return f"https://{cleaned}"


def about_rows() -> list[tuple[str, str, str]]:
    """(label key, display text, link target) for the About dialog.

    An empty link target means the row is plain text.
    """
    return [
        ("about.version", f"{APP_VERSION} · {APP_LICENSE}", ""),
        ("about.author", ABOUT_AUTHOR, ""),
        ("about.website", ABOUT_WEBSITE, normalize_about_url(ABOUT_WEBSITE)),
        ("about.github", ABOUT_GITHUB, normalize_about_url(ABOUT_GITHUB)),
    ]


def build_about_text() -> str:
    """Plain-text rendering, used by the command line and as a fallback."""
    from .i18n import t

    lines = [f"{APP_NAME} {APP_VERSION}", "", t("about.tagline"), ""]
    lines += [f"{t(key)}: {value}" for key, value, _link in about_rows()]
    return "\n".join(lines)
