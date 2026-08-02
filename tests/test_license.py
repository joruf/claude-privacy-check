"""Licence detail report."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from claude_privacy_check import core, license as licence


class LicenseReport(unittest.TestCase):
    def test_empty_machine_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(core, "HOME", tmp), \
                 mock.patch.object(core, "CLAUDE_DIR", os.path.join(tmp, ".claude")):
                report = licence.build_report()
        self.assertFalse(report["present"])
        self.assertEqual(report["method"], "none")
        ids = [s["id"] for s in report["sections"]]
        self.assertEqual(ids, ["subscription", "account", "environment", "install"])
        # Every section exposes detail rows for expand-in-GUI
        for section in report["sections"]:
            self.assertTrue(section["rows"], section["id"])

    def test_oauth_max_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = os.path.join(tmp, ".claude")
            os.makedirs(claude)
            with open(os.path.join(claude, ".credentials.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({
                    "claudeAiOauth": {
                        "accessToken": "SECRET",
                        "refreshToken": "SECRET2",
                        "expiresAt": 9999999999999,
                        "subscriptionType": "max",
                        "rateLimitTier": "default_claude_max_20x",
                        "scopes": ["user:inference", "user:profile"],
                    }
                }, fh)
            with open(os.path.join(tmp, ".claude.json"), "w", encoding="utf-8") as fh:
                json.dump({
                    "oauthAccount": {
                        "emailAddress": "a@example.com",
                        "organizationName": "Acme",
                        "organizationType": "claude_max",
                        "organizationRole": "admin",
                        "billingType": "stripe_subscription",
                        "hasExtraUsageEnabled": True,
                        "accountUuid": "acc-1",
                        "organizationUuid": "org-1",
                        "displayName": "Ada",
                    },
                    "installMethod": "native",
                    "firstStartTime": "2026-01-01T00:00:00Z",
                }, fh)
            with mock.patch.object(core, "HOME", tmp), \
                 mock.patch.object(core, "CLAUDE_DIR", claude):
                report = licence.build_report()
        self.assertTrue(report["present"])
        self.assertEqual(report["subscription"], "max")
        blob = json.dumps(report)
        self.assertNotIn("SECRET", blob)
        auth_rows = {r["id"]: r for r in report["sections"][0]["rows"]}
        self.assertIn("user:inference", auth_rows["scopes"]["value"])
        self.assertEqual(auth_rows["has_access_token"]["raw"], True)
        account_rows = {r["id"]: r for r in report["sections"][1]["rows"]}
        self.assertEqual(account_rows["emailAddress"]["raw"], "a@example.com")
        self.assertIn("Stripe", account_rows["billingType"]["value"])


if __name__ == "__main__":
    unittest.main()
