"""Names and paths: what a conversation carries besides your sentences.

Two things travel inside a Claude Code session that nobody types as a message,
and both are more revealing than the sentences around them.

**The name you gave the tab.** It is not a label on the surface. It is written
into the transcript as a record of its own (``{"type": "custom-title", ...}``),
into a ``custom-title.json`` beside it, into ``~/.claude/sessions/<pid>.json``,
and into the editor extension's log. A list of those names is a list of what
you worked on, one line each, and it does not distinguish the company's
projects from your own.

**The file you had open in the editor.** The extension appends the absolute
path of the active editor tab to the prompt, whether or not the file has
anything to do with the question. ``.../pmtool/.env`` in that line says you
were in a credentials file at that moment, and the file name alone says it.

This module finds both in the local copy and says where each one lives, so the
question stops being a guess. Reads, never writes.

What it can and cannot prove:

* **Certain:** the title is a record *inside* the conversation file rather than
  a setting beside it, and the opened-file path is part of the prompt text.
  Both are on this disk and can be read here.
* **Not decidable from this machine:** whether the server-side copy carries the
  same records, and therefore whether an organisation data export contains
  them. A record that lives inside the conversation is far more likely to
  travel with it than a local setting, which is why the view says "assume it
  travels" rather than "it is only local".
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections import Counter, defaultdict

from .core import CLAUDE_DIR, HOME
from .data import (active_session_ids, as_date, decode_project_path,
                   transcript_files)

# Records the transcript carries for the tab's name. Two of them, written
# together: one names the session, one names the agent shown in the tab.
TITLE_TYPES = (b'"type":"custom-title"', b'"type":"agent-name"')
TITLE_FIELDS = ("customTitle", "agentName")

# The line the extension appends to a prompt. Matched on the raw bytes, like
# every other sweep here, because the path is plain text inside a JSON string.
OPENED = re.compile(rb"The user opened the file ([^<\"\\]{1,400}?) in the IDE")

# A path whose *name alone* says what kind of file it is. A hit is not proof of
# a leak, it is proof that the fact "this person had a credentials file open"
# left the machine inside an ordinary prompt.
# Two rules, not one: a name has to *begin* a path segment, an extension has to
# *end* the path. Folding them together anchors the extensions to a slash and
# quietly misses every server.pem. The nouns are segment-anchored on purpose --
# matching "token" anywhere would flag a TokenParser.php and cost the list the
# credibility that is the only reason to show it.
SENSITIVE = re.compile(
    r"(^|/)("
    r"\.env(\.|$)|\.npmrc$|\.netrc$|\.pgpass$|\.htpasswd$"
    r"|id_rsa|id_ed25519|id_ecdsa"
    r"|credentials?(\.|$)|secrets?(\.|$)|tokens?(\.|$)|passwor[dt]s?(\.|$)"
    r")"
    r"|\.(pem|p12|pfx|key|keystore|kdbx)$",
    re.IGNORECASE)

# Where the editor extension writes its own log. Every rename of a tab is in
# there in the clear. Local, but a single `cat` of it during a session puts the
# whole list into that conversation, which is how it left this machine once.
IDE_LOG_GLOBS = (
    ".config/Code/logs/*/window*/exthost/Anthropic.claude-code/*.log",
    ".config/Code - Insiders/logs/*/window*/exthost/Anthropic.claude-code/*.log",
    ".config/VSCodium/logs/*/window*/exthost/Anthropic.claude-code/*.log",
    ".vscode-server/data/logs/*/exthost/Anthropic.claude-code/*.log",
)
RENAME = re.compile(r'"type":"rename_tab","title":"((?:[^"\\]|\\.)*)"')

SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")


def _title_of(record):
    for field in TITLE_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def is_sensitive(path):
    """Does the file name alone say what kind of file it is?"""
    return bool(SENSITIVE.search(path))


def side_file(path):
    """The ``custom-title.json`` that belongs to one transcript, if it exists."""
    session = os.path.basename(path)
    if not session.endswith(".jsonl"):
        return None
    candidate = os.path.join(os.path.dirname(path), session[:-6], "custom-title.json")
    return candidate if os.path.exists(candidate) else None


def scan_transcripts(progress=None):
    """Titles and opened-file paths, from the conversations themselves.

    Each entry keeps the transcript files it was found in. That list is the
    only thing that makes the row actionable: a name cannot be cut out of a
    conversation, so the unit a person can actually remove is the conversation
    that carries it.
    """
    titles = {}
    opened = {}
    title_files = set()
    opened_files = set()
    entries = list(transcript_files())

    for index, (bucket, path) in enumerate(entries):
        if progress:
            progress(index, len(entries))
        label = decode_project_path(bucket)
        try:
            stamp = as_date(os.stat(path).st_mtime)
            handle = open(path, "rb")
        except OSError:
            continue
        with handle:
            for line in handle:
                if any(marker in line for marker in TITLE_TYPES):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    name = _title_of(record)
                    if name:
                        title_files.add(path)
                        entry = titles.setdefault(name, {
                            "title": name, "projects": set(), "sessions": set(),
                            "files": set(), "extra": set(),
                            "first": stamp, "last": stamp})
                        entry["projects"].add(label)
                        entry["sessions"].add(record.get("sessionId") or path)
                        entry["files"].add(path)
                        beside = side_file(path)
                        if beside:
                            entry["extra"].add(beside)
                        entry["first"] = min(entry["first"], stamp)
                        entry["last"] = max(entry["last"], stamp)
                if b"ide_opened_file" not in line:
                    continue
                for found in OPENED.finditer(line):
                    opened_files.add(path)
                    name = found.group(1).decode("utf-8", "replace").strip()
                    entry = opened.setdefault(name, {
                        "path": name, "count": 0, "projects": set(),
                        "files": set(), "sensitive": is_sensitive(name)})
                    entry["count"] += 1
                    entry["projects"].add(label)
                    entry["files"].add(path)

    if progress:
        progress(len(entries), len(entries))
    return titles, opened, len(entries), len(title_files), len(opened_files)


def scan_title_files():
    """``custom-title.json`` written beside each session's transcript."""
    found = []
    pattern = os.path.join(CLAUDE_DIR, "projects", "*", "*", "custom-title.json")
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as handle:
                title = json.load(handle).get("customTitle")
        except (OSError, ValueError):
            title = None
        found.append({"path": path, "title": title if isinstance(title, str) else None})
    return found


