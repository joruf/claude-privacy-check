"""What is allowed to interrupt somebody, and what is not.

Three false alarms in one afternoon prompted these: a Team plan and a queue
length reported as if they were findings, a ``monitoring_notice`` that had just
*vanished* raised as CRITICAL, and a parse error on a settings file that was
merely being written at that moment. An alarm that cries wolf is worse than no
alarm, so the conditions are pinned down here.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from claude_privacy_check import core, watch

NOTICE = "surfaces.file:~/.claude/policy-limits.json.content.monitoring_notice"
PARSE = "surfaces.file:~/.claude/remote-settings.json.parse_error"
HOOK = "surfaces.file:~/.claude/settings.json.content.hooks.UserPromptSubmit"


class Blank(unittest.TestCase):
    def test_the_forms_nothing_arrives_in(self):
        for value in (core.MISSING, None, "", "   ", "null", "[]", "{}",
                      False, 0, [], {}):
            self.assertTrue(core.is_blank(value), f"{value!r} should count as blank")

    def test_a_real_value_is_not_blank(self):
        for value in ("We record everything", "[]x", 1, True, ["a"], {"a": 1},
                      "false positive"):
            self.assertFalse(core.is_blank(value), f"{value!r} should count as set")

    def test_the_string_false_is_blank_but_a_sentence_starting_with_it_is_not(self):
        self.assertTrue(core.is_blank("false"))
        self.assertFalse(core.is_blank("false CA bundle"))


class ChangeSeverity(unittest.TestCase):
    """A value that appeared and one that vanished are not the same event."""

    def test_a_monitoring_notice_appearing_is_critical(self):
        self.assertEqual(core.change_severity(NOTICE, "We record everything"),
                         "CRITICAL")

    def test_a_monitoring_notice_vanishing_is_not(self):
        for gone in (core.MISSING, None, ""):
            self.assertEqual(core.change_severity(NOTICE, gone), "MEDIUM")

    def test_a_hook_appearing_is_critical_and_one_removed_is_not(self):
        self.assertEqual(core.change_severity(HOOK, '[{"command": "curl"}]'),
                         "CRITICAL")
        self.assertEqual(core.change_severity(HOOK, "[]"), "MEDIUM")

    def test_a_parse_error_that_cleared_is_not_high(self):
        self.assertEqual(core.change_severity(PARSE, "Expecting value: line 1"),
                         "HIGH")
        self.assertEqual(core.change_severity(PARSE, core.MISSING), "MEDIUM")

    def test_a_quiet_path_is_never_made_louder(self):
        """The cap only ever lowers -- an INFO path stays INFO."""
        quiet = "surfaces.file:~/.claude/settings.json.content.permissions.allow"
        self.assertEqual(core.path_severity(quiet), "INFO")
        self.assertEqual(core.change_severity(quiet, core.MISSING), "INFO")

    def test_the_cap_matches_path_severity_for_anything_set(self):
        for path in (NOTICE, PARSE, HOOK):
            self.assertEqual(core.change_severity(path, "something"),
                             core.path_severity(path))


class DiffUsesIt(unittest.TestCase):
    ABSENT = {}
    NULL = {"monitoring_notice": None}
    SET = {"monitoring_notice": "This session is being recorded."}

    @staticmethod
    def _snapshot(content):
        return {"surfaces": {"file:~/.claude/policy-limits.json":
                             {"exists": True, "content": content}}}

    def _severities(self, before, after):
        return [c["severity"] for c in
                core.diff(self._snapshot(before), self._snapshot(after))]

    def test_a_notice_being_set_shows_up_as_critical(self):
        self.assertEqual(self._severities(self.ABSENT, self.SET), ["CRITICAL"])

    def test_the_same_notice_disappearing_does_not(self):
        self.assertEqual(self._severities(self.SET, self.ABSENT), ["MEDIUM"])

    def test_a_notice_set_to_null_is_not_an_alarm(self):
        """What Claude Code actually writes: the key is there, the value is not.

        This is the exact change that produced the CRITICAL popup.
        """
        self.assertEqual(self._severities(self.ABSENT, self.NULL), ["MEDIUM"])
        self.assertEqual(self._severities(self.NULL, self.ABSENT), ["MEDIUM"])


class HalfWrittenFile(unittest.TestCase):
    """The watch reacts to the write itself, so it reads files mid-write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "remote-settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_a_readable_file_is_returned(self):
        self._write('{"a": 1}')
        self.assertEqual(core._load_json(self.path), {"a": 1})

    def _repair_after(self, seconds, text='{"a": 1}'):
        timer = threading.Timer(seconds, lambda: self._write(text))
        timer.start()
        self.addCleanup(timer.cancel)

    def test_a_file_repaired_during_the_retry_parses(self):
        self._write('{"a":')                       # caught mid-write
        self._repair_after(core.RETRY_DELAYS[0] / 3)
        self.assertEqual(core._load_json(self.path), {"a": 1})

    def test_a_file_still_being_filled_on_the_second_retry_parses(self):
        """The exact shape of the false alarm: 0 bytes when the watch looked."""
        self._write("")
        self._repair_after(core.RETRY_DELAYS[0] * 1.5)
        self.assertEqual(core._load_json(self.path), {"a": 1})

    def test_an_empty_file_that_stays_empty_is_not_an_error(self):
        self._write("   \n")
        self.assertEqual(core._load_json(self.path), {})

    def test_a_file_that_stays_broken_still_raises(self):
        self._write("{not json")
        with self.assertRaises(json.JSONDecodeError):
            core._load_json(self.path)

    def test_read_settings_file_reports_a_lasting_parse_error(self):
        self._write("{not json")
        entry = core.read_settings_file(self.path)
        self.assertIn("parse_error", entry)

    def test_an_empty_settings_file_produces_no_parse_error(self):
        """This is what fired the HIGH alert on remote-settings.json."""
        self._write("")
        entry = core.read_settings_file(self.path)
        self.assertNotIn("parse_error", entry)
        self.assertEqual(entry["content"], {})


