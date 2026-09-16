"""Admin dashboard: the row your organisation sees about you.

The other views in this program ask what is being captured *on this machine*.
This one asks the opposite question, and it is the one people actually want
answered: **what does the organisation already see, without capturing anything
at all?**

On a Team or Enterprise seat the answer is a dashboard. Owners and Primary
Owners open Analytics and get active members, adoption, product stickiness,
"how agentic is their work?", top members by chats and artifacts, a Claude Code
leaderboard by lines of code, spend concentration, and a per-user, per-model
CSV with request counts and token counts in it. None of it is conversation
content. All of it is about a named person.

This module reconstructs that row from the local transcripts, so the figures
stop being an abstraction. Read, never written.

Three limits, stated in the interface as well:

* **Claude Code only.** Chats on claude.ai, artifacts made there, Cowork and
  Design sessions leave nothing on this disk. Those rows of the real dashboard
  stay empty here and are marked as such.
* **A lower bound.** A session deleted from ``~/.claude`` is gone from this
  count and still counted server-side. The real figures are never smaller than
  these.
* **Spend is list price.** Token counts are what actually happened; the dollar
  column is those tokens at the published per-model rate. On a seat-based plan
  no one is invoiced for it -- but the dashboard shows the number anyway, and
  it is the number that gets a person asked about.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .core import collect_account_and_mcp
from .data import decode_project_path, transcript_files
from .i18n import t
from .observer import BUSINESS_END, BUSINESS_START
# One definition of "a line a person typed" for the whole program: tool results
# and subagent turns are recorded as user turns too, and both must stay out.
from .worktime import STAMP, _is_prompt as is_own_prompt

# Published list price per 1M tokens, (input, output). Longest prefix wins, so
# a dated id such as claude-haiku-4-5-20251001 resolves to its family. A model
# that is not in here is counted in tokens and reported as unpriced rather than
# silently valued at zero.
PRICES = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4": (1.0, 5.0),
}
# Cache writes cost more than fresh input, cache reads a fraction of it. Both
# are multiples of the input rate rather than separate columns, which is how
# the rates are published and one less number to get wrong.
CACHE_WRITE_FACTOR = 1.25
CACHE_READ_FACTOR = 0.10

# Night is the part of "outside business hours" that reads differently: an
# evening at 20:00 is ordinary, 01:00 is not.
NIGHT_START, NIGHT_END = 22, 6
# Stickiness in the dashboard is a daily-active over monthly-active ratio.
STICKINESS_WINDOW = 28

ASSISTANT_MARK = b'"type":"assistant"'
USER_MARK = b'"type":"user"'

# A subagent writes its own transcript into a nested folder. Those are turns of
# the session above them, not sessions a person started, and counting them as
# sessions would inflate every per-session figure on the page.
SUBAGENT_DIR = "subagents"

# Tools whose accepted output is code. The dashboard's "lines of code accepted"
# counts exactly this: what Claude wrote and the user kept.
EDIT_TOOLS = {"Write": "content", "Edit": "new_string", "NotebookEdit": "new_source"}

# What a tool call was for. The tile counts calls per prompt and stops there;
# this breakdown is what decides whether a high figure reads as leverage or as
# somebody who stopped looking, so the view states it next to the ratio.
# Anything unlisted lands in "other", which keeps an unknown tool visible
# instead of quietly inflating one of the buckets.
TOOL_PURPOSE = (
    ("inspect", {"Bash", "Read", "Grep", "Glob", "NotebookRead", "WebSearch",
                 "WebFetch", "ToolSearch", "Monitor", "TaskOutput", "ListAgents"}),
    ("write", {"Write", "Edit", "MultiEdit", "NotebookEdit"}),
    ("delegate", {"Task", "Agent", "Skill", "SendMessage", "Workflow"}),
    ("confirm", {"AskUserQuestion", "ExitPlanMode"}),
)


def price_for(model):
    """(input, output) per 1M tokens, or None for a model with no known rate."""
    match = None
    for prefix, rate in PRICES.items():
        if model.startswith(prefix) and (match is None or len(prefix) > len(match[0])):
            match = (prefix, rate)
    return match[1] if match else None


def spend_of(model, tokens):
    """List-price value of one model's token counts, in USD."""
    rate = price_for(model)
    if rate is None:
        return 0.0
    price_in, price_out = rate
    return (tokens["input"] * price_in
            + tokens["cache_write"] * price_in * CACHE_WRITE_FACTOR
            + tokens["cache_read"] * price_in * CACHE_READ_FACTOR
            + tokens["output"] * price_out) / 1_000_000


