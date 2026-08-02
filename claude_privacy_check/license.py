"""Licence / subscription details from local Claude Code state.

Reads account metadata and credential *metadata* only — never access or
refresh tokens. Built for the Licence tab and `run.py --license`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import core
from .i18n import t

# Human-facing rows: (field_id, translation key for the label).
ACCOUNT_FIELDS = (
    ("emailAddress", "license.field.email"),
    ("displayName", "license.field.display_name"),
    ("accountUuid", "license.field.account_uuid"),
    ("organizationName", "license.field.org_name"),
    ("organizationUuid", "license.field.org_uuid"),
    ("organizationType", "license.field.org_type"),
    ("organizationRole", "license.field.org_role"),
    ("workspaceRole", "license.field.workspace_role"),
    ("seatTier", "license.field.seat_tier"),
    ("billingType", "license.field.billing_type"),
    ("hasExtraUsageEnabled", "license.field.extra_usage"),
)

AUTH_FIELDS = (
    ("method", "license.field.method"),
    ("subscription", "license.field.subscription"),
    ("plan", "license.field.plan"),
    ("rate_limit_tier", "license.field.rate_limit"),
    ("token_state", "license.field.token_state"),
    ("has_access_token", "license.field.access_token"),
    ("has_refresh_token", "license.field.refresh_token"),
    ("expires_at", "license.field.expires_at"),
    ("scopes", "license.field.scopes"),
    ("credentials_file", "license.field.credentials_file"),
    ("credentials_path", "license.field.credentials_path"),
)

ENV_FIELDS = (
    ("ANTHROPIC_API_KEY", "license.field.env_api_key"),
    ("ANTHROPIC_AUTH_TOKEN", "license.field.env_auth_token"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "license.field.env_oauth_token"),
    ("CLAUDE_CODE_USE_BEDROCK", "license.field.env_bedrock"),
    ("CLAUDE_CODE_USE_VERTEX", "license.field.env_vertex"),
)

BILLING_LABELS = {
    "stripe_subscription": "license.billing.stripe",
    "stripe_subscription_contracted": "license.billing.stripe_contracted",
    "apple_subscription": "license.billing.apple",
    "google_play_subscription": "license.billing.google_play",
}

METHOD_LABELS = {
    "oauth": "license.method.oauth",
    "api_key": "license.method.api_key",
    "oauth_env": "license.method.oauth_env",
    "bedrock": "license.method.bedrock",
    "vertex": "license.method.vertex",
    "none": "license.method.none",
}

TOKEN_STATE_LABELS = {
    "valid": "license.token.valid",
    "expired": "license.token.expired",
    "unknown": "license.token.unknown",
    "absent": "license.token.absent",
}


def _fmt_bool(value):
    if value is True:
        return t("license.value.yes")
    if value is False:
        return t("license.value.no")
    return t("value.none")


def _fmt_expires(ms):
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def _credentials_meta():
    """Non-secret fields from ~/.claude/.credentials.json."""
    path = os.path.join(core.CLAUDE_DIR, ".credentials.json")
    meta = {
        "credentials_file": False,
        "credentials_path": path,
        "has_access_token": False,
        "has_refresh_token": False,
        "subscription": None,
        "rate_limit_tier": None,
        "expires_at": None,
        "expires_at_ms": None,
        "scopes": [],
        "token_state": "absent",
    }
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return meta
    except (OSError, json.JSONDecodeError):
        meta["credentials_error"] = True
        return meta

    meta["credentials_file"] = True
    oauth = data.get("claudeAiOauth") or {}
    meta["has_access_token"] = bool(oauth.get("accessToken"))
    meta["has_refresh_token"] = bool(oauth.get("refreshToken"))
    sub = oauth.get("subscriptionType")
    if isinstance(sub, str) and sub:
        meta["subscription"] = sub.lower()
    if oauth.get("rateLimitTier"):
        meta["rate_limit_tier"] = oauth["rateLimitTier"]
    scopes = oauth.get("scopes") or []
    if isinstance(scopes, str):
        scopes = [scopes]
    meta["scopes"] = [s for s in scopes if isinstance(s, str)]
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)):
        meta["expires_at_ms"] = expires
        meta["expires_at"] = _fmt_expires(expires)
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        if meta["has_refresh_token"]:
            meta["token_state"] = "valid"
        else:
            meta["token_state"] = "valid" if expires > now_ms else "expired"
    elif meta["has_access_token"]:
        meta["token_state"] = "valid" if meta["has_refresh_token"] else "unknown"
    return meta


def _install_meta():
    """Install / first-run crumbs from ~/.claude.json (not secrets)."""
    path = os.path.join(core.HOME, ".claude.json")
    out = {"claude_json": False, "claude_json_path": path}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return out
    out["claude_json"] = True
    for key in ("installMethod", "firstStartTime", "autoUpdates"):
        if key in data:
            out[key] = data[key]
    return out


def _display_value(field_id, value):
    if value is None or value == "" or value == []:
        return t("value.none")
    if field_id in ("has_access_token", "has_refresh_token", "credentials_file",
                    "hasExtraUsageEnabled", "claude_json"):
        return _fmt_bool(bool(value))
    if field_id == "method":
        return t(METHOD_LABELS.get(value, "license.method.none"))
    if field_id == "token_state":
        return t(TOKEN_STATE_LABELS.get(value, "license.token.unknown"))
    if field_id == "billingType":
        key = BILLING_LABELS.get(value)
        return t(key) if key else str(value)
    if field_id == "organizationType" and value in core.PLAN_KEYS:
        return t(core.PLAN_KEYS[value])
    if field_id == "subscription" and value in core.PLAN_KEYS:
        return t(core.PLAN_KEYS[value])
    if field_id == "scopes":
        return ", ".join(value) if value else t("value.none")
    if isinstance(value, bool):
        return _fmt_bool(value)
    return str(value)


def _rows(fields, source):
    """One display row per known field (empty → 'none')."""
    rows = []
    for field_id, label_key in fields:
        value = source.get(field_id)
        rows.append({
            "id": field_id,
            "label": t(label_key),
            "value": _display_value(field_id, value),
            "raw": value,
        })
    return rows


def build_report():
    """Full licence picture for UI / CLI / JSON copy."""
    account, _mcp = core.collect_account_and_mcp()
    env = core.collect_env()
    auth = core.collect_auth(env)
    cred = _credentials_meta()
    install = _install_meta()

    # Prefer credential subscription; fall back to organisation type.
    subscription = cred.get("subscription") or auth.get("subscription")
    if not subscription:
        org = account.get("organizationType") or ""
        if org.startswith("claude_"):
            subscription = org[len("claude_"):]

    merged_auth = {
        **auth,
        **{k: cred[k] for k in (
            "credentials_file", "credentials_path", "has_access_token",
            "has_refresh_token", "rate_limit_tier", "expires_at", "scopes",
            "token_state") if k in cred},
        "subscription": subscription,
        "plan": core.plan_label(account, {**auth, "subscription": subscription}),
        "method": auth.get("method") or "none",
    }
    # If credentials say oauth but collect_auth already set method, keep it.
    if cred.get("has_access_token") and merged_auth["method"] == "none":
        merged_auth["method"] = "oauth"

    env_flags = {
        "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY" in env,
        "ANTHROPIC_AUTH_TOKEN": "ANTHROPIC_AUTH_TOKEN" in env,
        "CLAUDE_CODE_OAUTH_TOKEN": "CLAUDE_CODE_OAUTH_TOKEN" in env,
        "CLAUDE_CODE_USE_BEDROCK": core.truthy(env.get("CLAUDE_CODE_USE_BEDROCK", "")),
        "CLAUDE_CODE_USE_VERTEX": core.truthy(env.get("CLAUDE_CODE_USE_VERTEX", "")),
    }

    present = merged_auth["method"] != "none"
    sections = [
        {
            "id": "subscription",
            "title": t("license.section.subscription"),
            "summary": merged_auth["plan"],
            "rows": _rows(AUTH_FIELDS, merged_auth),
        },
        {
            "id": "account",
            "title": t("license.section.account"),
            "summary": (account.get("emailAddress")
                        or account.get("organizationName")
                        or t("value.none")),
            "rows": _rows(ACCOUNT_FIELDS, account),
        },
        {
            "id": "environment",
            "title": t("license.section.environment"),
            "summary": t("license.env.summary",
                         count=sum(1 for v in env_flags.values() if v)),
            "rows": _rows(ENV_FIELDS, env_flags),
        },
        {
            "id": "install",
            "title": t("license.section.install"),
            "summary": install.get("installMethod") or (
                t("license.value.present") if install.get("claude_json")
                else t("value.none")),
            "rows": _rows(
                (("claude_json", "license.field.claude_json"),
                 ("claude_json_path", "license.field.claude_json_path"),
                 ("installMethod", "license.field.install_method"),
                 ("firstStartTime", "license.field.first_start"),
                 ("autoUpdates", "license.field.auto_updates")),
                install,
            ),
        },
    ]

    return {
        "present": present,
        "plan": merged_auth["plan"],
        "method": merged_auth["method"],
        "subscription": subscription,
        "token_state": merged_auth.get("token_state"),
        "account": account,
        "auth": merged_auth,
        "environment": env_flags,
        "install": install,
        "sections": sections,
    }


def verdict_key(report):
    if not report["present"]:
        return "license.verdict.none"
    if report.get("token_state") == "expired":
        return "license.verdict.expired"
    return "license.verdict.ok"