class NotifyTriggers(unittest.TestCase):
    """Only HIGH and CRITICAL are worth a popup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "notified.json")
        self._real_state = watch.NOTIFY_STATE
        watch.NOTIFY_STATE = self.state
        self.sent = []

    def tearDown(self):
        watch.NOTIFY_STATE = self._real_state
        self.tmp.cleanup()

    def _run(self, findings=(), changes=()):
        result = {"snapshot": {}, "findings": list(findings),
                  "changes": list(changes), "status": "OK",
                  "baseline_path": "x", "baseline_time": None}
        with mock.patch.object(watch.core, "run_check", return_value=result), \
             mock.patch.object(watch, "_notify",
                               side_effect=lambda *a: self.sent.append(a)):
            watch.notify_check()
        return self.sent

    @staticmethod
    def _finding(severity, key="finding.x", **params):
        return {"severity": severity, "key": key, "params": params}

    @staticmethod
    def _change(severity, path="a.b"):
        return {"path": path, "before": "∅", "after": "x", "severity": severity}

    def test_a_paid_plan_and_a_queue_length_stay_silent(self):
        """Both are standing state, and both were reported as findings before."""
        sent = self._run(findings=[
            self._finding("INFO", "finding.subscription", plan="Team plan"),
            self._finding("INFO", "finding.telemetry_queue", events=425, files=12)])
        self.assertEqual(sent, [])

    def test_a_medium_deviation_stays_silent(self):
        self.assertEqual(self._run(changes=[self._change("MEDIUM")]), [])

    def test_a_critical_finding_notifies(self):
        self.assertEqual(len(self._run(findings=[self._finding("CRITICAL")])), 1)

    def test_a_high_deviation_notifies(self):
        self.assertEqual(len(self._run(changes=[self._change("HIGH")])), 1)

    def test_a_quiet_finding_next_to_a_loud_one_does_not_change_the_verdict(self):
        sent = self._run(findings=[self._finding("INFO", "finding.subscription"),
                                   self._finding("CRITICAL")])
        self.assertEqual(len(sent), 1)

    def test_the_same_state_notifies_only_once(self):
        self._run(findings=[self._finding("CRITICAL")])
        self._run(findings=[self._finding("CRITICAL")])
        self.assertEqual(len(self.sent), 1)

    def test_a_changing_queue_length_no_longer_counts_as_a_new_event(self):
        """The event count sat in the fingerprint, so every count was 'new'."""
        self._run(findings=[self._finding("CRITICAL"),
                            self._finding("INFO", "finding.telemetry_queue",
                                          events=425)])
        self._run(findings=[self._finding("CRITICAL"),
                            self._finding("INFO", "finding.telemetry_queue",
                                          events=612)])
        self.assertEqual(len(self.sent), 1)

    def test_a_growing_count_inside_a_loud_finding_does_not_renotify(self):
        """Same hit, larger number -- still the same thing to say."""
        for count in (3, 4, 17):
            self._run(findings=[self._finding("CRITICAL", "finding.telemetry_content",
                                              categories="credentials", count=count)])
        self.assertEqual(len(self.sent), 1)

    def test_a_new_category_in_the_same_finding_does_notify(self):
        """What was found is identity; how much of it is not."""
        self._run(findings=[self._finding("CRITICAL", "finding.telemetry_content",
                                          categories="credentials", count=3)])
        self._run(findings=[self._finding("CRITICAL", "finding.telemetry_content",
                                          categories="credentials, project names",
                                          count=3)])
        self.assertEqual(len(self.sent), 2)

    def test_the_all_clear_arrives_once_the_loud_signal_is_gone(self):
        self._run(findings=[self._finding("CRITICAL")])
        self._run(findings=[self._finding("INFO", "finding.subscription")])
        self.assertEqual(len(self.sent), 2)
        with open(self.state, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), [])


if __name__ == "__main__":
    unittest.main()
