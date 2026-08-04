"""Working time reconstructed from the transcript timestamps.

Every line a session writes carries a UTC timestamp, to the millisecond. Read
them in order and the transcripts stop being a record of *what* was worked on
and become a record of *when*: start of day, breaks, end of day, the Sunday
evening, the hour after midnight. Nobody set up a time clock. One exists anyway,
and it is more precise than any clock a works council ever negotiated over.

This module runs that reconstruction on the local copy, in the machine's own
time zone, so what it produces is exactly what anyone holding the data could
produce. It is also useful for its own sake -- it is the closest thing to an
honest answer to "how long did that actually take".

Method, and the limits of it:

* A minute with at least one event counts as a worked minute.
* A gap of up to ``IDLE_GAP`` minutes counts as continued work -- reading,
  typing, thinking. A longer gap is a break and is not counted.
* Days are cut at local midnight, the way a timesheet cuts them. Work past
  midnight therefore lands on the following day.
* It is a lower bound. Work that happened without Claude Code leaves no
  timestamp here, and the figures are activity, not attendance.

Reads, never writes.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .data import decode_project_path, transcript_files
from .observer import BUSINESS_END, BUSINESS_START

# A pause longer than this ends a work block. Fifteen minutes is the usual
# threshold in time tracking: short enough that a coffee break shows up, long
# enough that reading a diff does not.
IDLE_GAP = 15

# Only ever used to *count* long days and weeks, never to judge them. Stated in
# the interface so the number is not mistaken for a rule.
DAY_TARGET_HOURS = 8.0
WEEK_TARGET_HOURS = 40.0

# Fixed-width ISO-8601 in UTC, which is what Claude Code writes. Captured to
# the minute -- second-level resolution would only make the cache below miss.
STAMP = re.compile(rb'"timestamp":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d):\d\d(?:\.\d+)?Z"')

# What a person actually typed. Tool results and subagent turns are recorded as
# ``"type":"user"`` as well, so both are excluded -- and a tool result carrying a
# nested transcript is exactly why the exclusion is checked first. Matched on the
# raw bytes rather than through ``json.loads``: measured against a parsed pass
# over the whole history this agrees on every well-formed line and costs a
# fraction of the time.
PROMPT_MARK = b'"type":"user"'
TOOL_RESULT = b'"toolUseResult"'
SIDECHAIN = b'"isSidechain":true'


def human_minutes(total):
    """Minutes as ``7:12 h`` -- the way a timesheet writes it."""
    total = max(0, int(total))
    return f"{total // 60}:{total % 60:02d} h"


def clock(minute):
    """Epoch minute as local ``07:10``."""
    return datetime.fromtimestamp(minute * 60).strftime("%H:%M")


def _stamp_to_local(stamp, cache):
    """b'2026-07-10T05:10' -> (epoch minute, local date).

    Parsed by slicing rather than through ``strptime``: the field is fixed
    width, and this runs once per unique minute across the whole history.
    """
    known = cache.get(stamp)
    if known is None:
        text = stamp.decode()
        moment = datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]),
                          int(text[11:13]), int(text[14:16]), tzinfo=timezone.utc)
        known = (int(moment.timestamp()) // 60, moment.astimezone().date())
        cache[stamp] = known
    return known


def blocks(minutes, gap=IDLE_GAP):
    """Sorted epoch minutes -> [(first, last)] runs, gaps up to `gap` bridged."""
    runs = []
    for minute in sorted(minutes):
        if runs and minute - runs[-1][1] <= gap:
            runs[-1][1] = minute
        else:
            runs.append([minute, minute])
    return [(first, last) for first, last in runs]


def _is_prompt(line):
    """A line that carries something a person typed."""
    return (PROMPT_MARK in line and TOOL_RESULT not in line
            and SIDECHAIN not in line)


def _day_record(day, minutes, projects, prompts):
    """One day, measured. Also returns its per-hour minutes for the histogram."""
    runs = blocks(minutes)
    hours = Counter()
    active = 0
    for first, last in runs:
        start = datetime.fromtimestamp(first * 60)
        offset = start.hour * 60 + start.minute
        length = last - first + 1
        active += length
        for step in range(length):
            hours[((offset + step) // 60) % 24] += 1

    off_hours = sum(count for hour, count in hours.items()
                    if hour < BUSINESS_START or hour >= BUSINESS_END)
    first_minute, last_minute = runs[0][0], runs[-1][1]
    span = last_minute - first_minute + 1
    return {
        "date": day.isoformat(),
        "weekday": day.weekday(),
        "weekend": day.weekday() >= 5,
        "start": clock(first_minute),
        "end": clock(last_minute),
        "span": span,
        "active": active,
        "pause": span - active,
        "blocks": len(runs),
        "longest_block": max(last - first + 1 for first, last in runs),
        "off_hours": off_hours,
        "prompts": prompts,
        "projects": sorted(projects),
    }, hours


def build_report(progress=None):
    """Working time per day, week, weekday, hour and project.

    ``days`` comes newest first -- it reads as a log. ``weeks`` comes oldest
    first, because a run of weeks reads as a trend.
    """
    cache = {}
    labels = {}
    collected = defaultdict(lambda: {"minutes": set(), "projects": set(),
                                     "prompts": 0})
    project_minutes = defaultdict(set)
    project_days = defaultdict(set)
    stamps = 0

    entries = list(transcript_files())
    for index, (bucket, path) in enumerate(entries):
        if progress:
            progress(index, len(entries))
        label = labels.setdefault(bucket, decode_project_path(bucket))
        try:
            handle = open(path, "rb")
        except OSError:
            continue
        with handle:
            # Line by line: a transcript averages several kilobytes per line,
            # so the loop is short and the classification per line is exact.
            for line in handle:
                found = STAMP.search(line)
                if found is None:
                    continue
                minute, day = _stamp_to_local(found.group(1), cache)
                stamps += 1
                bucket_day = collected[day]
                bucket_day["minutes"].add(minute)
                bucket_day["projects"].add(label)
                if _is_prompt(line):
                    bucket_day["prompts"] += 1
                project_minutes[label].add(minute)
                project_days[label].add(day)

    if progress:
        progress(len(entries), len(entries))

    days = []
    hour_active = Counter()
    weekday_active = Counter()
    weeks = defaultdict(lambda: {"active": 0, "days": 0})
    for day in sorted(collected, reverse=True):
        gathered = collected[day]
        record, hours = _day_record(day, gathered["minutes"],
                                   gathered["projects"], gathered["prompts"])
        days.append(record)
        hour_active.update(hours)
        weekday_active[day.weekday()] += record["active"]
        year, week, _weekday = day.isocalendar()
        entry = weeks[(year, week)]
        entry["active"] += record["active"]
        entry["days"] += 1

    active = [d["active"] for d in days]
    total_active = sum(active)
    ordered = sorted(active)
    middle = len(ordered) // 2
    median = 0 if not ordered else (
        ordered[middle] if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) // 2)

    projects = sorted(
        ({"label": label,
          "active": sum(last - first + 1 for first, last in blocks(minutes)),
          "days": len(project_days[label])}
         for label, minutes in project_minutes.items()),
        key=lambda p: -p["active"])

    longest = max(days, key=lambda d: d["active"], default=None)
    earliest = min(days, key=lambda d: d["start"], default=None)
    latest = max(days, key=lambda d: d["end"], default=None)

    return {
        "days": days,
        "weeks": [{"year": year, "week": week, "active": entry["active"],
                   "days": entry["days"]}
                  for (year, week), entry in sorted(weeks.items())],
        "projects": projects,
        "active_days": len(days),
        "first_day": days[-1]["date"] if days else None,
        "last_day": days[0]["date"] if days else None,
        "total_active": total_active,
        "total_span": sum(d["span"] for d in days),
        "total_pause": sum(d["pause"] for d in days),
        "total_prompts": sum(d["prompts"] for d in days),
        "average_active": total_active // len(days) if days else 0,
        "median_active": median,
        "longest_day": {"date": longest["date"], "active": longest["active"]}
                       if longest else None,
        "earliest_start": {"date": earliest["date"], "time": earliest["start"]}
                          if earliest else None,
        "latest_end": {"date": latest["date"], "time": latest["end"]}
                      if latest else None,
        "longest_block": max((d["longest_block"] for d in days), default=0),
        "weekday_active": {n: weekday_active.get(n, 0) for n in range(7)},
        "hour_active": {n: hour_active.get(n, 0) for n in range(24)},
        "off_hours_active": sum(d["off_hours"] for d in days),
        "off_hours_days": sum(1 for d in days if d["off_hours"]),
        "weekend_active": sum(d["active"] for d in days if d["weekend"]),
        "weekend_days": sum(1 for d in days if d["weekend"]),
        "long_days": sum(1 for d in days
                         if d["active"] > DAY_TARGET_HOURS * 60),
        "long_weeks": sum(1 for w in weeks.values()
                          if w["active"] > WEEK_TARGET_HOURS * 60),
        "sessions": len(entries),
        "stamps": stamps,
        "idle_gap": IDLE_GAP,
        "day_target": DAY_TARGET_HOURS,
        "week_target": WEEK_TARGET_HOURS,
        "business_start": BUSINESS_START,
        "business_end": BUSINESS_END,
    }


def verdict_key(report):
    """One-line conclusion, as a translation key."""
    if not report["active_days"]:
        return "worktime.verdict.empty"
    if report["off_hours_active"] and report["weekend_active"]:
        return "worktime.verdict.both"
    if report["off_hours_active"] or report["weekend_active"]:
        return "worktime.verdict.offhours"
    return "worktime.verdict.business"
