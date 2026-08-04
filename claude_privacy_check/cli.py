"""Command line interface: argument parsing and terminal output."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import about, core, data, instructions, observer, watch, worktime
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
    # / --observer / --instructions) → window
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
                       "instructions" if args.instructions else "check")
    if args.license:
        return show_license(args.json)
    if args.list_data:
        return list_data(args.json)
    if args.worktime:
        return show_worktime(args.json)
    if args.observer:
        return show_observer(args.json)
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
