"""User-level install: symlink, menu entry, icons.

No root, no pip. Mirrors install.sh so run.py can ensure the desktop
integration is present and the GUI can show each step as it runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .core import APP_DIR
from .icons import ICON_NAME, ICONS_DIR, SIZES
from .i18n import t

HOME = os.path.expanduser("~")
BIN_DIR = os.path.join(HOME, ".local", "bin")
LINK = os.path.join(BIN_DIR, "claude-privacy-check")
DESKTOP_DIR = os.path.join(HOME, ".local", "share", "applications")
DESKTOP_FILE = os.path.join(DESKTOP_DIR, "claude-privacy-check.desktop")
DESKTOP_TEMPLATE = os.path.join(APP_DIR, "packaging", "claude-privacy-check.desktop")
HICOLOR = os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share"),
    "icons", "hicolor")
RUN_PY = os.path.join(APP_DIR, "run.py")

# Set by prepare_launch() when the process was started via run.py for the GUI.
_gui_pending = None


def _link_ok():
    try:
        return os.path.realpath(LINK) == os.path.realpath(RUN_PY)
    except OSError:
        return False


def _desktop_ok():
    if not os.path.isfile(DESKTOP_FILE):
        return False
    try:
        with open(DESKTOP_FILE, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    return RUN_PY in text and f"Icon={ICON_NAME}" in text


def _icons_ok():
    for size in SIZES:
        path = os.path.join(HICOLOR, f"{size}x{size}", "apps", f"{ICON_NAME}.png")
        if not os.path.isfile(path):
            return False
    svg = os.path.join(HICOLOR, "scalable", "apps", f"{ICON_NAME}.svg")
    return os.path.isfile(svg)


def pending_steps():
    """Ordered (step_id, translation_key) for work that is still outstanding."""
    steps = []
    if not _link_ok():
        steps.append(("link", "install.step.link"))
    if not _icons_ok():
        steps.append(("icons", "install.step.icons"))
    if not _desktop_ok():
        steps.append(("desktop", "install.step.desktop"))
    return steps


def needs_install():
    return bool(pending_steps())


def _emit(progress, key, **params):
    msg = t(key, **params)
    if progress is not None:
        progress(msg)
    return msg


def _install_link():
    os.makedirs(BIN_DIR, exist_ok=True)
    os.chmod(RUN_PY, os.stat(RUN_PY).st_mode | 0o111)
    if os.path.lexists(LINK) or os.path.islink(LINK):
        os.remove(LINK)
    os.symlink(RUN_PY, LINK)


def _install_icons():
    for size in SIZES:
        src = os.path.join(ICONS_DIR, f"{ICON_NAME}-{size}.png")
        dest_dir = os.path.join(HICOLOR, f"{size}x{size}", "apps")
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(dest_dir, f"{ICON_NAME}.png"))
    svg_src = os.path.join(ICONS_DIR, f"{ICON_NAME}.svg")
    svg_dir = os.path.join(HICOLOR, "scalable", "apps")
    os.makedirs(svg_dir, exist_ok=True)
    shutil.copy2(svg_src, os.path.join(svg_dir, f"{ICON_NAME}.svg"))
    cache = shutil.which("gtk-update-icon-cache")
    if cache:
        subprocess.run([cache, "-f", "-t", HICOLOR],
                       capture_output=True, check=False)


def _install_desktop():
    os.makedirs(DESKTOP_DIR, exist_ok=True)
    with open(DESKTOP_TEMPLATE, encoding="utf-8") as fh:
        text = fh.read().replace("@APPDIR@", APP_DIR)
    with open(DESKTOP_FILE, "w", encoding="utf-8") as fh:
        fh.write(text)
    upd = shutil.which("update-desktop-database")
    if upd:
        subprocess.run([upd, DESKTOP_DIR], capture_output=True, check=False)


def ensure(progress=None, force=False):
    """Install missing pieces. *progress* is called with a status string.

    Returns the list of step ids that were run.
    """
    steps = pending_steps() if not force else [
        ("link", "install.step.link"),
        ("icons", "install.step.icons"),
        ("desktop", "install.step.desktop"),
    ]
    if not steps:
        return []

    _emit(progress, "install.progress.start")
    done = []
    actions = {
        "link": _install_link,
        "icons": _install_icons,
        "desktop": _install_desktop,
    }
    for step_id, key in steps:
        _emit(progress, key)
        actions[step_id]()
        done.append(step_id)
    _emit(progress, "install.progress.done")
    return done


def uninstall(progress=None):
    """Remove symlink, menu entry and icons (keeps the baseline)."""
    _emit(progress, "install.progress.uninstall")
    try:
        from . import watch
        watch.uninstall()
    except Exception:  # noqa: BLE001 -- best-effort during removal
        pass
    for path in (LINK, DESKTOP_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    for size in SIZES:
        try:
            os.remove(os.path.join(HICOLOR, f"{size}x{size}", "apps",
                                   f"{ICON_NAME}.png"))
        except FileNotFoundError:
            pass
    try:
        os.remove(os.path.join(HICOLOR, "scalable", "apps", f"{ICON_NAME}.svg"))
    except FileNotFoundError:
        pass
    for cmd, args in (
        (shutil.which("gtk-update-icon-cache"), ["-f", "-t", HICOLOR]),
        (shutil.which("update-desktop-database"), [DESKTOP_DIR]),
    ):
        if cmd:
            subprocess.run([cmd, *args], capture_output=True, check=False)
    _emit(progress, "install.progress.uninstalled")


def _argv_wants_gui(argv):
    """Same rules as cli._wants_gui, without building the full parser."""
    if not argv:
        return True
    gui_flags = {"--gui", "--data"}
    cli_flags = {
        "--cli", "--about", "--init", "--show", "--json", "--quiet",
        "--list-data", "--observer", "--instructions", "--delete", "--yes",
        "--notify", "--watch-install", "--watch-uninstall", "--watch-status",
        "--setup", "--setup-uninstall", "--project", "--help", "-h",
    }
    # language-only → GUI
    stripped = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--language", "--lang") and i + 1 < len(argv):
            i += 2
            continue
        if a.startswith(("--language=", "--lang=")):
            i += 1
            continue
        stripped.append(a)
        i += 1
    if not stripped:
        return True
    if any(a == "--gui" or a.startswith("--gui=") for a in stripped):
        return True
    if any(a in cli_flags or a.startswith("--project=") or a.startswith("--delete=")
           for a in stripped):
        return False
    if "--data" in stripped or "--license" in stripped or "--observer" in stripped \
            or "--instructions" in stripped:
        return True
    return False


def prepare_launch(argv=None):
    """Called from run.py: remember pending work for the GUI, or install quietly.

    Returns the pending step list when the GUI should show progress.
    """
    global _gui_pending
    argv = list(sys.argv[1:] if argv is None else argv)
    # Explicit setup / help / about handle themselves — do not side-install.
    skip = {"--setup", "--setup-uninstall", "--help", "-h", "--about"}
    if any(a in skip for a in argv):
        _gui_pending = None
        return []
    pending = pending_steps()
    if not pending:
        _gui_pending = None
        return []
    if _argv_wants_gui(argv):
        _gui_pending = pending
        return pending
    # Headless launch: keep desktop integration current without a window.
    ensure(progress=None)
    _gui_pending = None
    return []


def take_gui_pending():
    """Consume the pending list prepared for the GUI (or re-check)."""
    global _gui_pending
    if _gui_pending is not None:
        pending = _gui_pending
        _gui_pending = None
        return pending
    return pending_steps()
