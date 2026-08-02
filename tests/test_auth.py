"""Licence / auth detection: what Claude Code needs to run."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from claude_privacy_check import core


def _snapshot(**overrides):
    base = {
        "surfaces": {},
        "env": {},
        "shell_profiles": {},
        "account": {},
        "auth": {
            "credentials_file": False,
            "has_access_token": False,
            "has_refresh_token": False,
            "token_state": "absent",
            "subscription": None,
            "rate_limit_tier": None,
            "method": "none",
        },
        "mcp_servers": {},
        "plugins": [],
        "local_history": {},
    }
    base.update(overrides)
    return base


class AuthCollect(unittest.TestCase):
    def test_no_credentials_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "CLAUDE_DIR", tmp):
                auth = core.collect_auth(env={})
        self.assertEqual(auth["method"], "none")
        self.assertEqual(auth["token_state"], "absent")
        self.assertFalse(auth["credentials_file"])

    def test_oauth_max_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".credentials.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({
                    "claudeAiOauth": {
                        "accessToken": "secret-token",
                        "refreshToken": "secret-refresh",
                        "expiresAt": 1,
                        "subscriptionType": "max",
                        "rateLimitTier": "default_claude_max_20x",
                    }
                }, fh)
            with mock.patch.object(core, "CLAUDE_DIR", tmp):
                auth = core.collect_auth(env={})
        self.assertEqual(auth["method"], "oauth")
        self.assertEqual(auth["subscription"], "max")
        self.assertEqual(auth["token_state"], "valid")
        self.assertTrue(auth["has_access_token"])
        self.assertNotIn("secret-token", json.dumps(auth))

    def test_expired_without_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".credentials.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({
                    "claudeAiOauth": {
                        "accessToken": "secret",
                        "expiresAt": 1,  # long expired
                        "subscriptionType": "pro",
                    }
                }, fh)
            with mock.patch.object(core, "CLAUDE_DIR", tmp):
                auth = core.collect_auth(env={})
        self.assertEqual(auth["token_state"], "expired")
        self.assertEqual(auth["subscription"], "pro")

    def test_api_key_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "CLAUDE_DIR", tmp):
                auth = core.collect_auth(env={"ANTHROPIC_API_KEY": "digest"})
        self.assertEqual(auth["method"], "api_key")
        self.assertEqual(auth["token_state"], "valid")


class AuthAssess(unittest.TestCase):
    def _keys(self, snapshot):
        return [f["key"] for f in core.assess(snapshot)]

    def test_no_license_finding(self):
        keys = self._keys(_snapshot())
        self.assertIn("finding.no_license", keys)

    def test_subscription_finding(self):
        keys = self._keys(_snapshot(auth={
            "credentials_file": True,
            "has_access_token": True,
            "has_refresh_token": True,
            "token_state": "valid",
            "subscription": "pro",
            "rate_limit_tier": None,
            "method": "oauth",
        }))
        self.assertIn("finding.subscription", keys)
        self.assertNotIn("finding.no_license", keys)

    def test_enterprise_still_flagged(self):
        keys = self._keys(_snapshot(
            account={"organizationType": "claude_enterprise"},
            auth={
                "credentials_file": True,
                "has_access_token": True,
                "has_refresh_token": True,
                "token_state": "valid",
                "subscription": "enterprise",
                "rate_limit_tier": None,
                "method": "oauth",
            },
        ))
        self.assertIn("finding.enterprise_plan", keys)
        self.assertIn("finding.subscription", keys)

    def test_plan_label(self):
        from claude_privacy_check.i18n import t
        self.assertEqual(core.plan_label({}, {"method": "none"}), t("plan.none"))
        self.assertEqual(core.plan_label({}, {"method": "api_key"}), t("plan.api_key"))
        self.assertEqual(
            core.plan_label({"organizationType": "claude_team"}, {}),
            t("plan.team"))


if __name__ == "__main__":
    unittest.main()
