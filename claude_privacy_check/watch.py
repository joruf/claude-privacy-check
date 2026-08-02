"""Continuous monitoring: desktop notification and systemd user units.

Event-driven rather than polled. The watched files are written by the server at
session start -- that is, right before the first prompt is typed. A path unit
reacts within about a second, which is in time; a coarse poll may not be. The
timer is a safety net for anything inotify cannot see.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import core
from .icons import ICON_NAME, icon_for_notify
from .i18n import t

UNIT_DIR = os.path.expanduser("~/.config/systemd/user")


def entry_command():
    """How to invoke this tool from a unit file.

    Prefers the installed console script; falls back to run.py in a checkout.
    """
    script = shutil.which("claude-privacy-check")
    if script:
        return script
    return f"{sys.executable} {os.path.join(core.APP_DIR, 'run.py')}"

# The marker deliberately lives in XDG_RUNTIME_DIR (/run/user/<uid>): the system
# clears that directory at logout. "Already notified" therefore applies to the
# running session only -- a finding that persists warns again after the next
# login, but never twice within one session.
NOTIFY_STATE = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
    "claude-privacy-check", "notified.json")

# Only these files change when an organisation rolls something out. The .path
# unit listens on them -- event-driven instead of on a clock.
WATCH_FILES = ["~/.claude/remote-settings.json", "~/.claude/policy-limits.json",
               "~/.claude/settings.json", "~/.claude/settings.local.json"]

SERVICE_UNIT = """\
[Unit]
Description=Claude Privacy Check — inspect configuration, warn on monitoring

[Service]
Type=oneshot
ExecStart={command} --notify
# 1 = deviation, 2 = critical finding. Both are valid results, not failures --
# without this the unit stays permanently in the "failed" state.
SuccessExitStatus=0 1 2
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
# %t is XDG_RUNTIME_DIR -- home of the session marker that disappears at logout,
# so the warning appears exactly once per session.
Environment=XDG_RUNTIME_DIR=%t
"""

PATH_UNIT = """\
[Unit]
Description=Watches the Claude Code configuration files

[Path]
{paths}
Unit=claude-privacy-check.service

[Install]
WantedBy=default.target
"""

TIMER_UNIT = """\
[Unit]
Description=Claude Privacy Check on a regular schedule

[Timer]
OnBootSec=2min
OnUnitActiveSec={interval}
Persistent=true
Unit=claude-privacy-check.service

[Install]
WantedBy=timers.target
"""


# ------------------------------------------------------------- notification

def notify_check():
    """Check, and warn at most once per session about monitoring.

    Only signals that actually point at capture are reported: every finding of
    the assessment, plus deviations at HIGH or CRITICAL. Harmless deviations
    (own settings, permissions, plugins) stay silent but remain visible in the
    CLI and the window.
    """
    result = core.run_check()
    code = core.exit_code(result)

    findings = result["findings"]
    alerts = [c for c in (result["changes"] or [])
              if c["severity"] in ("HIGH", "CRITICAL")]
    critical = (any(f["severity"] == "CRITICAL" for f in findings)
                or any(c["severity"] == "CRITICAL" for c in alerts))

    seen = _load_notified()
    if not findings and not alerts:
        if seen:                       # was flagged, is no longer
            _notify(t("notify.clear.title"), t("notify.clear.body"), "normal")
            _save_notified([])
        return code

    fingerprint = json.dumps(
        {"findings": sorted((f["severity"], f["key"], str(f["params"]))
                            for f in findings),
         "alerts": sorted((c["severity"], c["path"], str(c["after"])) for c in alerts)},
        sort_keys=True)
    if fingerprint in seen:
        print(t("notify.suppressed"))
        return code

    lines = [f"• {core.finding_title(f)}" for f in findings[:3]]
    lines += [f"• {c['path']}" for c in alerts[:3]]
    extra = len(findings) + len(alerts) - len(lines)
    if extra > 0:
        lines.append(t("notify.more", count=extra))
    title = t("notify.critical.title") if critical else t("notify.changed.title")
    _notify(title, "\n".join(lines), "critical" if critical else "normal")
    _save_notified(seen + [fingerprint])
    return code


def _load_notified():
    try:
        with open(NOTIFY_STATE, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_notified(fingerprints):
    try:
        os.makedirs(os.path.dirname(NOTIFY_STATE), exist_ok=True)
        with open(NOTIFY_STATE, "w", encoding="utf-8") as fh:
            json.dump(fingerprints[-20:], fh)
    except OSError:
        pass


def _notify(title, body, urgency):
    if not shutil.which("notify-send"):
        print(f"{title}\n{body}")
        return
    icon = icon_for_notify() or ICON_NAME
    cmd = ["notify-send", "--app-name=Claude Privacy Check",
           f"--urgency={urgency}", f"--icon={icon}"]
    if urgency == "critical":
        cmd.append("--expire-time=0")       # stays until dismissed
    subprocess.run(cmd + [title, body], check=False)


# ----------------------------------------------------------- systemd units

def _systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True)


def install(interval_minutes):
    os.makedirs(UNIT_DIR, exist_ok=True)
    paths = "\n".join(
        f"PathModified=%h/{os.path.expanduser(p)[len(core.HOME) + 1:]}"
        for p in WATCH_FILES)
    units = {
        "claude-privacy-check.service": SERVICE_UNIT.format(command=entry_command()),
        "claude-privacy-check.path": PATH_UNIT.format(paths=paths),
        "claude-privacy-check.timer": TIMER_UNIT.format(interval=f"{interval_minutes}min"),
    }
    for name, content in units.items():
        target = os.path.join(UNIT_DIR, name)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(t("watch.written", path=target))

    _systemctl("daemon-reload")
    for unit in ("claude-privacy-check.path", "claude-privacy-check.timer"):
        res = _systemctl("enable", "--now", unit)
        print(f"{unit}: "
              + (t("watch.active") if res.returncode == 0
                 else t("watch.error", error=res.stderr.strip())))
    print()
    print(t("watch.installed", files=len(WATCH_FILES), interval=interval_minutes))
    return 0


def uninstall():
    for unit in ("claude-privacy-check.path", "claude-privacy-check.timer"):
        _systemctl("disable", "--now", unit)
    removed = 0
    for name in ("claude-privacy-check.service", "claude-privacy-check.path",
                 "claude-privacy-check.timer"):
        path = os.path.join(UNIT_DIR, name)
        if os.path.exists(path):
            os.remove(path)
            removed += 1
            print(t("watch.removed", path=path))
    _systemctl("daemon-reload")
    print(t("watch.removed_count", count=removed) if removed
          else t("watch.not_installed"))
    return 0


def status():
    installed = [n for n in ("claude-privacy-check.path", "claude-privacy-check.timer")
                 if os.path.exists(os.path.join(UNIT_DIR, n))]
    if not installed:
        print(t("watch.not_installed"))
        print(t("watch.hint_install"))
        return 1
    for unit in installed:
        res = _systemctl("is-active", unit)
        print(f"  {unit}: {res.stdout.strip() or res.stderr.strip()}")
    print()
    print(t("watch.watched_files"))
    for path in WATCH_FILES:
        mark = "✓" if os.path.exists(os.path.expanduser(path)) else "·"
        print(f"  {mark} {path}")
    res = _systemctl("list-timers", "claude-privacy-check.timer", "--no-pager")
    print()
    print(res.stdout.strip() or t("watch.no_timer"))
    return 0
