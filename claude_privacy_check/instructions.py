"""Instruction files: what is being fed into Claude Code sessions.

Every session starts with more than the prompt. Memory files, project
instructions, agent and skill definitions are loaded automatically, and an
organisation can push its own via the ``claudeMd`` setting. Those are
instructions someone else wrote that shape what the assistant does on this
machine — worth being able to look at.

Listing is read-only; the content of a plain instruction file can be edited
from the interface, because seeing what steers a session and being unable to
change it is only half of it. Ownership is reported per entry, because an
instruction file owned by somebody else is a different thing from one you
wrote -- and it decides whether editing is offered at all.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import tempfile
from datetime import datetime

from .core import CLAUDE_DIR, HOME, PROJECTS_DIR, read_settings_file

PREVIEW_BYTES = 4000        # enough to judge a file, small enough to render
MAX_EDIT_BYTES = 1_000_000  # past this a text widget is the wrong tool


def _encoded_project(path):
    """Working path -> the directory name Claude Code derives from it."""
    return os.path.abspath(path).replace(os.sep, "-")


def _describe(path, scope, kind, origin="user"):
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            body = fh.read(PREVIEW_BYTES + 1)
    except OSError as exc:
        body = f"<{exc}>"
    truncated = len(body) > PREVIEW_BYTES
    # Why editing may not be on offer -- a translation key suffix, so the
    # interface can say which of the reasons it is instead of a grey button.
    if not os.path.isfile(path):
        locked = "missing"
    elif st.st_size > MAX_EDIT_BYTES:
        locked = "too_large"
    elif not os.access(path, os.W_OK):
        locked = "permission"
    else:
        locked = None
    return {
        "path": path,
        "name": os.path.basename(path),
        "scope": scope,          # translation key suffix: user / project / org
        "kind": kind,            # memory / instructions / agent / skill
        "bytes": st.st_size,
        "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "mtime": st.st_mtime,    # to notice a change made elsewhere while open
        "owner": owner,
        "foreign": owner != pwd.getpwuid(os.getuid()).pw_name,
        "origin": origin,        # who controls it: user or org
        "preview": body[:PREVIEW_BYTES],
        "truncated": truncated,
        "editable": locked is None,
        "locked": locked,
    }


def _markdown_in(directory, scope, kind, origin="user"):
    found = []
    if not os.path.isdir(directory):
        return found
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path) and name.endswith(".md"):
            entry = _describe(path, scope, kind, origin)
            if entry:
                found.append(entry)
    return found


def _skills_in(directory, scope):
    found = []
    if not os.path.isdir(directory):
        return found
    for name in sorted(os.listdir(directory)):
        skill = os.path.join(directory, name, "SKILL.md")
        if os.path.isfile(skill):
            entry = _describe(skill, scope, "skill")
            if entry:
                entry["name"] = f"{name}/SKILL.md"
                found.append(entry)
    return found


def read_text(path):
    """The whole file, strictly decoded.

    The preview in an entry is capped and decoded with replacements -- fine to
    look at, ruinous to write back. An editor needs the real thing, so a file
    that is not valid UTF-8 raises here and stays read-only.
    """
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def save_text(path, text):
    """Replace the content of an existing instruction file.

    Written beside the target and moved into place, so an interrupted write
    cannot leave a half-written instruction file behind. Symlinks are resolved
    first -- dotfiles are often symlinked into a checkout, and replacing the
    link with a regular file would quietly detach it. Where the directory is
    not writable the atomic route is impossible and the file is rewritten in
    place instead.
    """
    path = os.path.realpath(path)
    if not os.path.isfile(path):
        raise OSError(f"not a file: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    directory = os.path.dirname(path) or "."

    if os.access(directory, os.W_OK):
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".cpc-",
                                             suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return os.stat(path).st_mtime


def restat(entry):
    """Refresh the fields that a write changed, in place."""
    fresh = _describe(entry["path"], entry["scope"], entry["kind"], entry["origin"])
    if fresh:
        fresh["name"] = entry["name"]      # a skill carries its directory name
        entry.update(fresh)
    return entry


def collect(project_dirs=()):
    """Every instruction source that applies, grouped by where it comes from."""
    me = pwd.getpwuid(os.getuid()).pw_name
    entries = []

    # 1. Organisation-pushed instructions. Highest precedence and not written
    #    by the person sitting here -- listed first for that reason.
    for source in ("~/.claude/remote-settings.json",
                   "/etc/claude-code/managed-settings.json",
                   "~/.claude/settings.json", "~/.claude/settings.local.json"):
        surface = read_settings_file(source, skip_if_missing=True)
        content = (surface or {}).get("content") or {}
        pushed = content.get("claudeMd")
        if pushed:
            entries.append({
                "path": os.path.expanduser(source), "name": "claudeMd",
                "scope": "org", "kind": "instructions", "bytes": len(str(pushed)),
                "modified": "—", "mtime": 0, "owner": "—", "foreign": True,
                "origin": "org",
                "preview": pushed if isinstance(pushed, str)
                else json.dumps(pushed, ensure_ascii=False, indent=2),
                "truncated": False,
                # A value inside a settings file, not a file of its own -- and
                # organisation policy besides. Not something to edit here.
                "editable": False, "locked": "org",
            })

    # 2. User-wide instructions and definitions.
    user_md = os.path.join(CLAUDE_DIR, "CLAUDE.md")
    entry = _describe(user_md, "user", "instructions")
    if entry:
        entries.append(entry)
    entries += _markdown_in(os.path.join(CLAUDE_DIR, "agents"), "user", "agent")
    entries += _skills_in(os.path.join(CLAUDE_DIR, "skills"), "user")

    # 3. Per project: instructions in the tree, plus the auto-memory that gets
    #    loaded at session start.
    for project in sorted(set(project_dirs)):
        for name in ("CLAUDE.md", "CLAUDE.local.md",
                     os.path.join(".claude", "CLAUDE.md")):
            entry = _describe(os.path.join(project, name), "project", "instructions")
            if entry:
                entries.append(entry)
        entries += _markdown_in(os.path.join(project, ".claude", "agents"),
                                "project", "agent")
        entries += _skills_in(os.path.join(project, ".claude", "skills"), "project")

        memory = os.path.join(PROJECTS_DIR, _encoded_project(project), "memory")
        entries += _markdown_in(memory, "project", "memory")

        # Instructions also apply from directories above the project.
        parent = os.path.dirname(os.path.abspath(project))
        while parent.startswith(HOME) and parent != HOME:
            entry = _describe(os.path.join(parent, "CLAUDE.md"), "parent",
                              "instructions")
            if entry:
                entries.append(entry)
            parent = os.path.dirname(parent)

    seen, unique = set(), []
    for entry in entries:
        if entry["path"] in seen:
            continue
        seen.add(entry["path"])
        unique.append(entry)

    return {
        "entries": unique,
        "total_bytes": sum(e["bytes"] for e in unique),
        "org_controlled": sum(1 for e in unique if e["origin"] == "org"),
        "foreign_owner": sum(1 for e in unique if e["foreign"] and e["origin"] != "org"),
        "user": me,
    }