def human_count(value):
    """12345678 -> '12.3M'. The dashboard never prints a raw token count."""
    value = int(value)
    for limit, suffix in ((1_000_000_000, "G"), (1_000_000, "M"), (1_000, "k")):
        if abs(value) >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(value)


def money(value):
    """USD the way the spend report writes it.

    Deliberately not localised: this column reproduces an export that is issued
    in US format, and the field names next to it are printed verbatim for the
    same reason.
    """
    return f"${value:,.2f}"


def grouped(value):
    """An integer with the reading language's thousands separator.

    Every count this view states in its own words goes through here. The CSV
    table above is the exception, see ``money``.
    """
    return f"{int(value):,}".replace(",", t("format.thousands"))


def _empty_tokens():
    return {"requests": 0, "input": 0, "cache_write": 0, "cache_read": 0,
            "output": 0}


def _add_usage(target, usage):
    target["requests"] += 1
    target["input"] += usage.get("input_tokens") or 0
    target["cache_write"] += usage.get("cache_creation_input_tokens") or 0
    target["cache_read"] += usage.get("cache_read_input_tokens") or 0
    target["output"] += usage.get("output_tokens") or 0


def _total_tokens(entry):
    return (entry["input"] + entry["cache_write"] + entry["cache_read"]
            + entry["output"])


def _local(stamp, cache):
    """b'2026-07-10T05:10' -> (local date, hour, weekday).

    Parsed by slicing, once per unique minute across the whole history -- the
    field is fixed width and ``strptime`` would dominate the run.
    """
    known = cache.get(stamp)
    if known is None:
        text = stamp.decode()
        moment = datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]),
                          int(text[11:13]), int(text[14:16]),
                          tzinfo=timezone.utc).astimezone()
        known = (moment.date(), moment.hour, moment.weekday())
        cache[stamp] = known
    return known


def _lines_of(value):
    """Lines of real code in a tool's output.

    Blank lines and lone brackets are not lines of code; the published metric
    excludes them, so this does too.
    """
    if not isinstance(value, str):
        return 0
    return sum(1 for line in value.splitlines() if len(line.strip()) > 3)


def _count_code(name, payload, loc):
    """Accepted lines from one tool call."""
    field = EDIT_TOOLS.get(name)
    if field:
        loc[name] += _lines_of(payload.get(field))
    elif name == "MultiEdit":
        for edit in payload.get("edits") or []:
            if isinstance(edit, dict):
                loc["Edit"] += _lines_of(edit.get("new_string"))


def build_report(progress=None):
    """Every figure the organisation's analytics would hold about this account."""
    account, _mcp = collect_account_and_mcp()
    stamp_cache = {}

    models = defaultdict(_empty_tokens)
    month_models = defaultdict(_empty_tokens)          # (month, model)
    projects = {}
    tools = Counter()
    skills = Counter()
    loc = Counter()
    hours = Counter()
    weekdays = Counter()
    days = set()
    subagents = 0
    events = own = assistant = messages = 0
    unreadable = 0
    sessions = 0

    entries = list(transcript_files())
    for index, (bucket, path) in enumerate(entries):
        if progress:
            progress(index, len(entries))
        entry = projects.get(bucket)
        if entry is None:
            entry = projects[bucket] = {
                "bucket": bucket, "label": decode_project_path(bucket),
                "sessions": 0, "messages": 0, "own": 0, "bytes": 0,
                "first": None, "last": None}
        if os.sep + SUBAGENT_DIR + os.sep not in path:
            entry["sessions"] += 1
            sessions += 1
        try:
            entry["bytes"] += os.path.getsize(path)
            handle = open(path, "rb")
        except OSError:
            unreadable += 1
            continue

        with handle:
            for line in handle:
                found = STAMP.search(line)
                day = None
                if found is not None:
                    day, hour, weekday = _local(found.group(1), stamp_cache)
                    events += 1
                    hours[hour] += 1
                    weekdays[weekday] += 1
                    days.add(day)
                    if entry["first"] is None or day < entry["first"]:
                        entry["first"] = day
                    if entry["last"] is None or day > entry["last"]:
                        entry["last"] = day

                is_assistant = ASSISTANT_MARK in line
                if is_assistant or USER_MARK in line:
                    messages += 1
                    entry["messages"] += 1
                if is_own_prompt(line):
                    own += 1
                    entry["own"] += 1
                if not is_assistant:
                    continue

                # Only assistant turns carry the usage block and the tool calls,
                # so only these are parsed -- roughly a third of the lines.
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                assistant += 1
                model = message.get("model")
                usage = message.get("usage")
                if model and isinstance(usage, dict):
                    _add_usage(models[model], usage)
                    if day is not None:
                        _add_usage(month_models[(day.strftime("%Y-%m"), model)], usage)
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or "?"
                    tools[name] += 1
                    payload = block.get("input")
                    if not isinstance(payload, dict):
                        continue
                    _count_code(name, payload, loc)
                    if name in ("Task", "Agent"):
                        subagents += 1
                    elif name == "Skill":
                        skills[payload.get("skill") or "?"] += 1

    if progress:
        progress(len(entries), len(entries))

    return _assemble(account, len(entries), sessions, models, month_models,
                     projects, tools, skills, loc, hours, weekdays, days,
                     subagents, events, own, assistant, messages, unreadable)


