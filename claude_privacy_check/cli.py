"""Command line interface: argument parsing and terminal output."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import (about, analytics, core, data, instructions, observer, telemetry,
               watch, worktime)
from . import license as licence
from .i18n import (apply_startup_language, available_languages, current_language,
                   save_preference, t)

COLORS = {"CRITICAL": "\033[1;31m", "HIGH": "\033[31m", "MEDIUM": "\033[33m",
          "INFO": "\033[2m", "OK": "\033[32m", "RESET": "\033[0m"}


def paint(text, key):
    if not sys.stdout.isatty():
        return text
    return f"{COLORS.get(key, '')}{text}{COLORS['RESET']}"


def clip(value, width=90):
    text = str(value)
    return text if len(text) <= width else text[:width - 1] + "…"


def sev(severity):
    return paint(f"[{t('severity.' + severity)}]", severity)


def bar(fraction, width=28):
    """A magnitude as a block bar -- the terminal's answer to the GUI meters.

    Anything above zero gets at least one block: a row that reads 0:12 h next to
    an empty bar looks like a rendering fault.
    """
    fraction = max(0.0, min(1.0, fraction))
    filled = max(1, int(round(fraction * width))) if fraction else 0
    return "█" * filled + paint("·" * (width - filled), "INFO")


# ------------------------------------------------------------------ output

def print_findings(findings, quiet):
    shown = [f for f in findings if not quiet or core.SEV_ORDER[f["severity"]] >= 1]
    print(paint(f"── {t('section.assessment')} ──", "INFO"))
    if not shown:
        print(paint("  ✓ " + t("result.no_monitoring"), "OK"))
        return
    for f in shown:
        print(f"  {sev(f['severity'])} {core.finding_title(f)}")
        print(f"      {clip(core.finding_detail(f), 100)}")


def print_changes(changes, quiet, baseline_time):
    shown = [c for c in changes if not quiet or core.SEV_ORDER[c["severity"]] >= 1]
    print(paint(f"── {t('section.changes', time=baseline_time)} ──", "INFO"))
    if not changes:
        print(paint("  ✓ " + t("result.no_changes"), "OK"))
        return
    if not shown:
        print(paint("  ✓ " + t("result.no_relevant_changes", count=len(changes)), "OK"))
        return
    for c in shown:
        print(f"  {sev(c['severity'])} {c['path']}")
        print(f"      {t('label.before'):<8} {clip(c['before'])}")
        print(f"      {t('label.after'):<8} {clip(c['after'])}")


def print_history(hist):
    if not hist.get("transcript_files"):
        return
    print()
    print(paint(f"── {t('section.local_history')} ──", "INFO"))
    print("  " + t("history.summary", files=hist["transcript_files"],
                   mb=hist["megabytes"], oldest=hist["oldest"]))
    print("  " + t("history.hint"))


# ------------------------------------------------------------- data & delete

def list_data(as_json):
    inventory = data.list_local_data()
    if as_json:
        print(json.dumps(inventory, indent=2, ensure_ascii=False, default=str))
        return 0
    print(paint(f"── {t('section.transcripts')} ──", "INFO"))
    for p in inventory["projects"]:
        mark = paint("  " + t("label.running"), "MEDIUM") if p["has_active"] else ""
        print(f"  {p['label']}{mark}")
        print("      " + t("data.project_line", sessions=len(p["sessions"]),
                           size=data.human_bytes(p["bytes"]),
                           oldest=p["oldest"], newest=p["newest"]))
        print(f"      {p['path']}")
    print()
    print(paint(f"── {t('section.stores')} ──", "INFO"))
    for s in inventory["stores"]:
        print(f"  {t(s['label_key'] + '.name')}: "
              + t("data.store_line", files=s["files"],
                  size=data.human_bytes(s["bytes"])))
        print(f"      {s['path']}")
    print()
    print(t("data.total", size=data.human_bytes(inventory["total_bytes"])))
    print(t("data.delete_hint"))
    return 0


def show_license(as_json):
    report = licence.build_report()
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0

    print(paint(t(licence.verdict_key(report)),
                "OK" if report["present"] and report.get("token_state") != "expired"
                else "CRITICAL" if report.get("token_state") == "expired"
                else "HIGH"))
    print()
    for section in report["sections"]:
        print(paint(f"── {section['title']} ──", "INFO"))
        print(f"  {section['summary']}")
        for row in section["rows"]:
            print(f"  {row['label']}: {row['value']}")
        print()
    print(paint(t("license.raw_hint"), "INFO"))
    return 0 if report["present"] else 1


def show_analytics(as_json):
    report = analytics.build_report()
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0

    group = analytics.grouped
    print(paint(t("analytics.intro"), "INFO"))
    print()
    if not report["sessions"]:
        print("  " + t("analytics.empty"))
        return 0

    totals = report["totals"]
    print(paint(f"── {t('analytics.section.summary')} ──", "INFO"))
    print("  " + t("analytics.subtitle", first=report["first_day"],
                   last=report["last_day"], sessions=report["sessions"],
                   events=group(report["events"])))
    _print_pairs([
        (t("analytics.stat.requests"), group(totals["requests"]), "OK"),
        (t("analytics.stat.tokens"), analytics.human_count(totals["tokens"]), "OK"),
        (t("analytics.stat.spend"), analytics.money(totals["spend"]), "MEDIUM"),
        (t("analytics.stat.sessions"), group(report["sessions"]), "OK"),
        (t("analytics.stat.days", span=report["span_days"]),
         str(report["active_days"]), "OK"),
        (t("analytics.stat.own"), group(report["own_messages"]), "OK"),
        (t("analytics.stat.loc"), group(report["loc"]["total"]), "OK"),
        (t("analytics.stat.tools"), group(report["tool_calls"]), "OK"),
        (t("analytics.stat.ratio"), f"{report['tools_per_prompt']:.1f}",
         "MEDIUM" if report["tools_per_prompt"] >= 5 else "OK"),
        (t("analytics.stat.adoption"), f"{report['adoption'] * 100:.0f} %", "OK"),
        (t("analytics.stat.stickiness", days=report["stickiness_window"]),
         f"{report['stickiness'] * 100:.0f} %", "OK"),
        (t("analytics.stat.cache"), f"{totals['cache_share'] * 100:.1f} %", "OK"),
    ])

    print()
    print(paint(f"── {t('analytics.section.spend')} ──", "INFO"))
    _print_spend_table(report)
    print("  " + paint(t("analytics.spend.fields"), "INFO"))
    print("  " + paint(t("analytics.spend.note"), "INFO"))
    if report["unpriced"]:
        print("  " + paint(t("analytics.spend.unpriced",
                             models=", ".join(report["unpriced"])), "INFO"))

    if report["months"]:
        print()
        print(paint(f"── {t('analytics.section.months')} ──", "INFO"))
        peak = max(month["spend"] for month in report["months"]) or 1
        for month in report["months"]:
            print(f"  {month['month']:<8} {bar(month['spend'] / peak)} "
                  f"{analytics.money(month['spend']):>12}")
            for entry in month["models"]:
                print(f"      {clip(entry['model'], 26):<26} "
                      f"{group(entry['requests']):>8} req  "
                      f"{analytics.human_count(entry['tokens']):>8}  "
                      f"{analytics.money(entry['spend']):>12}")
        print("  " + paint(t("analytics.months.note"), "INFO"))

    print()
    print(paint(f"── {t('analytics.section.concentration')} ──", "INFO"))
    print("  " + t("analytics.concentration.line",
                   session=analytics.money(report["spend_per_session"]),
                   day=analytics.money(report["spend_per_day"]),
                   prompt=analytics.money(report["spend_per_prompt"])))
    print("  " + paint(t("analytics.concentration.note"), "INFO"))

    print()
    print(paint(f"── {t('analytics.section.code')} ──", "INFO"))
    loc = report["loc"]
    print("  " + t("analytics.code.loc", lines=group(loc["total"]),
                   perday=group(round(report["loc_per_day"]))))
    print("  " + t("analytics.code.detail", write=group(loc.get("Write", 0)),
                   edit=group(loc.get("Edit", 0))))
    print("  " + paint(t("analytics.code.export"), "INFO"))
    print("  " + paint(t("analytics.code.missing"), "INFO"))

    print()
    print(paint(f"── {t('analytics.section.agentic')} ──", "INFO"))
    if not report["tool_calls"]:
        print("  " + t("analytics.agentic.none"))
    else:
        print("  " + t("analytics.agentic.line",
                       ratio=f"{report['tools_per_prompt']:.1f}",
                       calls=group(report["tool_calls"]),
                       prompts=group(report["own_messages"])))
        print("  " + t("analytics.agentic.subagents", n=report["subagents"]))
        print()
        peak = report["tools"][0][1] or 1
        for name, count in report["tools"][:12]:
            print(f"  {clip(name, 16):<16} {bar(count / peak)} {group(count):>9}")
        print()
        _print_table(
            (t("analytics.mix.head.purpose"), t("analytics.mix.head.calls"),
             t("analytics.mix.head.share")),
            [(t("analytics.mix." + entry["purpose"]), group(entry["calls"]),
              f"{entry['share']:.1f} %") for entry in report["tool_mix"]],
            left={0})
        if report["inspect_per_write"]:
            print("  " + t("analytics.mix.ratio",
                           ratio=f"{report['inspect_per_write']:.1f}"))
        print("  " + paint(t("analytics.mix.note"), "INFO"))
    if report["skills"]:
        print()
        print(f"  {t('analytics.agentic.skills')}: "
              + ", ".join(f"{name} ({count})" for name, count in report["skills"]))
        print("  " + paint(t("analytics.agentic.skills.note"), "INFO"))

    print()
    print(paint(f"── {t('analytics.section.projects')} ──", "INFO"))
    print("  " + paint(t("analytics.projects.dashboard"), "INFO"))
    print()
    _print_project_table(report["projects"])

    print()
    print(paint(f"── {t('analytics.section.pattern')} ──", "INFO"))
    _print_pairs(analytics.pattern_rows(report))
    print()
    peak = max(report["hours"].values()) or 1
    for hour in range(24):
        count = report["hours"][hour]
        off = hour < report["business_start"] or hour >= report["business_end"]
        label = paint(f"{hour:02d}", "MEDIUM") if off and count else f"{hour:02d}"
        print(f"  {label}   {bar(count / peak)} "
              f"{group(count) if count else '—':>9}")
    print("  " + paint(t("analytics.pattern.note"), "INFO"))
    print("  " + paint(t("analytics.pattern.pointer", tab="--worktime"), "INFO"))

    print()
    print(paint(f"── {t('analytics.section.highlights')} ──", "INFO"))
    for item in analytics.highlights(report):
        print(f"  {paint('·', item['tone'])} {t(item['key'], **item['params'])}")

    readings = analytics.readings(report)
    if readings:
        print()
        print(paint(f"── {t('analytics.section.reading')} ──", "INFO"))
        print("  " + paint(t("analytics.reading.note"), "INFO"))
        width = max(len(t("analytics.reading.up")), len(t("analytics.reading.down")))
        for item in readings:
            print()
            print("  " + t(item["label"]))
            for side, tone in (("up", "OK"), ("down", "MEDIUM")):
                print(f"      {paint(t('analytics.reading.' + side), tone):<{width}}  "
                      + t(item[side]["key"], **item[side]["params"]))
        print()
        print("  " + paint(t("analytics.reading.relative"), "INFO"))

    steps = analytics.consequences(report)
    if steps:
        print()
        print(paint(f"── {t('analytics.section.consequences')} ──", "INFO"))
        for number, step in enumerate(steps, start=1):
            print(f"  {number}. " + t(step["key"], **step["params"]))

    print()
    print(paint(f"── {t('analytics.section.blind')} ──", "INFO"))
    print("  " + paint(t("analytics.blind.note"), "INFO"))
    print("  " + paint(t("analytics.blind.floor"), "INFO"))

    print()
    print(paint(t(analytics.verdict_key(report)), "OK"))
    return 0


def _print_pairs(rows):
    """Label / value / tone triples, aligned on the widest label."""
    if not rows:
        return
    width = max(len(label) for label, _value, _tone in rows)
    for label, value, tone in rows:
        print(f"  {label:<{width}}  {paint(value, tone)}")


def _print_table(head, rows, left, highlight_last=False):
    """A fixed-width table. ``left`` names the columns that read as text."""
    if not rows:
        return
    widths = [max(len(str(row[i])) for row in (head, *rows))
              for i in range(len(head))]

    def render(row):
        return "  ".join(f"{row[i]:<{widths[i]}}" if i in left
                         else f"{row[i]:>{widths[i]}}" for i in range(len(head)))

    print("  " + paint(render(head), "INFO"))
    for index, row in enumerate(rows):
        line = render(row)
        last = highlight_last and index == len(rows) - 1
        print("  " + (paint(line, "MEDIUM") if last else line))


def _print_spend_table(report):
    """The per-user CSV, one row per model, the way the export writes it."""
    group = analytics.grouped
    head = (t("analytics.spend.head.model"), t("analytics.spend.head.requests"),
            t("analytics.spend.head.prompt"), t("analytics.spend.head.completion"),
            t("analytics.spend.head.spend"))
    rows = [(clip(entry["model"], 26), group(entry["requests"]),
             group(entry["input"] + entry["cache_write"] + entry["cache_read"]),
             group(entry["output"]),
             analytics.money(entry["spend"]) if entry["priced"] else "—")
            for entry in report["models"]]
    totals = report["totals"]
    rows.append((t("analytics.spend.total"), group(totals["requests"]),
                 group(totals["input"] + totals["cache_write"] + totals["cache_read"]),
                 group(totals["output"]), analytics.money(totals["spend"])))
    _print_table(head, rows, left={0}, highlight_last=True)


def _print_project_table(projects):
    """Working directory, sessions, messages, own messages, size, period."""
    group = analytics.grouped
    head = (t("analytics.projects.head.project"),
            t("analytics.projects.head.sessions"),
            t("analytics.projects.head.messages"),
            t("analytics.projects.head.own"),
            t("analytics.projects.head.size"),
            t("analytics.projects.head.span"))
    rows = [(clip(entry["label"], 44), group(entry["sessions"]),
             group(entry["messages"]), group(entry["own"]),
             data.human_bytes(entry["bytes"]),
             f"{entry['first'] or '—'} … {entry['last'] or '—'}")
            for entry in projects]
    _print_table(head, rows, left={0, 5})


def show_observer(as_json):
    report = observer.build_report()
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0

    account = report["account"]
    plan = core.plan_label(account)
    print(paint(t("observer.intro"), "INFO"))
    print()
    print(paint(f"── {t('observer.section.identity')} ──", "INFO"))
    print("  " + t("observer.identity.line", org=account.get("organizationName", "—"),
                   plan=plan, role=account.get("organizationRole", "—"),
                   email=account.get("emailAddress", "—")))
    print("  " + paint(t("observer.identity.note"), "INFO"))

    print()
    print(paint(f"── {t('observer.section.projects')} ──", "INFO"))
    print("  " + t("count.projects", n=len(report["projects"])))
    print("  " + paint(t("observer.projects.note"), "INFO"))
    print("  " + paint(t("observer.projects.pointer", tab="--list-data"), "INFO"))

    print()
    print(paint(f"── {t('observer.section.pattern')} ──", "INFO"))
    print("  " + t("observer.pattern.line", sessions=report["sessions"],
                   days=report["active_days"],
                   size=data.human_bytes(report["bytes"])))
    print("  " + t("observer.pattern.hours", start=observer.BUSINESS_START,
                   end=observer.BUSINESS_END, count=report["off_hours"]))
    print("  " + t("observer.pattern.weekend", count=report["weekend"]))
    print("  " + paint(t("observer.pattern.note"), "INFO"))
    print("  " + paint(t("observer.pattern.pointer", tab="--worktime"), "INFO"))

    print()
    print(paint(f"── {t('observer.section.sweep')} ──", "INFO"))
    for category in report["categories"]:
        name = t(category["key"])
        if not category["count"]:
            print(f"  {paint('·', 'OK')} {name}: {t('observer.sweep.clean')}")
            continue
        tone = "CRITICAL" if category["confidence"] == "high" else "MEDIUM"
        print(f"  {paint('!', tone)} {name}: "
              + t("observer.sweep.hit", count=category["count"],
                  sessions=category["sessions"])
              + "  " + paint(f"({t('observer.conf.' + category['confidence'])})", "INFO"))
        for sample in category["samples"]:
            print(f"      {sample['date']}  {sample['project']}")
            print(f"        {clip(sample['excerpt'], 110)}")
    print()
    print(paint(t(observer.verdict_key(report)), "OK"))
    return 0


def show_telemetry(as_json):
    report = telemetry.build_report(core.collect_account_and_mcp()[0])
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0

    print(paint(t("telemetry.intro"), "INFO"))
    print()
    print(paint(f"── {t('telemetry.section.summary')} ──", "INFO"))
    if not report["exists"]:
        print("  " + t("telemetry.summary.missing"))
        return 0
    if not report["events"]:
        print("  " + t("telemetry.summary.empty"))
        return 0
    print("  " + t("telemetry.summary.line", files=report["files"],
                   events=report["events"],
                   size=data.human_bytes(report["bytes"]),
                   oldest=report["oldest"] or "—", newest=report["newest"] or "—"))
    print("  " + paint(t("telemetry.retry_note"), "INFO"))
    if report["truncated"]:
        print("  " + paint(t("telemetry.truncated",
                             size=data.human_bytes(telemetry.MAX_BYTES)), "MEDIUM"))
    if report["unreadable"]:
        print("  " + paint(t("telemetry.unreadable", count=report["unreadable"]),
                           "MEDIUM"))

    print()
    print(paint(f"── {t('telemetry.section.scan')} ──", "INFO"))
    for category in report["categories"]:
        name = t(category["key"])
        if not category["count"]:
            print(f"  {paint('·', 'OK')} {name}: {t('telemetry.scan.clean')}")
            continue
        print(f"  {paint('!', 'CRITICAL')} {name}: "
              + t("telemetry.scan.hit", count=category["count"],
                  literals=", ".join(category["literals"]) or "—"))
        for sample in category["samples"]:
            print(f"        {clip(sample, 110)}")
    print("  " + paint(t("telemetry.scan.note"), "INFO"))
    print("  " + paint(t("telemetry.scan.secret_note"), "INFO"))

    for section, rows, note in (
            ("telemetry.section.device", report["device"], "telemetry.device.note"),
            ("telemetry.section.identity", report["identity"],
             "telemetry.identity.note")):
        print()
        print(paint(f"── {t(section)} ──", "INFO"))
        for row in rows:
            print(f"  {row['path']}: {clip(row['value'], 70)}")
        print("  " + paint(t(note), "INFO"))
        if section.endswith("identity") and report["device_id_kind"]:
            print("  " + paint(t("telemetry.device_id." + report["device_id_kind"]),
                               "MEDIUM"))

    print()
    print(paint(f"── {t('telemetry.section.local_names')} ──", "INFO"))
    if not report["local_names"]:
        print("  " + t("telemetry.local_names.none"))
    for entry in report["local_names"]:
        print(f"  {entry['name']}: "
              + t("telemetry.local_names.row", count=entry["count"],
                  kind=t("telemetry.kind." + entry["kind"]), field=entry["field"]))
    print("  " + paint(t("telemetry.local_names.note"), "INFO"))

    print()
    print(paint(f"── {t('telemetry.section.events')} ──", "INFO"))
    for entry in report["event_names"]:
        print(f"  {entry['count']:5d}  {entry['name']}")
    print("  " + paint(t("telemetry.events.note"), "INFO"))

    if report["decoded"]:
        print()
        print(paint(f"── {t('telemetry.section.decoded')} ──", "INFO"))
        for entry in report["decoded"]:
            print(f"  {entry['path']}")
            print(f"      {clip(entry['sample'], 110)}")
        print("  " + paint(t("telemetry.decoded.note"), "INFO"))

    print()
    print(paint(f"── {t('telemetry.section.fields')} ──", "INFO"))
    for entry in report["fields"]:
        sample = clip(entry["sample"] or "", 60) if entry["sample"] else ""
        print(f"  {entry['count']:5d}  {entry['path']}"
              + (f"  = {sample}" if sample else ""))
    print("  " + paint(t("telemetry.fields.note"), "INFO"))

    print()
    print(paint(t(telemetry.verdict_key(report)), "OK"))
    return 0


def show_worktime(as_json):
    report = worktime.build_report()
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0

    print(paint(t("worktime.intro"), "INFO"))
    print()
    if not report["active_days"]:
        print("  " + t("worktime.empty"))
        return 0

    print(paint(f"── {t('worktime.section.overview')} ──", "INFO"))
    print("  " + t("worktime.subtitle", first=report["first_day"],
                   last=report["last_day"], sessions=report["sessions"],
                   stamps=report["stamps"]))
    longest = report["longest_day"]
    earliest, latest = report["earliest_start"], report["latest_end"]
    night, weekend = report["off_hours_active"], report["weekend_active"]
    rows = [
        (t("worktime.stat.total"),
         worktime.human_minutes(report["total_active"]), "OK"),
        (t("worktime.stat.days"), str(report["active_days"]), "OK"),
        (t("worktime.stat.average"),
         worktime.human_minutes(report["average_active"]), "OK"),
        (t("worktime.stat.median"),
         worktime.human_minutes(report["median_active"]), "OK"),
        (t("worktime.stat.longest", date=longest["date"]),
         worktime.human_minutes(longest["active"]), "OK"),
        (t("worktime.stat.block"),
         worktime.human_minutes(report["longest_block"]), "OK"),
        (t("worktime.stat.earliest", date=earliest["date"]), earliest["time"], "OK"),
        (t("worktime.stat.latest", date=latest["date"]), latest["time"], "OK"),
        (t("worktime.stat.pause"),
         worktime.human_minutes(report["total_pause"]), "OK"),
        (t("worktime.stat.offhours", start=report["business_start"],
           end=report["business_end"]),
         worktime.human_minutes(night), "MEDIUM" if night else "OK"),
        (t("worktime.stat.weekend"), worktime.human_minutes(weekend),
         "MEDIUM" if weekend else "OK"),
        (t("worktime.stat.prompts"), str(report["total_prompts"]), "OK"),
    ]
    width = max(len(label) for label, _value, _tone in rows)
    for label, value, tone in rows:
        print(f"  {label:<{width}}  {paint(value, tone)}")
    print("  " + paint(t("worktime.method", gap=report["idle_gap"]), "INFO"))
    print("  " + paint(t("worktime.method.floor"), "INFO"))

    print()
    print(paint(f"── {t('worktime.section.weekday')} ──", "INFO"))
    peak = max(report["weekday_active"].values()) or 1
    for day in range(7):
        minutes = report["weekday_active"][day]
        print(f"  {t('weekday.' + str(day)):<4} {bar(minutes / peak)} "
              f"{worktime.human_minutes(minutes):>9}")

    print()
    print(paint(f"── {t('worktime.section.hours')} ──", "INFO"))
    peak = max(report["hour_active"].values()) or 1
    for hour in range(24):
        minutes = report["hour_active"][hour]
        off = hour < report["business_start"] or hour >= report["business_end"]
        label = paint(f"{hour:02d}", "MEDIUM") if off and minutes else f"{hour:02d}"
        print(f"  {label}   {bar(minutes / peak)} "
              f"{worktime.human_minutes(minutes) if minutes else '—':>9}")
    print("  " + paint(t("worktime.hours.note", start=report["business_start"],
                         end=report["business_end"]), "INFO"))

    print()
    print(paint(f"── {t('worktime.section.weeks')} ──", "INFO"))
    weeks = report["weeks"]
    peak = max(w["active"] for w in weeks) or 1
    for week in weeks:
        print(f"  {t('worktime.weeks.label', week=week['week']):<9} "
              f"{bar(week['active'] / peak)} "
              f"{worktime.human_minutes(week['active']):>9}  "
              f"{t('worktime.short.days', n=week['days'])}")
    print("  " + paint(t("worktime.weeks.note", shown=len(weeks), total=len(weeks),
                         target=f"{report['week_target']:.0f}",
                         over=report["long_weeks"]), "INFO"))

    print()
    print(paint(f"── {t('worktime.section.days')} ──", "INFO"))
    for day in report["days"]:
        marks = [t(key) for flag, key in
                 ((day["weekend"], "worktime.day.weekend"),
                  (day["off_hours"], "worktime.day.night")) if flag]
        head = f"{day['date']}  {t('weekday.' + str(day['weekday']))}"
        print(f"  {head}" + (paint("  [" + ", ".join(marks) + "]", "MEDIUM")
                             if marks else ""))
        print("      " + t("worktime.day.line", start=day["start"], end=day["end"],
                           active=worktime.human_minutes(day["active"]),
                           pause=worktime.human_minutes(day["pause"]),
                           blocks=day["blocks"]))
        print("      " + paint(
            t("worktime.day.meta", prompts=day["prompts"],
              projects=", ".join(os.path.basename(p) or p
                                 for p in day["projects"]) or "—"), "INFO"))

    print()
    print(paint(f"── {t('worktime.section.projects')} ──", "INFO"))
    for project in report["projects"]:
        print(f"  {os.path.basename(project['label']) or project['label']}")
        print("      " + t("worktime.project.line",
                           active=worktime.human_minutes(project["active"]),
                           days=project["days"]))
    print("  " + paint(t("worktime.projects.note"), "INFO"))

    print()
    print(paint(t(worktime.verdict_key(report)), "MEDIUM"))
    print(paint(t("worktime.note.clock", tab="--list-data"), "INFO"))
    return 0


def show_instructions(projects, as_json):
    # Like the check itself: the projects recorded in the baseline are always
    # included, so running from some other directory does not come up empty.
    baseline = core.load_baseline()
    targets = set(projects) | set((baseline or {}).get("project_dirs") or [])
    report = instructions.collect(targets)
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0
    print(paint(t("instructions.intro"), "INFO"))
    print()
    if not report["entries"]:
        print("  " + t("instructions.none"))
        return 0
    for entry in report["entries"]:
        tone = "CRITICAL" if entry["origin"] == "org" else \
               "HIGH" if entry["foreign"] else "OK"
        print(f"  {paint('[' + t('instructions.scope.' + entry['scope']) + ']', tone)} "
              f"{t('instructions.kind.' + entry['kind'])}: {entry['name']}")
        print("      " + entry["path"])
        print("      " + t("instructions.meta",
                           size=data.human_bytes(entry["bytes"]),
                           modified=entry["modified"], owner=entry["owner"]))
    print()
    print(t("instructions.summary", count=len(report["entries"]),
            size=data.human_bytes(report["total_bytes"])))
    if report["org_controlled"]:
        print(paint(t("instructions.org_warn", count=report["org_controlled"]),
                    "CRITICAL"))
    if report["foreign_owner"]:
        print(paint(t("instructions.foreign_warn", count=report["foreign_owner"]),
                    "HIGH"))
    return 0


def delete_data(paths, assume_yes):
    targets = []
    for path in paths:
        try:
            targets.append(data.check_deletable(path))
        except data.NotDeletable as exc:
            print(t("delete.rejected", path=path, reason=t(exc.key, **exc.params)),
                  file=sys.stderr)
    if not targets:
        return 1
    print(t("delete.list_header"))
    for real in targets:
        if os.path.isdir(real):
            stats = data.dir_stats(real)
            detail = t("delete.entry_dir", size=data.human_bytes(stats["bytes"]),
                       files=stats["files"])
        else:
            detail = data.human_bytes(os.path.getsize(real))
        print(f"  {real}  ({detail})")
    print()
    print(t("delete.note"))
    if not assume_yes:
        try:
            answer = input("\n" + t("delete.confirm") + " ").strip().lower()
        except EOFError:
            print(t("delete.needs_yes"), file=sys.stderr)
            return 1
        if answer not in {"y", "yes", "j", "ja"}:
            print(t("delete.aborted"))
            return 1
    deleted, errors = data.delete_paths(targets)
    for path, exc in errors:
        reason = t(exc.key, **exc.params) if isinstance(exc, data.NotDeletable) else exc
        print(t("delete.rejected", path=path, reason=reason), file=sys.stderr)
    print(t("delete.done", count=deleted))
    return 0 if not errors else 1


# ------------------------------------------------------------------ parser

def build_parser(lang_codes):
    p = argparse.ArgumentParser(
        prog="claude-privacy-check", description=t("cli.description"))
    p.add_argument("--language", "--lang", dest="language", choices=lang_codes,
                   help=t("cli.help.language"))
    p.add_argument("--gui", action="store_true", help=t("cli.help.gui"))
    p.add_argument("--cli", action="store_true", help=t("cli.help.cli"))
    p.add_argument("--about", action="store_true", help=t("cli.help.about"))
    # Same one-line form the other programs here answer with, so a build can be
    # identified without opening a window.
    p.add_argument("--version", action="version", help=t("cli.help.version"),
                   version=f"{about.APP_NAME} {about.APP_VERSION}")
    p.add_argument("--data", action="store_true", help=t("cli.help.data_view"))
    p.add_argument("--license", action="store_true", help=t("cli.help.license"))
    p.add_argument("--init", action="store_true", help=t("cli.help.init"))
    p.add_argument("--show", action="store_true", help=t("cli.help.show"))
    p.add_argument("--json", action="store_true", help=t("cli.help.json"))
    p.add_argument("--quiet", action="store_true", help=t("cli.help.quiet"))
    p.add_argument("--baseline", default=core.BASELINE, help=t("cli.help.baseline"))
    p.add_argument("--project", action="append", default=None,
                   metavar="DIR", help=t("cli.help.project"))
    p.add_argument("--list-data", action="store_true", help=t("cli.help.list_data"))
    p.add_argument("--worktime", action="store_true", help=t("cli.help.worktime"))
    p.add_argument("--observer", action="store_true", help=t("cli.help.observer"))
    p.add_argument("--analytics", action="store_true", help=t("cli.help.analytics"))
    p.add_argument("--telemetry", action="store_true", help=t("cli.help.telemetry"))
    p.add_argument("--instructions", action="store_true",
                   help=t("cli.help.instructions"))
    p.add_argument("--delete", action="append", metavar="PATH", default=None,
                   help=t("cli.help.delete"))
    p.add_argument("--yes", action="store_true", help=t("cli.help.yes"))
    p.add_argument("--notify", action="store_true", help=t("cli.help.notify"))
    p.add_argument("--watch-install", action="store_true",
                   help=t("cli.help.watch_install"))
    p.add_argument("--watch-uninstall", action="store_true",
                   help=t("cli.help.watch_uninstall"))
    p.add_argument("--watch-status", action="store_true", help=t("cli.help.watch_status"))
    p.add_argument("--interval", type=int, default=15, metavar="MINUTES",
                   help=t("cli.help.interval"))
    p.add_argument("--setup", action="store_true", help=t("cli.help.setup"))
    p.add_argument("--setup-uninstall", action="store_true",
                   help=t("cli.help.setup_uninstall"))
    return p


def _wants_gui(args):
    """GUI is the default; --cli and other terminal actions stay headless."""
    if args.gui:
        return True
    if args.cli:
        return False
    if args.about or args.init or args.show or args.json or args.quiet:
        return False
    if args.list_data or args.delete or args.notify:
        return False
    if args.watch_install or args.watch_uninstall or args.watch_status:
        return False
    if args.setup or args.setup_uninstall:
        return False
    if args.project is not None:
        return False
    # bare launch, --language and the view flags (--data / --license / --worktime
    # / --observer / --analytics / --telemetry / --instructions) → window
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    codes = [c for c, _ in available_languages()]

    # Language must be settled before the parser is built, because argparse
    # help texts are translated too.
    override = None
    for i, arg in enumerate(argv):
        if arg in ("--language", "--lang") and i + 1 < len(argv):
            override = argv[i + 1]
        elif arg.startswith(("--language=", "--lang=")):
            override = arg.split("=", 1)[1]
    apply_startup_language(override if override in codes else None)

    args = build_parser(codes).parse_args(argv)

    moved = core.migrate_legacy_baseline()
    if moved:
        print(t("baseline.migrated", path=moved), file=sys.stderr)
    if args.language:
        save_preference(current_language())

    if args.about:
        print(about.build_about_text())
        return 0
    if args.setup_uninstall:
        from . import install as app_install
        app_install.uninstall(progress=print)
        return 0
    if args.setup:
        from . import install as app_install
        app_install.ensure(progress=print, force=True)
        return 0
    if _wants_gui(args):
        from .gui import run as run_gui
        return run_gui("data" if args.data else
                       "license" if args.license else
                       "worktime" if args.worktime else
                       "observer" if args.observer else
                       "analytics" if args.analytics else
                       "telemetry" if args.telemetry else
                       "instructions" if args.instructions else "check")
    if args.license:
        return show_license(args.json)
    if args.list_data:
        return list_data(args.json)
    if args.worktime:
        return show_worktime(args.json)
    if args.observer:
        return show_observer(args.json)
    if args.analytics:
        return show_analytics(args.json)
    if args.telemetry:
        return show_telemetry(args.json)
    if args.instructions:
        return show_instructions(
            [os.path.abspath(x) for x in (args.project or [os.getcwd()])], args.json)
    if args.delete:
        return delete_data(args.delete, args.yes)
    if args.notify:
        return watch.notify_check()
    if args.watch_install:
        return watch.install(max(1, args.interval))
    if args.watch_uninstall:
        return watch.uninstall()
    if args.watch_status:
        return watch.status()

    projects = [os.path.abspath(p) for p in
                (args.project if args.project is not None else [os.getcwd()])]

    if args.init:
        snapshot = core.collect(projects)
        findings = core.assess(snapshot)
        core.save_baseline(snapshot, args.baseline)
        if args.json:
            print(json.dumps({"baseline": args.baseline, "findings": findings},
                             indent=2, ensure_ascii=False))
        else:
            print(t("baseline.written", path=args.baseline))
            print(t("baseline.projects", projects=", ".join(projects) or "—"))
            print_findings(findings, args.quiet)
        return 2 if any(f["severity"] == "CRITICAL" for f in findings) else 0

    if args.show:
        snapshot = core.collect(projects)
        result = {"snapshot": snapshot, "findings": core.assess(snapshot),
                  "changes": None, "baseline_time": None}
    else:
        result = core.run_check(args.baseline, projects)
        if result["baseline_time"] is None and not args.json:
            print(t("baseline.missing", path=args.baseline))
            print(t("baseline.hint_init") + "\n")

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print_findings(result["findings"], args.quiet)
        if result["changes"] is not None:
            print()
            print_changes(result["changes"], args.quiet, result["baseline_time"])
        print_history(result["snapshot"].get("local_history") or {})
        print()
        print(paint(t("caveat.server_export"), "INFO"))

    if args.show:
        return 2 if any(f["severity"] == "CRITICAL" for f in result["findings"]) else 0
    return core.exit_code(result)
