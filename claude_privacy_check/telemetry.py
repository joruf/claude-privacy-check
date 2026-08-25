"""Outbound telemetry queue: the payload itself, not the configuration.

Every other check in this tool reads *configuration* — what could be captured
if someone turned it on. This one reads what is actually queued to leave the
machine. Claude Code keeps its own first-party events in ``~/.claude/telemetry``
as JSON, one event per line, and they sit there in the clear.

Two things follow, and the interface says both:

- The files are named ``1p_failed_events.*``. This is the **retry queue** — the
  events whose delivery failed. Anything sent successfully is not kept. So it is
  a sample of what goes out, not a complete send log, and an empty directory
  proves nothing.
- What is in there is Anthropic's own product telemetry, not the organisation's.
  A company that wants prompt content configures OpenTelemetry, a hook or a
  gateway, and those surfaces are covered elsewhere in this tool.

What makes the queue worth reading anyway: it is the one place where the claim
"no content leaves this machine" can be checked against evidence rather than
against a settings file. So the scan looks for the strings that would prove
otherwise — this machine's home path, its project names, the account address,
and literal secret shapes — and it decodes the base64 fields first, because two
of them carry a nested JSON object that a plain grep would walk straight past.

Stdlib only, and deliberately free of imports from the rest of the package, so
``core`` can depend on this module rather than the other way round.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections import Counter

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
TELEMETRY_DIR = os.path.join(CLAUDE_DIR, "telemetry")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

# The queue is normally well under a megabyte. The cap is a guard against a
# pathological directory, not a design constraint, and a run that hits it says so.
MAX_BYTES = 64 << 20
MAX_SAMPLES = 4          # per category, enough to make the point
CONTEXT = 50             # characters of context around a hit
VALUE_CLIP = 120         # sample values are for recognition, not for reading

# Fields that describe the machine rather than the event. Not a finding —
# disclosure. This is the answer to "can anyone tell which computer I am on".
DEVICE_FIELDS = (
    "event_data.env.platform",
    "event_data.env.arch",
    "event_data.env.linux_distro_id",
    "event_data.env.linux_distro_version",
    "event_data.env.linux_kernel",
    "event_data.env.node_version",
    "event_data.env.shell",
    "event_data.env.terminal",
    "event_data.env.package_managers",
    "event_data.env.runtimes",
    "event_data.env.vcs",
    "event_data.env.deployment_environment",
    "event_data.env.version",
    "event_data.entrypoint",
    "event_data.client_type",
)

# Fields that name the account or the organisation behind it. device_id sits here
# rather than with the machine fields on purpose: despite the name, what Claude
# Code puts in it is the account's stable user id from ~/.claude.json, not the
# machineID recorded next to it. ``device_id_kind`` in the report proves which of
# the two it is on this machine instead of trusting the label.
IDENTITY_FIELDS = (
    "event_data.device_id",
    "event_data.auth.account_uuid",
    "event_data.auth.organization_uuid",
    "event_data.user_type",
    "event_data.session_id",
    "event_data.model",
)

# Categories the scan reports. All high confidence: unlike a sweep over
# transcripts, none of these can turn up in an outbound event by accident.
# A hit is a hit.
CAT_PATH = ("path", "telemetry.cat.path")
CAT_PROJECT = ("project", "telemetry.cat.project")
CAT_ACCOUNT = ("account", "telemetry.cat.account")
CAT_SECRET = ("secret", "telemetry.cat.secret")
CATEGORIES = (CAT_PATH, CAT_PROJECT, CAT_ACCOUNT, CAT_SECRET)

# Long enough to rule out the short base64-ish tokens that appear in ordinary
# values (a two-letter language code, a hex digest), and unpadded input is
# rejected outright rather than guessed at.
B64 = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")

# Where a user's own skills, subagents and slash commands live. Their names are
# free text somebody here typed, and they travel with the events that record a
# skill being loaded.
NAME_DIRS = (("skills", "skill"), ("agents", "agent"), ("commands", "command"))

# Directory names that identify nobody and occur as ordinary payload values.
# A project folder called "test" is not a thing worth raising CRITICAL over when
# some field happens to say "test", and a finding that fires on nothing teaches
# people to ignore the next one.
GENERIC_LEAVES = frozenset({
    "app", "apps", "backup", "build", "code", "data", "demo", "dev", "dist",
    "doc", "docs", "example", "examples", "home", "lib", "main", "new", "old",
    "prod", "project", "projects", "sandbox", "scratch", "scratchpad", "src",
    "staging", "temp", "test", "tests", "tmp", "user", "users", "work",
})


def _secret_shapes():
    """Literal credential patterns, borrowed from the transcript sweep.

    Imported here rather than at module scope on purpose: ``observer`` reaches
    back into ``core``, and ``core`` imports this module. A local import keeps
    this file a leaf and the cycle from ever forming, while the patterns stay
    defined in exactly one place.
    """
    try:
        from .observer import CATEGORIES as SWEEP
    except ImportError:                                   # pragma: no cover
        return []
    return [re.compile(pattern)
            for _slug, _key, confidence, patterns in SWEEP
            if confidence == "high"
            for pattern in patterns]


def local_names():
    """Names chosen on this machine: skills, subagents, slash commands.

    Mapped lowercase name -> (kind, path). Matched against whole field values
    rather than as substrings, so a two-letter command name is as safe to look
    for as a long one and nothing matches by accident.
    """
    found = {}
    for sub, kind in NAME_DIRS:
        directory = os.path.join(CLAUDE_DIR, sub)
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            continue
        for entry in entries:
            name = entry[:-3] if entry.endswith(".md") else entry
            if not name or name.startswith("."):
                continue
            found.setdefault(name.lower(), (kind, os.path.join(directory, entry)))
    return found


def stable_ids():
    """userID and machineID as ~/.claude.json records them.

    Read here rather than taken from ``core`` so this module keeps depending on
    nothing. Only these two scalars are touched; the file holds a great deal more.
    """
    try:
        with open(os.path.join(HOME, ".claude.json"), encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {key: config[key] for key in ("userID", "machineID")
            if isinstance(config.get(key), str)}


def local_identifiers(account=None):
    """Strings that would identify this machine or its work if they left it.

    ``account`` is the parsed ``~/.claude.json`` account block, passed in by the
    caller so this module needs nothing from ``core``.

    Returns (category slug, literal) pairs. Short or generic values are dropped:
    a two-character project name would match half the payload and turn the whole
    scan into noise.
    """
    found = []
    home = HOME.rstrip("/")
    if len(home) > 4:                     # not "/" or "/root"
        found.append((CAT_PATH[0], home))
    user = os.path.basename(home)
    if len(user) >= 4:
        found.append((CAT_PATH[0], user))

    # Project directory names are the working paths with the separators
    # flattened -- the single most telling thing the local history gives away.
    #
    # Only the ones below the home directory, though. Sessions run in /tmp leave
    # directories like "-tmp-staffel-speed-demo" behind, whose last segment is a
    # generic word ("demo", "test", "speed") that says nothing about anyone and
    # matches ordinary payload text. Filtering by where the session actually ran
    # keeps the list to names that identify real work.
    prefix = home.replace("/", "-")
    try:
        entries = sorted(os.listdir(PROJECTS_DIR))
    except OSError:
        entries = []
    for name in entries:
        if not name.startswith(prefix):
            continue
        leaf = name.rstrip("-").rsplit("-", 1)[-1]
        if (len(leaf) >= 4 and leaf.lower() != user.lower()
                and leaf.lower() not in GENERIC_LEAVES):
            found.append((CAT_PROJECT[0], leaf))

    email = (account or {}).get("emailAddress")
    if isinstance(email, str) and "@" in email:
        found.append((CAT_ACCOUNT[0], email))
        domain = email.split("@", 1)[1]
        if len(domain) >= 4:
            found.append((CAT_ACCOUNT[0], domain))

    # Longest first, so the more specific literal wins when two overlap.
    seen, unique = set(), []
    for slug, literal in sorted(found, key=lambda p: -len(p[1])):
        if literal.lower() not in seen:
            seen.add(literal.lower())
            unique.append((slug, literal))
    return unique


def decode_b64(value):
    """A base64 string decoded to text, or None if it is not one.

    Two payload fields carry a nested JSON object this way. They are the obvious
    place for something to hide, precisely because nothing that greps the file
    as text will ever see through them.
    """
    if not isinstance(value, str) or not B64.fullmatch(value):
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Printable text only -- a decoded blob of control bytes is a coincidence,
    # not a hidden message, and showing it would be noise.
    return text if text.isprintable() else None


def _walk(obj, prefix=""):
    """Flatten one event to (dotted path, value) pairs, leaves only."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, f"{prefix}[]")
    else:
        yield prefix, obj