def scan_sessions():
    """``~/.claude/sessions/<pid>.json`` -- the name, and the ones before it.

    ``formerNames`` is the point of reading these: renaming a tab does not
    replace the old name, it files it. A name you thought better of is still
    there.
    """
    found = []
    for path in sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        former = [entry.get("name") for entry in record.get("formerNames") or []
                  if isinstance(entry, dict) and entry.get("name")]
        found.append({
            "path": path,
            "name": record.get("name"),
            "source": record.get("nameSource"),
            "cwd": record.get("cwd"),
            "former": former,
        })
    return found


def scan_ide_logs():
    """Tab renames in the editor extension's own log."""
    files, renames, titles = [], 0, Counter()
    for pattern in IDE_LOG_GLOBS:
        for path in sorted(glob.glob(os.path.join(HOME, pattern))):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    body = handle.read()
            except OSError:
                continue
            found = RENAME.findall(body)
            if not found:
                continue
            files.append(path)
            renames += len(found)
            titles.update(name.strip() for name in found if name.strip())
    return {"files": files, "renames": renames, "titles": titles.most_common()}


def build_report(progress=None):
    """Every name and path the local copy carries, and where each one lives."""
    titles, opened, transcripts, title_files, opened_files = scan_transcripts(progress)

    running = active_session_ids()

    def in_use(paths):
        """Is one of these transcripts being written right now?"""
        return any(os.path.basename(path)[:-6] in running for path in paths)

    title_rows = sorted(
        ({"title": entry["title"],
          "projects": sorted(entry["projects"]),
          "sessions": len(entry["sessions"]),
          "files": sorted(entry["files"]),
          "extra": sorted(entry["extra"]),
          "active": in_use(entry["files"]),
          "first": entry["first"], "last": entry["last"]}
         for entry in titles.values()),
        key=lambda entry: entry["title"].lower())

    opened_rows = sorted(
        ({"path": entry["path"], "count": entry["count"],
          "projects": len(entry["projects"]), "sensitive": entry["sensitive"],
          "files": sorted(entry["files"]),
          "active": in_use(entry["files"])}
         for entry in opened.values()),
        key=lambda entry: (not entry["sensitive"], -entry["count"]))

    sessions = scan_sessions()
    former = sorted({name for entry in sessions for name in entry["former"]})
    typed = [entry for entry in sessions if entry["source"] == "user"]

    return {
        "titles": title_rows,
        "title_transcripts": title_files,
        "title_files": scan_title_files(),
        "sessions": sessions,
        "typed_names": [entry["name"] for entry in typed if entry["name"]],
        "former_names": former,
        "opened": opened_rows,
        "opened_total": sum(entry["count"] for entry in opened_rows),
        "opened_transcripts": opened_files,
        "sensitive": [entry for entry in opened_rows if entry["sensitive"]],
        "ide_logs": scan_ide_logs(),
        "transcripts": transcripts,
    }


def places(report):
    """Where a name lives, with what is in each place.

    Four rows, because a person who wants this gone has to visit four places
    and the interface should not make them guess which.
    """
    logs = report["ide_logs"]
    return [
        {"place": "transcript", "count": report["title_transcripts"],
         "path": os.path.join(CLAUDE_DIR, "projects"), "travels": True},
        {"place": "title_file", "count": len(report["title_files"]),
         "path": os.path.join(CLAUDE_DIR, "projects", "…", "custom-title.json"),
         "travels": False},
        {"place": "session", "count": len(report["sessions"]),
         "path": SESSIONS_DIR, "travels": False},
        {"place": "ide_log", "count": logs["renames"],
         "path": os.path.join(HOME, ".config", "Code", "logs", "…"),
         "travels": False},
    ]


def removable(entry):
    """Everything on disk that carries this one row.

    The transcripts themselves plus the ``custom-title.json`` beside them. Only
    whole files: a name cannot be cut out of a conversation without rewriting
    it, and a rewritten transcript breaks ``--resume`` while changing nothing
    at all on the server.
    """
    return list(entry.get("files") or []) + list(entry.get("extra") or [])


def verdict_key(report):
    """One-line conclusion, as a translation key."""
    if report["sensitive"]:
        return "names.verdict.sensitive"
    if report["titles"] or report["opened"]:
        return "names.verdict.found"
    return "names.verdict.clean"