def _assemble(account, transcripts, sessions, models, month_models, projects,
              tools, skills, loc, hours, weekdays, days, subagents, events,
              own, assistant, messages, unreadable):
    """Everything the pass collected, turned into the figures a dashboard shows."""
    model_rows = []
    totals = _empty_tokens()
    total_spend = 0.0
    unpriced = []
    for name, counts in models.items():
        value = spend_of(name, counts)
        total_spend += value
        if price_for(name) is None:
            unpriced.append(name)
        for key in totals:
            totals[key] += counts[key]
        model_rows.append({"model": name, **counts,
                           "tokens": _total_tokens(counts), "spend": value,
                           "priced": price_for(name) is not None})
    model_rows.sort(key=lambda row: -row["spend"] or -row["tokens"])

    months = defaultdict(lambda: {"models": [], "spend": 0.0, "requests": 0,
                                  "tokens": 0})
    for (month, name), counts in month_models.items():
        value = spend_of(name, counts)
        record = months[month]
        record["models"].append({"model": name, **counts,
                                 "tokens": _total_tokens(counts), "spend": value})
        record["spend"] += value
        record["requests"] += counts["requests"]
        record["tokens"] += _total_tokens(counts)
    month_rows = [{"month": month, **record} for month, record in sorted(months.items())]
    for record in month_rows:
        record["models"].sort(key=lambda row: -row["spend"] or -row["tokens"])

    project_rows = sorted(
        ({**entry,
          "first": entry["first"].isoformat() if entry["first"] else None,
          "last": entry["last"].isoformat() if entry["last"] else None}
         for entry in projects.values()),
        key=lambda entry: -entry["messages"])

    first_day = min(days) if days else None
    last_day = max(days) if days else None
    span = (last_day - first_day).days + 1 if days else 0
    recent = 0
    if last_day is not None:
        edge = last_day - timedelta(days=STICKINESS_WINDOW - 1)
        recent = sum(1 for day in days if day >= edge)

    off_hours = sum(count for hour, count in hours.items()
                    if hour < BUSINESS_START or hour >= BUSINESS_END)
    night = sum(count for hour, count in hours.items()
                if hour >= NIGHT_START or hour < NIGHT_END)
    weekend = weekdays.get(5, 0) + weekdays.get(6, 0)
    workdays = [(day, weekdays.get(day, 0)) for day in range(5)]
    code_lines = sum(loc.values())

    calls = sum(tools.values())
    mix_counts = {name: 0 for name, _members in TOOL_PURPOSE}
    mix_counts["other"] = 0
    for name, count in tools.items():
        bucket = next((purpose for purpose, members in TOOL_PURPOSE
                       if name in members), "other")
        mix_counts[bucket] += count
    mix = [{"purpose": purpose, "calls": mix_counts[purpose],
            "share": share(mix_counts[purpose], calls)}
           for purpose in [name for name, _members in TOOL_PURPOSE] + ["other"]
           if mix_counts[purpose]]

    return {
        "account": account,
        "sessions": sessions,
        "transcripts": transcripts,
        "subagent_transcripts": transcripts - sessions,
        "unreadable": unreadable,
        "bytes": sum(entry["bytes"] for entry in project_rows),
        "messages": messages,
        "own_messages": own,
        "assistant_messages": assistant,
        "events": events,
        "active_days": len(days),
        "span_days": span,
        "first_day": first_day.isoformat() if first_day else None,
        "last_day": last_day.isoformat() if last_day else None,
        "adoption": len(days) / span if span else 0.0,
        "stickiness": recent / STICKINESS_WINDOW if days else 0.0,
        "models": model_rows,
        "months": month_rows,
        "unpriced": sorted(set(unpriced)),
        "totals": {**totals, "tokens": _total_tokens(totals), "spend": total_spend,
                   "cache_share": (totals["cache_read"] / _total_tokens(totals)
                                   if _total_tokens(totals) else 0.0)},
        "projects": project_rows,
        "tools": tools.most_common(),
        "tool_calls": sum(tools.values()),
        "tools_per_prompt": sum(tools.values()) / own if own else 0.0,
        "tool_mix": mix,
        "inspect_per_write": (mix_counts["inspect"] / mix_counts["write"]
                              if mix_counts["write"] else 0.0),
        "inspect_share": share(mix_counts["inspect"], sum(tools.values())),
        "lines_per_prompt": code_lines / own if own else 0.0,
        "prompts_per_day": own / len(days) if days else 0.0,
        "subagents": subagents,
        "skills": skills.most_common(),
        "loc": {"total": code_lines, **dict(loc)},
        "loc_per_day": code_lines / len(days) if days else 0.0,
        "spend_per_session": total_spend / sessions if sessions else 0.0,
        "spend_per_day": total_spend / len(days) if days else 0.0,
        "spend_per_prompt": total_spend / own if own else 0.0,
        "hours": {hour: hours.get(hour, 0) for hour in range(24)},
        "weekdays": {day: weekdays.get(day, 0) for day in range(7)},
        "off_hours": off_hours,
        "night": night,
        "weekend": weekend,
        "saturday": weekdays.get(5, 0),
        "sunday": weekdays.get(6, 0),
        "peak_weekday": max(weekdays, key=lambda d: weekdays[d]) if weekdays else None,
        "quiet_weekday": min(workdays, key=lambda pair: pair[1])[0] if workdays else None,
        "peak_hour": max(hours, key=lambda h: hours[h]) if hours else None,
        "night_hours": [(hour, hours[hour]) for hour in
                        list(range(NIGHT_START, 24)) + list(range(0, NIGHT_END))
                        if hours.get(hour)],
        "business_start": BUSINESS_START,
        "business_end": BUSINESS_END,
        "night_start": NIGHT_START,
        "night_end": NIGHT_END,
        "stickiness_window": STICKINESS_WINDOW,
    }