def _excerpt(text, start, end):
    """A short readable window around a hit."""
    left = max(0, start - CONTEXT)
    right = min(len(text), end + CONTEXT)
    window = re.sub(r"\s+", " ", text[left:right]).strip()
    return ("…" if left > 0 else "") + window + ("…" if right < len(text) else "")


def _files():
    """(path, size) for every queue file, newest last. Missing dir -> empty."""
    try:
        names = sorted(os.listdir(TELEMETRY_DIR))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(TELEMETRY_DIR, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        out.append((path, size))
    return out


def compile_identifiers(identifiers):
    """(slug, literal, regex) for each identifier, compiled once per run.

    A bare name is matched on its edges rather than as a raw substring. Without
    that, a project called "test" reports a hit on the word "latest" and on the
    name of a command called "testing" -- noise that would make a CRITICAL
    finding fire on nothing. Paths and addresses keep plain containment: they
    carry their own delimiters and appear inside longer strings legitimately.
    """
    out = []
    for slug, literal in identifiers:
        escaped = re.escape(literal)
        if any(ch in literal for ch in "/@\\"):
            pattern = escaped
        else:
            pattern = r"(?<![A-Za-z0-9])" + escaped + r"(?![A-Za-z0-9])"
        out.append((slug, literal, re.compile(pattern, re.IGNORECASE)))
    return out


def _scan_text(text, matchers, secrets, hits, keep_samples):
    """Record every identifier and secret shape found in one string."""
    for slug, literal, pattern in matchers:
        match = pattern.search(text)
        if match is None:
            continue
        record = hits[slug]
        record["count"] += 1
        record["literals"].add(literal)
        if keep_samples and len(record["samples"]) < MAX_SAMPLES:
            record["samples"].append(_excerpt(text, match.start(), match.end()))
    for pattern in secrets:
        match = pattern.search(text)
        if not match:
            continue
        record = hits[CAT_SECRET[0]]
        record["count"] += 1
        if keep_samples and len(record["samples"]) < MAX_SAMPLES:
            # Never the match itself: writing a live credential into a report
            # that gets printed, exported as JSON or pasted into a ticket would
            # spread the very thing being reported. The shape is the finding.
            record["samples"].append(pattern.pattern)


def read_queue(identifiers=(), keep_samples=True, progress=None):
    """Parse the queue once. Everything both callers need comes from here.

    ``keep_samples`` off strips the excerpts, which is what the snapshot wants:
    the baseline is written to disk and kept, and a scan that found a credential
    must not be the thing that makes a second copy of it.
    """
    secrets = _secret_shapes()
    matchers = compile_identifiers(identifiers)
    names_map = local_names()
    hits = {slug: {"count": 0, "samples": [], "literals": set()}
            for slug, _key in CATEGORIES}
    name_hits = Counter()
    names = Counter()
    fields = {}
    decoded_samples = {}
    device, identity = {}, {}
    events = unreadable = read_bytes = 0
    stamps = []
    truncated = False

    entries = _files()
    for index, (path, size) in enumerate(entries):
        if progress:
            progress(index, len(entries))
        if read_bytes + size > MAX_BYTES:
            truncated = True
            break
        read_bytes += size
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                unreadable += 1
                continue
            events += 1

            payload = event.get("event_data") or {}
            name = payload.get("event_name")
            if isinstance(name, str):
                names[name] += 1
            stamp = payload.get("client_timestamp")
            if isinstance(stamp, str) and len(stamp) >= 10:
                stamps.append(stamp[:10])

            for dotted, value in _walk(event):
                entry = fields.setdefault(dotted, {"count": 0, "sample": None})
                entry["count"] += 1
                if entry["sample"] is None and value not in (None, ""):
                    entry["sample"] = str(value)[:VALUE_CLIP]
                if dotted in DEVICE_FIELDS:
                    device.setdefault(dotted, str(value))
                if dotted in IDENTITY_FIELDS:
                    identity.setdefault(dotted, str(value))
                if not isinstance(value, str) or not value:
                    continue
                # Whole-value match, not a substring one: a skill called "pm"
                # is then as findable as one called "newproject", and no field
                # matches merely because a short name occurs inside it.
                if value.lower() in names_map:
                    name_hits[(value.lower(), dotted)] += 1
                _scan_text(value, matchers, secrets, hits, keep_samples)
                plain = decode_b64(value)
                if plain is None:
                    continue
                decoded_samples.setdefault(dotted, plain[:VALUE_CLIP * 3])
                _scan_text(plain, matchers, secrets, hits, keep_samples)

    if progress:
        progress(len(entries), len(entries))

    # The label on the field is not evidence of what is in it. Compare.
    ids = stable_ids()
    device_value = identity.get("event_data.device_id")
    if device_value is None:
        device_kind = None
    elif device_value == ids.get("userID"):
        device_kind = "user"
    elif device_value == ids.get("machineID"):
        device_kind = "machine"
    else:
        device_kind = "unknown"

    return {
        "dir": TELEMETRY_DIR,
        "exists": os.path.isdir(TELEMETRY_DIR),
        "files": len(entries),
        "bytes": read_bytes,
        "events": events,
        "unreadable": unreadable,
        "truncated": truncated,
        "oldest": min(stamps) if stamps else None,
        "newest": max(stamps) if stamps else None,
        "event_names": [{"name": n, "count": c} for n, c in names.most_common()],
        "device": [{"path": p, "value": device[p]} for p in DEVICE_FIELDS
                   if p in device],
        "identity": [{"path": p, "value": identity[p]} for p in IDENTITY_FIELDS
                     if p in identity],
        "fields": [{"path": p, "count": e["count"], "sample": e["sample"]}
                   for p, e in sorted(fields.items())],
        "decoded": [{"path": p, "sample": v}
                    for p, v in sorted(decoded_samples.items())],
        "device_id_kind": device_kind,
        "local_names": [
            {"name": name, "kind": names_map[name][0], "path": names_map[name][1],
             "field": field, "count": count}
            for (name, field), count in sorted(name_hits.items(),
                                               key=lambda kv: (-kv[1], kv[0]))
        ],
        "categories": [
            {"slug": slug, "key": key, "count": hits[slug]["count"],
             "literals": sorted(hits[slug]["literals"]),
             "samples": hits[slug]["samples"]}
            for slug, key in CATEGORIES
        ],
    }


def collect_summary(identifiers=()):
    """The compact shape recorded in the snapshot.

    Counts only, no excerpts and no matched literals: the baseline lands in a
    file on disk, and this scan exists to find content that should not be
    copied around, not to copy it once more.
    """
    report = read_queue(identifiers, keep_samples=False)
    return {
        "exists": report["exists"],
        "files": report["files"],
        "events": report["events"],
        "bytes": report["bytes"],
        "oldest": report["oldest"],
        "newest": report["newest"],
        "hits": {c["slug"]: c["count"] for c in report["categories"] if c["count"]},
        "local_names": len(report["local_names"]),
    }


def build_report(account=None, progress=None):
    """The full picture for the interface."""
    return read_queue(local_identifiers(account), keep_samples=True,
                      progress=progress)


def verdict_key(report):
    """One-line conclusion, as a translation key."""
    hits = {c["slug"]: c["count"] for c in report["categories"]}
    if hits.get(CAT_SECRET[0]):
        return "telemetry.verdict.secret"
    if any(hits.get(slug) for slug, _key in CATEGORIES):
        return "telemetry.verdict.identifiers"
    if not report["events"]:
        return "telemetry.verdict.empty"
    return "telemetry.verdict.clean"
