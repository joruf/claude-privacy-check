"""Detection core: collect, assess, compare.

Every configuration surface through which an organisation could capture prompt
content, tool calls or metadata is read here, turned into a snapshot, and
compared against a baseline the user recorded at a point they trusted.

Findings carry a translation key plus parameters rather than finished text, so
the same result object renders in whatever language the output layer uses.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
from datetime import datetime, timezone

from .i18n import t

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
APP_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _xdg_baseline():
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    return os.path.join(base, "claude-privacy-check", "baseline.json")


# Never inside the checkout. The baseline holds the account e-mail, the
# organisation id and every watched path -- personal state that has no business
# sitting in a directory people clone, and that an installed package could not
# write to anyway.
BASELINE = _xdg_baseline()
LEGACY_BASELINE = os.path.join(APP_DIR, "data", "baseline.json")


def migrate_legacy_baseline():
    """Move a baseline left inside the checkout by an earlier version.

    Returns the destination when something was moved, otherwise None.
    """
    if not os.path.exists(LEGACY_BASELINE):
        return None
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    if os.path.exists(BASELINE):
        os.remove(LEGACY_BASELINE)       # superseded by the one outside
    else:
        shutil.move(LEGACY_BASELINE, BASELINE)
        os.chmod(BASELINE, 0o600)
    try:
        os.rmdir(os.path.dirname(LEGACY_BASELINE))
    except OSError:
        pass                             # other files in there, leave it
    return BASELINE

# --------------------------------------------------------------------------
# Configuration surfaces
# --------------------------------------------------------------------------

# System-wide settings files. Always recorded -- even when absent, because
# their appearance is itself the signal.
SYSTEM_FILES = [
    "/etc/claude-code/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-settings.json",  # macOS
    "~/.claude/remote-settings.json",   # pushed from the admin console
    "~/.claude/policy-limits.json",     # server policy incl. monitoring_notice
    "~/.claude/settings.json",
    "~/.claude/settings.local.json",
]
SYSTEM_GLOBS = [
    "/etc/claude-code/managed-settings.d/*.json",
    "/Library/Application Support/ClaudeCode/managed-settings.d/*.json",
]
# Project files. Recorded only when present -- otherwise every new working
# directory would produce phantom changes in the diff.
PROJECT_FILES = [".claude/settings.json", ".claude/settings.local.json", ".mcp.json"]

# Security-relevant environment variables. Deliberately a whitelist rather than
# prefix matching, so volatile session variables do not flood the diff.
WATCH_ENV_EXACT = {
    "CLAUDE_CODE_ENABLE_TELEMETRY", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
    "ENABLE_ENHANCED_TELEMETRY_BETA", "CLAUDE_CODE_PROPAGATE_TRACEPARENT",
    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN",
    "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
}
WATCH_ENV_PREFIX = ("OTEL_", "CLAUDE_CODE_OTEL_")

# Values that may be secrets -- store presence and a digest only.
SECRET_ENV = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
              "OTEL_EXPORTER_OTLP_HEADERS", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE"}

# Content logging: these variables capture actual prompts and responses.
CONTENT_LOGGING = [
    "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_ASSISTANT_RESPONSES",
    "OTEL_LOG_RAW_API_BODIES", "OTEL_LOG_TOOL_CONTENT", "OTEL_LOG_TOOL_DETAILS",
]

# Hook events able to capture prompt or tool content.
CONTENT_HOOKS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "Stop"}

SHELL_PROFILES = [
    "~/.bashrc", "~/.bash_profile", "~/.profile", "~/.zshrc", "~/.zshenv",
    "/etc/environment", "/etc/profile",
]
SHELL_PROFILE_GLOBS = ["/etc/profile.d/*.sh", "~/.config/environment.d/*.conf"]
PROFILE_PATTERN = re.compile(
    r"OTEL_|CLAUDE_CODE_ENABLE_TELEMETRY|ANTHROPIC_BASE_URL|ANTHROPIC_AUTH_TOKEN|"
    r"NODE_EXTRA_CA_CERTS|CLAUDE_CODE_CLIENT_", re.I)

SEV_ORDER = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "INFO": 0}

# Claude.ai subscription / organisation type → translation key for the plan name.
PLAN_KEYS = {
    "pro": "plan.pro", "claude_pro": "plan.pro",
    "max": "plan.max", "claude_max": "plan.max",
    "team": "plan.team", "claude_team": "plan.team",
    "enterprise": "plan.enterprise", "claude_enterprise": "plan.enterprise",
}
PAID_SUBSCRIPTIONS = frozenset({"pro", "max", "team", "enterprise"})

# Fields that change on every run or merely describe the scope of collection --
# never a finding. local_history grows with every session and is display-only.
DIFF_IGNORE_ROOTS = {"collected_at", "project_dirs", "local_history"}


def digest(value):
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()[:16]


def truthy(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------

def read_settings_file(path, skip_if_missing=False):
    """Read a settings file including owner and mode.

    ``skip_if_missing`` returns None instead of {"exists": False} -- used for
    project files, whose absence carries no signal.
    """
    real = os.path.expanduser(path)
    if not os.path.exists(real):
        return None if skip_if_missing else {"exists": False}
    entry = {"exists": True}
    try:
        st = os.stat(real)
        entry["mode"] = stat.filemode(st.st_mode)
        try:
            entry["owner"] = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            entry["owner"] = str(st.st_uid)
    except OSError as exc:
        entry["stat_error"] = str(exc)
    try:
        with open(real, encoding="utf-8") as fh:
            entry["content"] = json.load(fh)
    except json.JSONDecodeError as exc:
        entry["parse_error"] = str(exc)
    except OSError as exc:
        entry["read_error"] = str(exc)
    return entry


def collect_env_from_proc():
    """Environment of running Claude Code processes, read from /proc.

    More reliable than os.environ: Claude Code is often started from an IDE and
    can be handed variables that are invisible in the calling shell.
    """
    found = {}
    for pid_dir in glob.glob("/proc/[0-9]*"):
        try:
            with open(os.path.join(pid_dir, "comm"), encoding="utf-8") as fh:
                comm = fh.read().strip()
            with open(os.path.join(pid_dir, "cmdline"), "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace")
            if "claude" not in comm and "claude" not in cmdline:
                continue
            with open(os.path.join(pid_dir, "environ"), "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except (OSError, PermissionError):
            continue
        for item in raw.split("\0"):
            if "=" not in item:
                continue
            key, _, value = item.partition("=")
            if key in WATCH_ENV_EXACT or key.startswith(WATCH_ENV_PREFIX):
                found[key] = value
    return found


def collect_env():
    env = {}
    for key, value in os.environ.items():
        if key in WATCH_ENV_EXACT or key.startswith(WATCH_ENV_PREFIX):
            env[key] = value
    env.update(collect_env_from_proc())
    return {k: (digest(v) if k in SECRET_ENV else v) for k, v in sorted(env.items())}


def collect_profiles():
    hits = {}
    targets = [os.path.expanduser(p) for p in SHELL_PROFILES]
    for pattern in SHELL_PROFILE_GLOBS:
        targets.extend(sorted(glob.glob(os.path.expanduser(pattern))))
    for path in targets:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = [ln.strip() for ln in fh
                         if PROFILE_PATTERN.search(ln) and not ln.strip().startswith("#")]
        except OSError:
            continue
        if lines:
            hits[path] = sorted(lines)
    return hits


def collect_account_and_mcp():
    """Organisation membership and configured MCP servers from ~/.claude.json."""
    account, mcp = {}, {}
    path = os.path.join(HOME, ".claude.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"error": "unreadable"}, {}
    oauth = data.get("oauthAccount") or {}
    for key in ("organizationUuid", "organizationName", "organizationType",
                "organizationRole", "workspaceRole", "seatTier", "emailAddress",
                "billingType", "hasExtraUsageEnabled", "accountUuid", "displayName"):
        if key in oauth:
            account[key] = oauth[key]
    if data.get("mcpServers"):
        mcp["<global>"] = sorted(data["mcpServers"].keys())
    for project, cfg in (data.get("projects") or {}).items():
        servers = (cfg or {}).get("mcpServers") or {}
        if servers:
            mcp[project] = sorted(servers.keys())
    return account, mcp


def collect_auth(env=None):
    """How Claude Code authenticates on this machine — no token values stored.

    Claude Code needs one of: a Claude.ai OAuth login (Pro/Max/Team/Enterprise),
    an Anthropic Console API key, CLAUDE_CODE_OAUTH_TOKEN, or cloud-provider auth
    (Bedrock / Vertex). Install-only state is not a licence.
    """
    env = env if env is not None else collect_env()
    auth = {
        "credentials_file": False,
        "has_access_token": False,
        "has_refresh_token": False,
        "token_state": "absent",   # absent | valid | expired | unknown
        "subscription": None,      # pro | max | team | enterprise
        "rate_limit_tier": None,
        "method": "none",          # oauth | api_key | oauth_env | bedrock | vertex | none
    }

    path = os.path.join(CLAUDE_DIR, ".credentials.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        auth["credentials_file"] = True
        oauth = data.get("claudeAiOauth") or {}
        if oauth.get("accessToken"):
            auth["has_access_token"] = True
        if oauth.get("refreshToken"):
            auth["has_refresh_token"] = True
        sub = oauth.get("subscriptionType")
        if isinstance(sub, str) and sub:
            auth["subscription"] = sub.lower()
        if oauth.get("rateLimitTier"):
            auth["rate_limit_tier"] = oauth["rateLimitTier"]
        expires = oauth.get("expiresAt")
        if auth["has_access_token"]:
            if auth["has_refresh_token"]:
                auth["token_state"] = "valid"
            elif isinstance(expires, (int, float)):
                # expiresAt is milliseconds since epoch
                now_ms = datetime.now(timezone.utc).timestamp() * 1000
                auth["token_state"] = "valid" if expires > now_ms else "expired"
            else:
                auth["token_state"] = "unknown"
        if auth["has_access_token"] or auth["has_refresh_token"]:
            auth["method"] = "oauth"
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        auth["credentials_error"] = True

    if auth["method"] == "none":
        if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            auth["method"] = "oauth_env"
            auth["token_state"] = "valid"
        elif env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
            auth["method"] = "api_key"
            auth["token_state"] = "valid"
        elif truthy(env.get("CLAUDE_CODE_USE_BEDROCK", "")):
            auth["method"] = "bedrock"
            auth["token_state"] = "valid"
        elif truthy(env.get("CLAUDE_CODE_USE_VERTEX", "")):
            auth["method"] = "vertex"
            auth["token_state"] = "valid"

    return auth


def plan_key(account=None, auth=None):
    """Translation key for the paid plan, or None if unknown / not a Claude.ai plan."""
    auth = auth or {}
    account = account or {}
    for raw in (auth.get("subscription"), account.get("organizationType")):
        if raw and raw in PLAN_KEYS:
            return PLAN_KEYS[raw]
    return None


def plan_label(account=None, auth=None):
    """Human-readable plan name for headers."""
    key = plan_key(account, auth)
    if key:
        return t(key)
    method = (auth or {}).get("method")
    if method == "api_key":
        return t("plan.api_key")
    if method == "oauth_env":
        return t("plan.oauth_env")
    if method == "bedrock":
        return t("plan.bedrock")
    if method == "vertex":
        return t("plan.vertex")
    if method == "oauth":
        return t("plan.oauth_unknown")
    return t("plan.none")


def collect_local_history():
    """Extent of the local plaintext transcripts in ~/.claude/projects/.

    Display only: quantifies how far back a hook injected later could reach.
    The files are stored unencrypted.
    """
    count, total, oldest = 0, 0, None
    for root, _dirs, files in os.walk(PROJECTS_DIR):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            total += st.st_size
            if oldest is None or st.st_mtime < oldest:
                oldest = st.st_mtime
    return {
        "transcript_files": count,
        "megabytes": round(total / 1024 / 1024, 1),
        "oldest": (datetime.fromtimestamp(oldest, timezone.utc).date().isoformat()
                   if oldest else None),
    }


def collect(project_dirs=()):
    """Record the current state. project_dirs: extra projects to inspect."""
    account, mcp = collect_account_and_mcp()
    env = collect_env()
    auth = collect_auth(env)
    # Prefer organisationType when credentials lack subscriptionType
    if not auth.get("subscription"):
        org = account.get("organizationType") or ""
        if org.startswith("claude_"):
            auth["subscription"] = org[len("claude_"):]
        elif org in PAID_SUBSCRIPTIONS:
            auth["subscription"] = org
    surfaces = {}
    for path in SYSTEM_FILES:
        surfaces[f"file:{path}"] = read_settings_file(path)
    for pattern in SYSTEM_GLOBS:
        for path in sorted(glob.glob(pattern)):
            surfaces[f"file:{path}"] = read_settings_file(path)
    for project in sorted(set(project_dirs)):
        for rel in PROJECT_FILES:
            path = os.path.join(project, rel)
            entry = read_settings_file(path, skip_if_missing=True)
            if entry is not None:
                surfaces[f"file:{path}"] = entry

    plugin_dir = os.path.join(CLAUDE_DIR, "plugins")
    plugins = sorted(os.listdir(plugin_dir)) if os.path.isdir(plugin_dir) else []

    return {
        "version": 4,
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_dirs": sorted(set(project_dirs)),
        "surfaces": surfaces,
        "env": env,
        "shell_profiles": collect_profiles(),
        "account": account,
        "auth": auth,
        "mcp_servers": mcp,
        "plugins": plugins,
        "local_history": collect_local_history(),
    }


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------

def iter_settings_contents(snapshot):
    for name, entry in snapshot["surfaces"].items():
        if isinstance(entry, dict) and isinstance(entry.get("content"), dict):
            yield name, entry["content"]


def assess(snapshot):
    """Findings as (severity, translation key, params) -- rendered by the caller."""
    findings = []

    def add(sev, key, **params):
        findings.append({"severity": sev, "key": key, "params": params})

    env = snapshot["env"]

    # Content logging via OpenTelemetry
    for var in CONTENT_LOGGING:
        if var in env and truthy(env[var]):
            add("CRITICAL", "finding.content_logging", var=var)
    if truthy(env.get("CLAUDE_CODE_ENABLE_TELEMETRY", "")):
        add("HIGH", "finding.telemetry_on",
            endpoint=(env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
                      or env.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
                      or t("value.unknown")))

    # Redirected API traffic -- sees everything in the clear
    if env.get("ANTHROPIC_BASE_URL"):
        add("CRITICAL", "finding.base_url", url=env["ANTHROPIC_BASE_URL"])
    if env.get("NODE_EXTRA_CA_CERTS"):
        add("CRITICAL", "finding.extra_ca", path=env["NODE_EXTRA_CA_CERTS"])
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if env.get(var):
            add("MEDIUM", "finding.proxy", var=var, value=env[var])
    if env.get("NODE_OPTIONS"):
        add("MEDIUM", "finding.node_options", value=env["NODE_OPTIONS"])

    # Server-delivered policy
    policy = snapshot["surfaces"].get("file:~/.claude/policy-limits.json") or {}
    content = policy.get("content") or {}
    if content.get("monitoring_notice"):
        add("CRITICAL", "finding.monitoring_notice", notice=content["monitoring_notice"])
    if content.get("compliance_taints"):
        add("HIGH", "finding.compliance_taints", taints=content["compliance_taints"])

    # Settings pushed from the admin console
    remote = snapshot["surfaces"].get("file:~/.claude/remote-settings.json") or {}
    if remote.get("exists") and remote.get("content"):
        add("HIGH", "finding.remote_settings",
            content=json.dumps(remote["content"], ensure_ascii=False)[:400])

    # Managed settings (MDM / root)
    for name, entry in snapshot["surfaces"].items():
        if "managed-settings" in name and (entry or {}).get("exists"):
            add("HIGH", "finding.managed_settings", file=name[5:])

    # Hooks and helpers in every settings file
    for name, cfg in iter_settings_contents(snapshot):
        for event, spec in (cfg.get("hooks") or {}).items():
            add("CRITICAL" if event in CONTENT_HOOKS else "HIGH", "finding.hook",
                event=event, file=name[5:],
                spec=json.dumps(spec, ensure_ascii=False)[:300])
        for key, sev in (("apiKeyHelper", "MEDIUM"), ("otelHeadersHelper", "HIGH"),
                         ("statusLine", "MEDIUM"), ("claudeMd", "MEDIUM")):
            if cfg.get(key):
                add(sev, "finding.helper", key=key, file=name[5:],
                    value=json.dumps(cfg[key], ensure_ascii=False)[:300])
        for var, value in (cfg.get("env") or {}).items():
            if var in WATCH_ENV_EXACT or var.startswith(WATCH_ENV_PREFIX):
                add("CRITICAL" if var in CONTENT_LOGGING and truthy(value) else "HIGH",
                    "finding.env_in_settings", var=var, file=name[5:], value=value)
        # Extended local retention widens the window a later hook could harvest.
        days = cfg.get("cleanupPeriodDays")
        if isinstance(days, (int, float)) and days > 30:
            add("MEDIUM", "finding.cleanup_period", days=days, file=name[5:])

    # Foreign owner on user settings
    me = pwd.getpwuid(os.getuid()).pw_name
    for name, entry in snapshot["surfaces"].items():
        if name.startswith("file:~/") and (entry or {}).get("owner") not in (None, me):
            add("HIGH", "finding.foreign_owner", file=name[5:],
                owner=entry["owner"], me=me)

    # Shell profiles
    for path, lines in snapshot["shell_profiles"].items():
        add("HIGH", "finding.profile_telemetry", path=path,
            lines="; ".join(lines)[:400])

    # Licence / payment model: Claude Code needs a paid Claude.ai seat, an API
    # key, or cloud-provider auth. Tokens themselves are never stored.
    auth = snapshot.get("auth") or {}
    account = snapshot.get("account") or {}
    method = auth.get("method") or "none"
    subscription = auth.get("subscription")
    org_type = account.get("organizationType")

    if method == "none":
        add("HIGH", "finding.no_license")
    elif auth.get("token_state") == "expired":
        add("MEDIUM", "finding.auth_expired")
    elif method == "api_key":
        add("INFO", "finding.auth_api_key")
    elif method in ("bedrock", "vertex"):
        add("INFO", "finding.auth_cloud", provider=method)
    elif method == "oauth_env":
        add("INFO", "finding.auth_oauth_env")
    elif subscription in PAID_SUBSCRIPTIONS:
        add("INFO", "finding.subscription", plan=t(PLAN_KEYS[subscription]))
    elif method == "oauth":
        add("INFO", "finding.auth_oauth_unknown")

    # Enterprise unlocks the Compliance API for the organisation
    if org_type == "claude_enterprise" or subscription == "enterprise":
        add("MEDIUM", "finding.enterprise_plan")

    findings.sort(key=lambda f: -SEV_ORDER[f["severity"]])
    return findings


def finding_title(finding):
    return t(f"{finding['key']}.title", **finding["params"])


def finding_detail(finding):
    return t(f"{finding['key']}.detail", **finding["params"])


# --------------------------------------------------------------------------
# Comparison against the baseline
# --------------------------------------------------------------------------

def flatten(obj, prefix=""):
    """Nested structure to flat path/value pairs."""
    out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        try:
            rendered = json.dumps(sorted(obj), ensure_ascii=False)
        except TypeError:
            rendered = json.dumps(obj, ensure_ascii=False)
        out[prefix] = rendered
    else:
        out[prefix] = obj
    return out


def path_severity(path):
    lowered = path.lower()
    if any(var.lower() in lowered for var in CONTENT_LOGGING):
        return "CRITICAL"
    if "monitoring_notice" in lowered or "compliance_taints" in lowered:
        return "CRITICAL"
    if "anthropic_base_url" in lowered or "node_extra_ca_certs" in lowered:
        return "CRITICAL"
    if ".hooks." in lowered or lowered.endswith(".hooks"):
        return "CRITICAL"
    if "managed-settings" in lowered or "remote-settings" in lowered:
        return "HIGH"
    if "shell_profiles" in lowered or "otel" in lowered or "telemetry" in lowered:
        return "HIGH"
    if "helper" in lowered or lowered.startswith("mcp_servers") \
            or lowered.startswith("account") or lowered.startswith("plugins"):
        return "MEDIUM"
    if ".owner" in lowered or ".mode" in lowered:
        return "MEDIUM"
    if "permissions" in lowered:
        return "INFO"
    return "MEDIUM"


MISSING = "∅"     # marks "key absent on this side" in a diff


def diff(old, new):
    flat_old = {k: v for k, v in flatten(old).items()
                if k.split(".")[0] not in DIFF_IGNORE_ROOTS}
    flat_new = {k: v for k, v in flatten(new).items()
                if k.split(".")[0] not in DIFF_IGNORE_ROOTS}
    changes = []
    for key in sorted(set(flat_old) | set(flat_new)):
        before, after = flat_old.get(key, MISSING), flat_new.get(key, MISSING)
        if before != after:
            changes.append({"path": key, "before": before, "after": after,
                            "severity": path_severity(key)})
    changes.sort(key=lambda c: -SEV_ORDER[c["severity"]])
    return changes


# --------------------------------------------------------------------------
# Shared flow for CLI and GUI
# --------------------------------------------------------------------------

def load_baseline(path=BASELINE):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_baseline(snapshot, path=BASELINE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.chmod(path, 0o600)


def run_check(baseline_path=BASELINE, extra_projects=()):
    """Collect, assess and compare. Returns the complete result.

    Project directories stored in the baseline are always re-checked, so a run
    from a different working directory does not report phantom changes.
    """
    previous = load_baseline(baseline_path)
    projects = set(extra_projects)
    if previous:
        projects.update(previous.get("project_dirs") or [])
    snapshot = collect(projects)
    findings = assess(snapshot)
    changes = diff(previous, snapshot) if previous else None
    critical = (any(f["severity"] == "CRITICAL" for f in findings)
                or bool(changes and any(c["severity"] == "CRITICAL" for c in changes)))
    if critical:
        status = "CRITICAL"
    elif changes:
        status = "CHANGED"
    elif previous is None:
        status = "NO_BASELINE"
    else:
        status = "OK"
    return {
        "snapshot": snapshot,
        "findings": findings,
        "changes": changes,
        "status": status,
        "baseline_path": baseline_path,
        "baseline_time": previous.get("collected_at") if previous else None,
    }


def exit_code(result):
    if result["status"] == "CRITICAL":
        return 2
    return 1 if result["changes"] else 0
