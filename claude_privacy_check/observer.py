"""Observer view: what a triage over this machine's data would surface.

Volume feels like protection — 200 sessions, hundreds of megabytes, surely
nobody reads that. Nobody does. They search it. This module runs the same
mechanical triage an observer would and shows the result, so the question
stops being a matter of intuition.

Deliberately an approximation, and the interface says so: a server-side export
holds a different slice of the data. What this proves is what is *findable* on
this disk, in seconds, without reading a single sentence by hand.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime

from .core import CLAUDE_DIR, PROJECTS_DIR, collect_account_and_mcp
from .data import as_date, decode_project_path

# Terms an observer would sweep for. Search patterns, not interface text, so
# they live here rather than in the locale files -- but German and English are
# both covered because prompts are written in either.
#
# The category name is a translation key; the patterns are matched
# case-insensitively against the raw transcript bytes.
# Two confidence tiers, because they mean very different things.
#
# "high" are literal secret shapes -- a match is a finding on its own.
# "low" are topic words. In a developer's transcripts these fire on ordinary
# work too ("password" in a function signature, a "kreditkarte" column in a
# schema), so they are reported as mentions worth a look, not as evidence.
# Saying so is the honest framing: an observer's sweep produces noise as well,
# and pretending otherwise would overstate the result in both directions.
CATEGORIES = [
    ("credentials", "observer.cat.credentials", "high", [
        r"sk-ant-[A-Za-z0-9_-]{16,}", r"ghp_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}", r"AKIA[0-9A-Z]{16}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bDE\d{2}(?:[ ]?\d{4}){4}[ ]?\d{2}\b",          # IBAN
    ]),
    ("secretwords", "observer.cat.secretwords", "low", [
        r"\bpasswor[dt]\s*[:=]\s*\S", r"\bzugangsdaten\b",
        r"\bapi[_ -]?key\s*[:=]\s*\S", r"\bsecret\s*[:=]\s*\S",
    ]),
    ("finance", "observer.cat.finance", "low", [
        r"\bgehalt\b", r"\bgehalts\w*", r"\blohnabrechnung\b",
        r"\bsteuererkl\w*", r"\bkontoauszug\b", r"\bschulden\b",
        r"\bsalary\b", r"\bpayslip\b",
    ]),
    ("jobsearch", "observer.cat.jobsearch", "low", [
        r"\bbewerbungsschreiben\b", r"\blebenslauf\b",
        r"\beigenk[üu]ndigung\b", r"\bvorstellungsgespr\w*",
        r"\barbeitszeugnis\b", r"\bjob application\b", r"\bcover letter\b",
    ]),
    ("health", "observer.cat.health", "low", [
        r"\bkrankschreib\w*", r"\barzttermin\b", r"\bmedikament\w*",
        r"\bpsychotherap\w*", r"\bdiagnosis\b", r"\bsick leave\b",
    ]),
    ("legal", "observer.cat.legal", "low", [
        r"\banwalt\b", r"\banw[äa]ltin\b", r"\bscheidung\b",
        r"\babmahnung\b", r"\blawsuit\b", r"\bdivorce\b",
    ]),
    ("personal", "observer.cat.personal", "low", [
        r"\bmeine frau\b", r"\bmein mann\b", r"\bmeine tochter\b",
        r"\bmein sohn\b", r"\bmeine freundin\b",
        r"\bmy wife\b", r"\bmy husband\b",
    ]),
]

MAX_SAMPLES = 4          # per category, enough to make the point
CONTEXT = 60             # characters of context around a hit
# Base64 blobs and minified payloads are matches without meaning to a reader.
BLOB = re.compile(rb"[A-Za-z0-9+/=]{40,}")
CHUNK = 4 << 20       # 4 MiB per read
OVERLAP = 512         # carried over so a match is not split at a boundary

BUSINESS_START, BUSINESS_END = 7, 19


def _combined():
    """Two regexes with a named group per category.

    Performance, measured on ~325 MB of transcripts: six separate
    case-insensitive passes took 60 s, one combined case-insensitive pass 41 s.
    ``re.IGNORECASE`` on bytes defeats the literal-prefix optimisation, so the
    topical patterns are spelled in lowercase and matched against a buffer that
    ``bytes.lower()`` has folded at C speed. The secret shapes keep their case,
    which is part of what makes them distinctive.

    The low-confidence patterns must therefore contain no uppercase literal --
    lowercasing them here would also turn ``\\S`` into ``\\s`` and break them.
    A test enforces that.

    Returns (case_sensitive, matched_against_lowercase).
    """
    cased, folded = [], []
    for slug, _key, confidence, patterns in CATEGORIES:
        group = f"(?P<{slug}>" + "|".join(patterns) + ")"
        (cased if confidence == "high" else folded).append(group)
    return (re.compile("|".join(cased).encode()),
            re.compile("|".join(folded).encode()))


def _excerpt(line_bytes, match):
    """A short, readable window around the hit, or None if it is a blob.

    Matches inside base64 payloads are real matches but tell a reader nothing,
    so they are counted and not shown.
    """
    start = max(0, match.start() - CONTEXT)
    end = min(len(line_bytes), match.end() + CONTEXT)
    window = line_bytes[start:end]
    if BLOB.search(window):
        return None
    snippet = re.sub(r"\s+", " ", window.decode("utf-8", "replace")).strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(line_bytes) else "")


def _transcripts():
    """(project directory, transcript path) for every transcript.

    Subagent transcripts live in a nested ``subagents/`` folder; they belong to
    the project above them, not to a project called "subagents".
    """
    if not os.path.isdir(PROJECTS_DIR):
        return
    for bucket in sorted(os.listdir(PROJECTS_DIR)):
        root = os.path.join(PROJECTS_DIR, bucket)
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.endswith(".jsonl"):
                    yield bucket, os.path.join(base, name)


def build_report(progress=None):
    """Everything a mechanical triage would extract. Reads, never writes."""
    account, _mcp = collect_account_and_mcp()
    cased_re, folded_re = _combined()

    projects = {}
    hours = Counter()
    weekdays = Counter()
    days = set()
    hits = {slug: {"count": 0, "sessions": set(), "samples": []}
            for slug, _key, _conf, _patterns in CATEGORIES}
    total_bytes = 0
    entries = list(_transcripts())

    for index, (bucket, path) in enumerate(entries):
        if progress:
            progress(index, len(entries))
        try:
            st = os.stat(path)
        except OSError:
            continue
        stamp = datetime.fromtimestamp(st.st_mtime)
        hours[stamp.hour] += 1
        weekdays[stamp.weekday()] += 1
        days.add(stamp.date())
        total_bytes += st.st_size

        # The directory name is the working path -- the single strongest signal,
        # available without opening a file.
        entry = projects.setdefault(bucket, {
            "label": decode_project_path(bucket), "sessions": 0, "bytes": 0,
            "oldest": st.st_mtime, "newest": st.st_mtime})
        entry["sessions"] += 1
        entry["bytes"] += st.st_size
        entry["oldest"] = min(entry["oldest"], st.st_mtime)
        entry["newest"] = max(entry["newest"], st.st_mtime)

        session = os.path.basename(path)[:-6]
        try:
            with open(path, "rb") as fh:
                # Whole chunks rather than lines: a transcript has hundreds of
                # thousands of lines, and the per-line Python loop cost dwarfs
                # the matching itself. The overlap keeps a match from being
                # split across a chunk boundary.
                carry = b""
                while True:
                    block = fh.read(CHUNK)
                    if not block:
                        break
                    buffer = carry + block
                    lowered = buffer.lower()
                    matches = [(m, buffer) for m in cased_re.finditer(buffer)]
                    matches += [(m, lowered) for m in folded_re.finditer(lowered)]
                    for match, source in matches:
                        slug = match.lastgroup
                        record = hits[slug]
                        record["count"] += 1
                        record["sessions"].add(session)
                        if len(record["samples"]) < MAX_SAMPLES:
                            excerpt = _excerpt(source, match)
                            if excerpt is None:      # base64 blob, unreadable
                                continue
                            record["samples"].append({
                                "project": entry["label"], "session": session,
                                "date": as_date(st.st_mtime), "excerpt": excerpt,
                            })
                    carry = buffer[-OVERLAP:]
        except OSError:
            continue

    if progress:
        progress(len(entries), len(entries))

    for entry in projects.values():
        entry["oldest"] = as_date(entry["oldest"])
        entry["newest"] = as_date(entry["newest"])

    off_hours = sum(n for h, n in hours.items()
                    if h < BUSINESS_START or h >= BUSINESS_END)
    weekend = weekdays[5] + weekdays[6]

    return {
        "account": account,
        "projects": sorted(projects.values(), key=lambda p: -p["bytes"]),
        "sessions": len(entries),
        "bytes": total_bytes,
        "active_days": len(days),
        "hours": {h: hours.get(h, 0) for h in range(24)},
        "weekdays": {d: weekdays.get(d, 0) for d in range(7)},
        "off_hours": off_hours,
        "weekend": weekend,
        "categories": [
            {"slug": slug, "key": key, "confidence": confidence,
             "count": hits[slug]["count"],
             "sessions": len(hits[slug]["sessions"]),
             "samples": hits[slug]["samples"]}
            for slug, key, confidence, _patterns in CATEGORIES
        ],
    }


def verdict_key(report):
    """One-line conclusion, as a translation key."""
    for category in report["categories"]:
        if category["confidence"] == "high" and category["count"]:
            return "observer.verdict.credentials"
    if any(c["count"] for c in report["categories"]):
        return "observer.verdict.mentions"
    return "observer.verdict.clean"
