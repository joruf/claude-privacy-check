"""Working time reconstructed from transcript timestamps.

The arithmetic here is the whole product of the view, so it is pinned down:
block detection across a break, the day cut at *local* midnight rather than at
UTC midnight, and what does and does not count as something a person typed.

The time zone is fixed for the duration of these tests. Timestamps in a
transcript are UTC; every figure the view shows is local, and a test that ran in
whatever zone the machine happens to use would prove nothing.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check import data, worktime  # noqa: E402

# Berlin: UTC+2 in July, so 22:00 UTC is 00:00 the next local day -- exactly the
# case a naive implementation gets wrong.
TZ = "Europe/Berlin"


def stamp(text, kind="user", extra=""):
    """One transcript line, compact the way Claude Code writes it."""
    return f'{{"type":"{kind}","timestamp":"{text}","sessionId":"s"{extra}}}\n'


class Blocks(unittest.TestCase):
    def test_a_short_gap_is_still_work(self):
        minutes = [100, 101, 110, 115]          # gaps of 1, 9 and 5 minutes
        self.assertEqual(worktime.blocks(minutes), [(100, 115)])

    def test_a_long_gap_splits_the_day(self):
        minutes = [100, 101, 200, 201]
        self.assertEqual(worktime.blocks(minutes), [(100, 101), (200, 201)])

    def test_the_threshold_itself_counts_as_work(self):
        gap = worktime.IDLE_GAP
        self.assertEqual(worktime.blocks([0, gap]), [(0, gap)])
        self.assertEqual(worktime.blocks([0, gap + 1]), [(0, 0), (gap + 1, gap + 1)])

    def test_unsorted_input_is_handled(self):
        self.assertEqual(worktime.blocks([115, 100, 101]), [(100, 115)])

    def test_no_minutes_no_blocks(self):
        self.assertEqual(worktime.blocks([]), [])


class Formatting(unittest.TestCase):
    def test_minutes_read_like_a_timesheet(self):
        self.assertEqual(worktime.human_minutes(0), "0:00 h")
        self.assertEqual(worktime.human_minutes(59), "0:59 h")
        self.assertEqual(worktime.human_minutes(465), "7:45 h")
        self.assertEqual(worktime.human_minutes(60 * 125 + 31), "125:31 h")

    def test_negative_input_does_not_produce_a_broken_string(self):
        self.assertEqual(worktime.human_minutes(-5), "0:00 h")


class Prompts(unittest.TestCase):
    def test_a_typed_prompt_counts(self):
        self.assertTrue(worktime._is_prompt(b'{"type":"user","timestamp":"x"}'))

    def test_a_tool_result_does_not(self):
        self.assertFalse(worktime._is_prompt(
            b'{"type":"user","toolUseResult":{"ok":true}}'))

    def test_a_subagent_turn_does_not(self):
        self.assertFalse(worktime._is_prompt(
            b'{"type":"user","isSidechain":true}'))

    def test_an_assistant_line_does_not(self):
        self.assertFalse(worktime._is_prompt(b'{"type":"assistant"}'))


class Report(unittest.TestCase):
    def setUp(self):
        self.previous_tz = os.environ.get("TZ")
        os.environ["TZ"] = TZ
        time.tzset()
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)
        self.original = data.PROJECTS_DIR
        data.PROJECTS_DIR = str(self.projects)

    def tearDown(self):
        data.PROJECTS_DIR = self.original
        self.tmp.cleanup()
        if self.previous_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = self.previous_tz
        time.tzset()

    def write(self, project, session, lines):
        folder = self.projects / project
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{session}.jsonl").write_text("".join(lines), encoding="utf-8")

    def test_a_morning_with_one_break(self):
        """08:00-08:20 local, a 40 minute break, then 09:00-09:10."""
        self.write("-tmp-project", "a", [
            stamp("2026-07-06T06:00:00.000Z"),
            stamp("2026-07-06T06:10:00.000Z"),
            stamp("2026-07-06T06:20:00.000Z"),
            stamp("2026-07-06T07:00:00.000Z"),
            stamp("2026-07-06T07:10:00.500Z"),
        ])
        report = worktime.build_report()
        self.assertEqual(report["active_days"], 1)
        day = report["days"][0]
        self.assertEqual(day["date"], "2026-07-06")
        self.assertEqual(day["start"], "08:00")
        self.assertEqual(day["end"], "09:10")
        self.assertEqual(day["blocks"], 2)
        # 21 minutes in the first block, 11 in the second -- inclusive, because a
        # minute carrying an event is a worked minute.
        self.assertEqual(day["active"], 21 + 11)
        self.assertEqual(day["span"], 71)
        self.assertEqual(day["pause"], 71 - 32)
        self.assertEqual(day["weekday"], 0)          # a Monday
        self.assertFalse(day["weekend"])
        self.assertEqual(day["off_hours"], 0)
        self.assertEqual(report["total_active"], 32)
        self.assertEqual(report["longest_block"], 21)

    def test_late_utc_lands_on_the_next_local_day(self):
        """22:30 UTC in July is 00:30 the following day in Berlin."""
        self.write("-tmp-project", "a", [
            stamp("2026-07-06T22:30:00.000Z"),
            stamp("2026-07-06T22:40:00.000Z"),
        ])
        report = worktime.build_report()
        self.assertEqual([d["date"] for d in report["days"]], ["2026-07-07"])
        day = report["days"][0]
        self.assertEqual(day["start"], "00:30")
        self.assertEqual(day["off_hours"], day["active"])
        self.assertEqual(report["off_hours_days"], 1)

    def test_days_are_newest_first_and_weeks_oldest_first(self):
        for day in ("2026-07-06", "2026-07-08", "2026-07-15"):
            self.write("-tmp-project", day, [stamp(f"{day}T08:00:00.000Z")])
        report = worktime.build_report()
        self.assertEqual([d["date"] for d in report["days"]],
                         ["2026-07-15", "2026-07-08", "2026-07-06"])
        self.assertEqual([(w["year"], w["week"], w["days"]) for w in report["weeks"]],
                         [(2026, 28, 2), (2026, 29, 1)])

    def test_a_weekend_is_reported_as_one(self):
        self.write("-tmp-project", "a", [
            stamp("2026-07-05T10:00:00.000Z"),      # a Sunday
            stamp("2026-07-05T10:20:00.000Z"),
        ])
        report = worktime.build_report()
        self.assertTrue(report["days"][0]["weekend"])
        self.assertEqual(report["weekend_days"], 1)
        self.assertEqual(report["weekend_active"], report["total_active"])
        self.assertEqual(worktime.verdict_key(report), "worktime.verdict.offhours")

    def test_hours_are_counted_in_local_time(self):
        # 06:00-06:20 UTC = 08:00-08:20 local: 21 minutes, all in hour 8.
        self.write("-tmp-project", "a", [
            stamp("2026-07-06T06:00:00.000Z"),
            stamp("2026-07-06T06:10:00.000Z"),
            stamp("2026-07-06T06:20:00.000Z"),
        ])
        report = worktime.build_report()
        self.assertEqual(report["hour_active"][8], 21)
        self.assertEqual(report["hour_active"][6], 0)
        self.assertEqual(sum(report["hour_active"].values()), report["total_active"])

    def test_a_block_spanning_two_hours_is_split_between_them(self):
        # 06:50-07:10 UTC = 08:50-09:10 local: 10 minutes in hour 8, 11 in hour 9.
        self.write("-tmp-project", "a", [
            stamp("2026-07-06T06:50:00.000Z"),
            stamp("2026-07-06T07:00:00.000Z"),
            stamp("2026-07-06T07:10:00.000Z"),
        ])
        report = worktime.build_report()
        self.assertEqual(report["hour_active"][8], 10)
        self.assertEqual(report["hour_active"][9], 11)

    def test_prompts_are_counted_but_tool_results_are_not(self):
        self.write("-tmp-project", "a", [
            stamp("2026-07-06T06:00:00.000Z"),
            stamp("2026-07-06T06:01:00.000Z", extra=',"toolUseResult":{"ok":1}'),
            stamp("2026-07-06T06:02:00.000Z", kind="assistant"),
            stamp("2026-07-06T06:03:00.000Z", extra=',"isSidechain":true'),
        ])
        report = worktime.build_report()
        self.assertEqual(report["total_prompts"], 1)
        self.assertEqual(report["days"][0]["prompts"], 1)
        self.assertEqual(report["stamps"], 4)

    def test_two_projects_on_one_day(self):
        """Interleaved work: the day counts a minute once, each project claims it."""
        self.write("-tmp-one", "a", [stamp("2026-07-06T06:00:00.000Z"),
                                     stamp("2026-07-06T06:10:00.000Z")])
        self.write("-tmp-two", "b", [stamp("2026-07-06T06:05:00.000Z"),
                                     stamp("2026-07-06T06:15:00.000Z")])
        report = worktime.build_report()
        self.assertEqual(report["active_days"], 1)
        self.assertEqual(len(report["days"][0]["projects"]), 2)
        self.assertEqual(len(report["projects"]), 2)
        self.assertEqual(report["total_active"], 16)         # 08:00-08:15 local
        self.assertEqual([p["active"] for p in report["projects"]], [11, 11])
        self.assertGreater(sum(p["active"] for p in report["projects"]),
                           report["total_active"])

    def test_subagent_transcripts_belong_to_the_project_above(self):
        folder = self.projects / "-tmp-project" / "subagents"
        folder.mkdir(parents=True)
        (folder / "sub.jsonl").write_text(stamp("2026-07-06T06:00:00.000Z"),
                                          encoding="utf-8")
        report = worktime.build_report()
        self.assertEqual(len(report["projects"]), 1)
        self.assertNotIn("subagents", report["projects"][0]["label"])

    def test_a_line_without_a_usable_timestamp_is_skipped(self):
        self.write("-tmp-project", "a", [
            '{"type":"user","timestamp":"not-a-date"}\n',
            '{"type":"summary"}\n',
            stamp("2026-07-06T06:00:00.000Z"),
        ])
        report = worktime.build_report()
        self.assertEqual(report["stamps"], 1)
        self.assertEqual(report["active_days"], 1)

    def test_an_empty_history_reports_nothing_rather_than_failing(self):
        report = worktime.build_report()
        self.assertEqual(report["active_days"], 0)
        self.assertEqual(report["total_active"], 0)
        self.assertIsNone(report["longest_day"])
        self.assertIsNone(report["first_day"])
        self.assertEqual(worktime.verdict_key(report), "worktime.verdict.empty")
        self.assertEqual(report["hour_active"], {h: 0 for h in range(24)})

    def test_a_missing_projects_directory_is_not_an_error(self):
        data.PROJECTS_DIR = str(self.projects / "does-not-exist")
        report = worktime.build_report()
        self.assertEqual(report["sessions"], 0)
        self.assertEqual(report["active_days"], 0)

    def test_the_report_survives_json(self):
        """--json and “Copy JSON” hand the report out verbatim."""
        self.write("-tmp-project", "a", [stamp("2026-07-06T06:00:00.000Z")])
        report = worktime.build_report()
        restored = json.loads(json.dumps(report, default=str))
        self.assertEqual(restored["total_active"], report["total_active"])

    def test_progress_is_reported_for_every_transcript(self):
        for name in ("a", "b"):
            self.write("-tmp-project", name, [stamp("2026-07-06T06:00:00.000Z")])
        seen = []
        worktime.build_report(progress=lambda done, total: seen.append((done, total)))
        self.assertEqual(seen, [(0, 2), (1, 2), (2, 2)])


if __name__ == "__main__":
    unittest.main()