def share(part, whole):
    """Percentage, as the dashboard prints it."""
    return (part / whole * 100) if whole else 0.0


def pattern_rows(report):
    """The working-pattern table as (label, value, tone) triples.

    Built at render time rather than stored in the report: the weekday names in
    it are translated, so the window survives a language change without the
    whole scan having to run again.
    """
    events = report["events"]
    if not events:
        return []

    def as_share(count):
        return t("analytics.value.share", count=grouped(count),
                 share=f"{share(count, events):.1f}")

    def as_day(day):
        count = report["weekdays"][day]
        return t("analytics.value.day", day=t(f"weekday.{day}"),
                 count=grouped(count), share=f"{share(count, events):.1f}")

    night_rows = report["night_hours"]
    rows = [
        (t("analytics.metric.events"),
         t("analytics.value.plain", count=grouped(events)), "INFO"),
        (t("analytics.metric.offhours", start=report["business_start"],
           end=report["business_end"]),
         as_share(report["off_hours"]),
         "MEDIUM" if report["off_hours"] else "OK"),
        (t("analytics.metric.night", start=report["night_start"],
           end=report["night_end"]),
         as_share(report["night"]), "MEDIUM" if report["night"] else "OK"),
        (t("analytics.metric.weekend"),
         t("analytics.value.weekend", count=grouped(report["weekend"]),
           share=f"{share(report['weekend'], events):.1f}",
           sat=grouped(report["saturday"]), sun=grouped(report["sunday"])),
         "MEDIUM" if report["weekend"] else "OK"),
    ]
    if report["peak_weekday"] is not None:
        rows.append((t("analytics.metric.peakday"),
                     as_day(report["peak_weekday"]), "INFO"))
    if report["quiet_weekday"] is not None:
        rows.append((t("analytics.metric.quietday"),
                     as_day(report["quiet_weekday"]), "INFO"))
    if report["peak_hour"] is not None:
        rows.append((t("analytics.metric.peakhour"),
                     t("analytics.value.hour", hour=f"{report['peak_hour']:02d}",
                       count=grouped(report["hours"][report["peak_hour"]])),
                     "INFO"))
    rows.append((
        t("analytics.metric.nightoutliers"),
        ", ".join(f"{hour:02d}:00 {grouped(count)}" for hour, count in night_rows)
        or t("analytics.value.none"),
        "MEDIUM" if night_rows else "OK"))
    return rows


