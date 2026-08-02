"""Local history data: inventory and deletion.

Everything Claude Code keeps on disk about past sessions, listed so it can be
removed selectively. Deletion is guarded: only real paths below ~/.claude,
never the directory itself, never credentials or configuration.
"""

from __future__ import annotations

import glob
import os
import shutil
from datetime import datetime

from .core import CLAUDE_DIR, PROJECTS_DIR

# Stores below ~/.claude that hold history and may be deleted.
# (directory name, label key, description key)
DATA_STORES = [
    ("file-history", "store.file_history"),
    ("shell-snapshots", "store.shell_snapshots"),
    ("session-env", "store.session_env"),
    ("sessions", "store.sessions"),
    ("plans", "store.plans"),
    ("backups", "store.backups"),
]

# Files that are never deleted -- authentication and configuration.
PROTECTED_NAMES = {".credentials.json", "settings.json", "settings.local.json",
                   "policy-limits.json", "remote-settings.json", ".claude.json"}


def human_bytes(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def dir_stats(path):
    """Size, file count and time span of a directory."""
    total, count, oldest, newest = 0, 0, None, None
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            total += st.st_size
            count += 1
            if oldest is None or st.st_mtime < oldest:
                oldest = st.st_mtime
            if newest is None or st.st_mtime > newest:
                newest = st.st_mtime
    return {"bytes": total, "files": count, "oldest": oldest, "newest": newest}


def active_session_ids():
    """Session IDs of running Claude Code processes -- do not delete those."""
    ids = set()
    for pid_dir in glob.glob("/proc/[0-9]*"):
        try:
            with open(os.path.join(pid_dir, "environ"), "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except (OSError, PermissionError):
            continue
        for item in raw.split("\0"):
            if item.startswith("CLAUDE_CODE_SESSION_ID="):
                ids.add(item.partition("=")[2])
    return ids


def as_date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"


def decode_project_path(name):
    """Directory name -> working path.

    Claude Code encodes the path as '-home-user-Documents-project'. Hyphens
    inside a folder name are indistinguishable from separators, so this
    resolves against the real filesystem: 'sensor-control-v2' stays one
    folder instead of becoming three levels.
    """
    parts = name.lstrip("-").split("-")
    path, i = os.sep, 0
    while i < len(parts):
        candidate, j = parts[i], i + 1
        while not os.path.exists(os.path.join(path, candidate)) and j < len(parts):
            candidate += "-" + parts[j]
            j += 1
        nxt = os.path.join(path, candidate)
        if not os.path.exists(nxt):
            return os.path.join(path, "-".join(parts[i:]))   # gone from disk
        path, i = nxt, j
    return path


def list_local_data():
    """Inventory of local history, grouped by project and by store."""
    active = active_session_ids()
    projects = []
    if os.path.isdir(PROJECTS_DIR):
        for entry in sorted(os.scandir(PROJECTS_DIR), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            stats = dir_stats(entry.path)
            sessions = []
            for f in sorted(os.scandir(entry.path), key=lambda e: e.name):
                if not (f.is_file() and f.name.endswith(".jsonl")):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                sid = f.name[:-6]
                sessions.append({
                    "id": sid, "path": f.path, "bytes": st.st_size,
                    "date": as_date(st.st_mtime), "active": sid in active,
                })
            sessions.sort(key=lambda s: s["date"], reverse=True)
            projects.append({
                "name": entry.name, "label": decode_project_path(entry.name),
                "path": entry.path, "sessions": sessions, "bytes": stats["bytes"],
                "files": stats["files"],
                "oldest": as_date(stats["oldest"]), "newest": as_date(stats["newest"]),
                "has_active": any(s["active"] for s in sessions),
            })
    projects.sort(key=lambda p: -p["bytes"])

    stores = []
    for key, label_key in DATA_STORES:
        path = os.path.join(CLAUDE_DIR, key)
        if not os.path.isdir(path):
            continue
        stats = dir_stats(path)
        if not stats["files"]:
            continue
        stores.append({"key": key, "label_key": label_key, "path": path,
                       "bytes": stats["bytes"], "files": stats["files"],
                       "oldest": as_date(stats["oldest"]),
                       "newest": as_date(stats["newest"])})
    stores.sort(key=lambda s: -s["bytes"])

    return {
        "projects": projects,
        "stores": stores,
        "active_sessions": sorted(active),
        "total_bytes": sum(p["bytes"] for p in projects) + sum(s["bytes"] for s in stores),
    }


class NotDeletable(ValueError):
    """Raised with a translation key and params, so callers can localise it."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params


def check_deletable(path):
    """Raise NotDeletable unless the path may be removed.

    Guards against typos and symlink escapes: only real paths below ~/.claude,
    never the directory itself, never credentials or configuration.
    """
    real = os.path.realpath(path)
    root = os.path.realpath(CLAUDE_DIR)
    if real == root:
        raise NotDeletable("delete.err.root")
    if not real.startswith(root + os.sep):
        raise NotDeletable("delete.err.outside", path=real)
    if os.path.basename(real) in PROTECTED_NAMES:
        raise NotDeletable("delete.err.protected", name=os.path.basename(real))
    if not os.path.exists(real):
        raise NotDeletable("delete.err.missing", path=real)
    return real


def delete_paths(paths):
    """Delete the given paths. Returns (deleted count, [(path, error)])."""
    deleted, errors = 0, []
    for path in paths:
        try:
            real = check_deletable(path)
        except NotDeletable as exc:
            errors.append((path, exc))
            continue
        try:
            if os.path.isdir(real) and not os.path.islink(real):
                shutil.rmtree(real)
            else:
                os.remove(real)
            deleted += 1
        except OSError as exc:
            errors.append((path, exc))
    return deleted, errors
