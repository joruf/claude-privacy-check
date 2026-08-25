"""The outbound telemetry queue: parsing, the content scan, and its restraint.

Two properties matter more than the parsing and are pinned down here:

- a secret hidden inside a base64 field is still found, because that is the one
  place a plain text search would miss it, and
- neither the excerpt nor the baseline is allowed to become a second copy of
  what was found.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from unittest import mock

from claude_privacy_check import core, telemetry

FAKE_KEY = "sk-ant-" + "A1b2C3d4E5f6G7h8J9k0"


def event(**payload):
    """One queue line, in the shape Claude Code writes."""
    body = {"event_name": "tengu_started",
            "client_timestamp": "2026-08-06T05:15:01.699Z",
            "session_id": "s-1", "auth": {"account_uuid": "a-1"}}
    body.update(payload)
    return json.dumps({"event_type": "ClaudeCodeInternalEvent", "event_data": body})


class Queue:
    """A throwaway ~/.claude with a telemetry directory in it."""

    def __init__(self, lines=(), names=(), config=None):
        self.tmp = tempfile.TemporaryDirectory()
        home = self.tmp.name
        claude = os.path.join(home, ".claude")
        queue = os.path.join(claude, "telemetry")
        os.makedirs(queue)
        os.makedirs(os.path.join(claude, "projects"), exist_ok=True)
        if lines:
            with open(os.path.join(queue, "1p_failed_events.a.b.json"), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        for sub, entries in names:
            os.makedirs(os.path.join(claude, sub), exist_ok=True)
            for entry in entries:
                open(os.path.join(claude, sub, entry), "w", encoding="utf-8").close()
        if config is not None:
            with open(os.path.join(home, ".claude.json"), "w", encoding="utf-8") as fh:
                json.dump(config, fh)
        self.patches = [
            mock.patch.object(telemetry, "HOME", home),
            mock.patch.object(telemetry, "CLAUDE_DIR", claude),
            mock.patch.object(telemetry, "TELEMETRY_DIR", queue),
            mock.patch.object(telemetry, "PROJECTS_DIR",
                              os.path.join(claude, "projects")),
        ]

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()
        return False


class Parsing(unittest.TestCase):
    def test_a_missing_directory_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(telemetry, "TELEMETRY_DIR",
                                   os.path.join(tmp, "nope")):
                report = telemetry.read_queue()
        self.assertFalse(report["exists"])
        self.assertEqual(report["events"], 0)
        self.assertEqual(telemetry.verdict_key(report), "telemetry.verdict.empty")

    def test_events_fields_and_span(self):
        with Queue([event(model="claude-opus-5"),
                    event(event_name="tengu_skill_loaded",
                          client_timestamp="2026-08-09T07:00:00.000Z")]):
            report = telemetry.read_queue()
        self.assertEqual(report["events"], 2)
        self.assertEqual(report["oldest"], "2026-08-06")
        self.assertEqual(report["newest"], "2026-08-09")
        names = {e["name"]: e["count"] for e in report["event_names"]}
        self.assertEqual(names, {"tengu_started": 1, "tengu_skill_loaded": 1})
        paths = {f["path"] for f in report["fields"]}
        self.assertIn("event_data.auth.account_uuid", paths)

    def test_a_broken_line_is_counted_not_fatal(self):
        with Queue([event(), "{not json", "", event()]):
            report = telemetry.read_queue()
        self.assertEqual(report["events"], 2)
        self.assertEqual(report["unreadable"], 1)


class ContentScan(unittest.TestCase):
    def test_the_home_path_is_found(self):
        with Queue([event(cwd="/home/someone/work")]):
            report = telemetry.read_queue([(telemetry.CAT_PATH[0], "/home/someone")])
        hits = {c["slug"]: c["count"] for c in report["categories"]}
        self.assertEqual(hits[telemetry.CAT_PATH[0]], 1)
        self.assertEqual(telemetry.verdict_key(report),
                         "telemetry.verdict.identifiers")

    def test_a_secret_inside_a_base64_field_is_found(self):
        """The whole reason the decoder exists: a plain grep walks past this."""
        blob = base64.b64encode(json.dumps({"note": FAKE_KEY}).encode()).decode()
        with Queue([event(additional_metadata=blob)]):
            report = telemetry.read_queue()
        hits = {c["slug"]: c["count"] for c in report["categories"]}
        self.assertEqual(hits[telemetry.CAT_SECRET[0]], 1)
        self.assertEqual(telemetry.verdict_key(report), "telemetry.verdict.secret")

    def test_a_plain_text_search_really_would_miss_it(self):
        """Guards the guard: the encoded line must not contain the key."""
        blob = base64.b64encode(json.dumps({"note": FAKE_KEY}).encode()).decode()
        self.assertNotIn(FAKE_KEY, event(additional_metadata=blob))

    def test_the_secret_itself_is_never_put_in_the_sample(self):
        blob = base64.b64encode(json.dumps({"note": FAKE_KEY}).encode()).decode()
        with Queue([event(additional_metadata=blob)]):
            report = telemetry.read_queue()
        samples = next(c["samples"] for c in report["categories"]
                       if c["slug"] == telemetry.CAT_SECRET[0])
        self.assertTrue(samples, "the shape that matched should be reported")
        for sample in samples:
            self.assertNotIn(FAKE_KEY, sample)

    def test_a_clean_queue_says_so(self):
        with Queue([event()]):
            report = telemetry.read_queue([(telemetry.CAT_PATH[0], "/home/absent")])
        self.assertEqual(telemetry.verdict_key(report), "telemetry.verdict.clean")

    def test_decoded_fields_are_shown_decoded(self):
        blob = base64.b64encode(b'{"subscription_type":"team"}').decode()
        with Queue([event(additional_metadata=blob)]):
            report = telemetry.read_queue()
        decoded = {d["path"]: d["sample"] for d in report["decoded"]}
        self.assertIn("event_data.additional_metadata", decoded)
        self.assertIn("subscription_type",
                      decoded["event_data.additional_metadata"])

    def test_a_hex_digest_is_not_mistaken_for_base64(self):
        with Queue([event(device_id="a" * 64)]):
            report = telemetry.read_queue()
        self.assertEqual(report["decoded"], [])


class LocalNames(unittest.TestCase):
    def test_names_chosen_here_are_reported_with_their_kind(self):
        with Queue([event(skill_name="emis"), event(skill_name="emis"),
                    event(skill_name="quality")],
                   names=[("skills", ["emis"]), ("agents", ["quality.md"])]):
            report = telemetry.read_queue()
        found = {e["name"]: (e["kind"], e["count"]) for e in report["local_names"]}
        self.assertEqual(found, {"emis": ("skill", 2), "quality": ("agent", 1)})

    def test_a_two_letter_name_is_matched_whole_not_as_a_substring(self):
        """Whole-value matching is what makes a short name safe to look for."""
        with Queue([event(skill_name="pm"), event(model="claude-opus-5")],
                   names=[("commands", ["pm.md"])]):
            report = telemetry.read_queue()
        found = {e["name"]: e["count"] for e in report["local_names"]}
        self.assertEqual(found, {"pm": 1}, "'pm' must not match inside other values")

    def test_a_name_that_never_travelled_is_not_reported(self):
        with Queue([event()], names=[("skills", ["unused"])]):
            report = telemetry.read_queue()
        self.assertEqual(report["local_names"], [])


class DeviceId(unittest.TestCase):
    """The field is called device_id. That is a label, not evidence."""

    def test_the_user_id_is_recognised_as_such(self):
        with Queue([event(device_id="u" * 64)],
                   config={"userID": "u" * 64, "machineID": "m" * 64}):
            report = telemetry.read_queue()
        self.assertEqual(report["device_id_kind"], "user")

    def test_a_real_machine_id_is_recognised_too(self):
        with Queue([event(device_id="m" * 64)],
                   config={"userID": "u" * 64, "machineID": "m" * 64}):
            report = telemetry.read_queue()
        self.assertEqual(report["device_id_kind"], "machine")

    def test_neither_is_reported_as_unknown(self):
        with Queue([event(device_id="x" * 64)],
                   config={"userID": "u" * 64, "machineID": "m" * 64}):
            report = telemetry.read_queue()
        self.assertEqual(report["device_id_kind"], "unknown")


class SnapshotRestraint(unittest.TestCase):
    """The baseline is written to disk and kept. It must stay a tally."""

    def test_the_summary_carries_counts_but_no_content(self):
        with Queue([event(cwd="/home/someone/secret-client")]):
            summary = telemetry.collect_summary(
                [(telemetry.CAT_PATH[0], "/home/someone")])
        self.assertEqual(summary["hits"], {telemetry.CAT_PATH[0]: 1})
        rendered = json.dumps(summary)
        self.assertNotIn("secret-client", rendered)
        self.assertNotIn("/home/someone", rendered)

    def test_the_queue_is_excluded_from_the_baseline_diff(self):
        """It drains and refills on its own; a diff would cry wolf every run."""
        self.assertIn("telemetry", core.DIFF_IGNORE_ROOTS)
        changes = core.diff({"telemetry": {"events": 1}}, {"telemetry": {"events": 9}})
        self.assertEqual(changes, [])


class Assessment(unittest.TestCase):
    def _snapshot(self, queue):
        return {"surfaces": {}, "env": {}, "shell_profiles": {}, "account": {},
                "auth": {"method": "none"}, "mcp_servers": {}, "plugins": [],
                "local_history": {}, "telemetry": queue}

    def test_a_hit_is_critical(self):
        findings = core.assess(self._snapshot(
            {"exists": True, "files": 1, "events": 3, "hits": {"path": 2}}))
        match = [f for f in findings if f["key"] == "finding.telemetry_content"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["severity"], "CRITICAL")
        self.assertEqual(match[0]["params"]["count"], 2)

    def test_a_clean_queue_is_reported_as_checked(self):
        """A surface that was examined and came back negative should say so."""
        findings = core.assess(self._snapshot(
            {"exists": True, "files": 2, "events": 7, "hits": {}}))
        match = [f for f in findings if f["key"] == "finding.telemetry_queue"]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["severity"], "INFO")
        self.assertEqual(match[0]["params"]["events"], 7)

    def test_no_queue_produces_no_finding(self):
        findings = core.assess(self._snapshot(
            {"exists": False, "files": 0, "events": 0, "hits": {}}))
        self.assertEqual([f for f in findings
                          if f["key"].startswith("finding.telemetry_")], [])


class Translations(unittest.TestCase):
    def test_every_category_name_is_translated(self):
        import pathlib
        catalog = json.loads(
            (pathlib.Path(__file__).resolve().parent.parent / "claude_privacy_check"
             / "locales" / "en.json").read_text(encoding="utf-8"))
        for _slug, key in telemetry.CATEGORIES:
            self.assertIn(key, catalog)
        for _sub, kind in telemetry.NAME_DIRS:
            self.assertIn(f"telemetry.kind.{kind}", catalog)


if __name__ == "__main__":
    unittest.main()