def highlights(report):
    """What an owner's eye stops on, worst first.

    Each entry is a translation key with its parameters and a tone, so the
    window and the terminal render the same conclusions.
    """
    found = []
    totals = report["totals"]
    if totals["requests"]:
        found.append({
            "key": "analytics.high.spend", "tone": "MEDIUM" if totals["spend"] >= 500
            else "INFO",
            "params": {"spend": money(totals["spend"]),
                       "days": report["active_days"],
                       "requests": grouped(totals["requests"])}})
    if report["own_messages"]:
        found.append({
            "key": "analytics.high.agentic",
            "tone": "MEDIUM" if report["tools_per_prompt"] >= 5 else "INFO",
            "params": {"ratio": f"{report['tools_per_prompt']:.1f}",
                       "calls": grouped(report["tool_calls"]),
                       "prompts": grouped(report["own_messages"])}})
    if report["loc"]["total"]:
        found.append({"key": "analytics.high.code", "tone": "INFO",
                      "params": {"lines": grouped(report["loc"]["total"])}})
    if report["skills"]:
        found.append({
            "key": "analytics.high.names", "tone": "MEDIUM",
            "params": {"names": ", ".join(name for name, _n in report["skills"][:6]),
                       "count": len(report["skills"])}})
    if report["weekend"] or report["night"]:
        found.append({
            "key": "analytics.high.pattern", "tone": "MEDIUM",
            "params": {"weekend": f"{share(report['weekend'], report['events']):.1f}",
                       "night": f"{share(report['night'], report['events']):.1f}"}})
    found.sort(key=lambda item: 0 if item["tone"] == "MEDIUM" else 1)
    return found


def readings(report):
    """The same figures, read both ways.

    Every number on this page carries two readings, and which one lands is not
    decided by the number. A view that shows a person their own dashboard row
    and leaves the interpretation to them is only half the work, so both
    readings are spelled out, for the same figure, side by side.
    """
    totals = report["totals"]
    found = []

    def entry(topic, up_params=None, down_params=None):
        found.append({
            "topic": topic,
            "label": f"analytics.reading.{topic}.label",
            "up": {"key": f"analytics.reading.{topic}.up", "params": up_params or {}},
            "down": {"key": f"analytics.reading.{topic}.down",
                     "params": down_params or {}}})

    if totals["requests"]:
        entry("spend", {"spend": money(totals["spend"])})
    if report["own_messages"] and report["tool_calls"]:
        entry("agentic",
              {"ratio": f"{report['tools_per_prompt']:.1f}",
               "lines": f"{report['lines_per_prompt']:.0f}",
               "prompts": f"{report['prompts_per_day']:.0f}"},
              {"inspect": f"{report['inspect_share']:.0f}"})
    if report["loc"]["total"]:
        entry("code", {"lines": grouped(report["loc"]["total"])})
    if report["weekend"] or report["night"]:
        entry("pattern", {"days": report["active_days"], "span": report["span_days"]})
    if report["skills"]:
        entry("names", {"count": len(report["skills"])})
    return found


def consequences(report):
    """What follows from the readings, in the order the options are real.

    The third one is deliberately included: lowering the ratio is possible, it
    is a bad trade, and a person is owed the choice rather than the conclusion.
    """
    if not report["sessions"]:
        return []
    return [
        {"key": "analytics.consequence.hidden", "params": {}},
        {"key": "analytics.consequence.prepare",
         "params": {"lines": grouped(report["loc"]["total"]),
                    "sessions": grouped(report["sessions"]),
                    "days": report["active_days"],
                    "inspect": f"{report['inspect_share']:.0f}"}},
        {"key": "analytics.consequence.lower", "params": {}},
    ]


def verdict_key(report):
    """One-line conclusion, as a translation key."""
    if not report["sessions"]:
        return "analytics.verdict.empty"
    if report["weekend"] or report["night"]:
        return "analytics.verdict.pattern"
    return "analytics.verdict.usage"
