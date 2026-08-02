"""Command line interface: argument parsing and terminal output."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import about, core, data, instructions, observer, watch
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


def show_observer(as_json):
    report = observer.build_report()
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return 0

    account = report["account"]
    plan = {"claude_team": t("plan.team"), "claude_enterprise": t("plan.enterprise")}.get(
        account.get("organizationType"), account.get("organizationType", "—"))
    print(paint(t("observer.intro"), "INFO"))
    print()
    print(paint(f"── {t('observer.section.identity')} ──", "INFO"))
    print("  " + t("observer.identity.line", org=account.get("organizationName", "—"),
                   plan=plan, role=account.get("organizationRole", "—"),
                   email=account.get("emailAddress", "—")))
    print("  " + paint(t("observer.identity.note"), "INFO"))

    print()
    print(paint(f"── {t('observer.section.projects')} ──", "INFO"))
    for project in report["projects"]:
        print(f"  {project['label']}")
        print("      " + t("data.project_line", sessions=project["sessions"],
                           size=data.human_bytes(project["bytes"]),
                           oldest=project["oldest"], newest=project["newest"]))
    print("  " + paint(t("observer.projects.note"), "INFO"))

    print()
    print(paint(f"── {t('observer.section.pattern')} ──", "INFO"))
    print("  " + t("observer.pattern.line", sessions=report["sessions"],
                   days=report["active_days"],
                   size=data.human_bytes(report["bytes"])))
    print("  " + t("observer.pattern.hours", start=observer.BUSINESS_START,
                   end=observer.BUSINESS_END, count=report["off_hours"]))
    print("  " + t("observer.pattern.weekend", count=report["weekend"]))
    print("  " + paint(t("observer.pattern.note"), "INFO"))

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
    p.add_argument("--about", action="store_true", help=t("cli.help.about"))
    p.add_argument("--data", action="store_true", help=t("cli.help.data_view"))
    p.add_argument("--init", action="store_true", help=t("cli.help.init"))
    p.add_argument("--show", action="store_true", help=t("cli.help.show"))
    p.add_argument("--json", action="store_true", help=t("cli.help.json"))
    p.add_argument("--quiet", action="store_true", help=t("cli.help.quiet"))
    p.add_argument("--baseline", default=core.BASELINE, help=t("cli.help.baseline"))
    p.add_argument("--project", action="append", default=None,
                   metavar="DIR", help=t("cli.help.project"))
    p.add_argument("--list-data", action="store_true", help=t("cli.help.list_data"))
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
    return p


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
    if args.gui:
        from .gui import run as run_gui
        return run_gui("data" if args.data else
                       "observer" if args.observer else
                       "instructions" if args.instructions else "check")
    if args.list_data:
        return list_data(args.json)
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
