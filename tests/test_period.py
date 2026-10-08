"""The period selection the time-based views share.

Two things are pinned here. The calendar arithmetic -- month starts across a
year boundary are where a hand-rolled version goes wrong. And that each of the
three reports actually honours a span: working time and the admin dashboard
by the timestamp of every line, the observer by the date a transcript was last
written, which is what its pattern has always gone by.

The time zone is fixed for the report tests, for the same reason as in the
working-time tests: timestamps are UTC, the days they land on are local.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check import analytics, data, observer, period, worktime  # noqa: E402

TZ = "Europe/Berlin"
JULY = (date(2026, 7, 1), date(2026, 7, 31))
AUGUST = (date(2026, 8, 1), date(2026, 8, 31))


class Bounds(unittest.TestCase):
    TODAY = date(2026, 10, 8)

    def test_the_default_is_the_last_full_month(self):
        self.assertEqual(period.DEFAULT, "last-month")
        self.assertEqual(period.bounds("last-month", self.TODAY),
                         (date(2026, 9, 1), date(2026, 9, 30)))

    def test_the_current_month_runs_up_to_today(self):
        self.assertEqual(period.bounds("current-month", self.TODAY),
                         (date(2026, 10, 1), self.TODAY))

    def test_three_months_are_the_running_one_and_the_two_before(self):
        self.assertEqual(period.bounds("last-3-months", self.TODAY),
                         (date(2026, 8, 1), self.TODAY))

    def test_the_whole_history_has_no_bounds(self):
        self.assertIsNone(period.bounds("all", self.TODAY))

    def test_month_starts_cross_the_year_boundary(self):
        january = date(2027, 1, 15)
        self.assertEqual(period.bounds("last-month", january),
                         (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(period.bounds("last-3-months", january)[0],
                         date(2026, 11, 1))

    def test_last_month_ends_on_its_own_last_day(self):
        self.assertEqual(period.bounds("last-month", date(2028, 3, 1))[1],
                         date(2028, 2, 29))          # a leap year

    def test_both_ends_are_inside(self):
        self.assertTrue(period.contains(JULY, date(2026, 7, 1)))
        self.assertTrue(period.contains(JULY, date(2026, 7, 31)))
        self.assertFalse(period.contains(JULY, date(2026, 8, 1)))
        self.assertTrue(period.contains(None, date(1999, 1, 1)))

    def test_a_file_last_written_before_the_span_is_skipped(self):
        before = datetime(2026, 6, 30, 23, 0).timestamp()
        inside = datetime(2026, 7, 1, 0, 30).timestamp()
        self.assertTrue(period.predates(JULY, before))
        self.assertFalse(period.predates(JULY, inside))
        self.assertFalse(period.predates(None, before))

    def test_every_choice_is_listed_once(self):
        self.assertEqual(len(set(period.CHOICES)), len(period.CHOICES))
        self.assertIn(period.DEFAULT, period.CHOICES)


def line(kind, stamp, extra=None):
    record = {"type": kind, "timestamp": stamp, **(extra or {})}
    return json.dumps(record, separators=(",", ":")) + "\n"


class Reports(unittest.TestCase):
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

    def write(self, project, session, lines, written=None):
        folder = self.projects / project
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{session}.jsonl"
        target.write_text("".join(lines), encoding="utf-8")
        if written is not None:
            stamp = datetime(*written).timestamp()
            os.utime(target, (stamp, stamp))

    def two_months(self):
        """One transcript in July and one spanning July into August."""
        self.write("-tmp-alpha", "july", [
            line("user", "2026-07-06T06:00:00.000Z"),
            line("user", "2026-07-06T06:10:00.000Z"),
        ], written=(2026, 7, 6, 8, 10))
        self.write("-tmp-beta", "both", [
            line("user", "2026-07-31T06:00:00.000Z"),
            line("user", "2026-08-03T06:00:00.000Z"),
            line("user", "2026-08-03T06:05:00.000Z"),
        ], written=(2026, 8, 3, 8, 5))

    def test_working_time_keeps_only_the_days_inside(self):
        self.two_months()
        report = worktime.build_report(span=AUGUST)
        self.assertEqual([d["date"] for d in report["days"]], ["2026-08-03"])
        self.assertEqual(report["total_active"], 6)
        self.assertEqual(report["sessions"], 1)
        self.assertEqual(report["stamps"], 2)
        self.assertEqual([p["label"] for p in report["projects"]], ["/tmp/beta"])
        self.assertEqual(report["period"], ["2026-08-01", "2026-08-31"])

    def test_working_time_is_summed_per_month(self):
        self.two_months()
        report = worktime.build_report()
        self.assertEqual(report["months"], [
            {"month": "2026-07", "active": 11 + 1, "days": 2},
            {"month": "2026-08", "active": 6, "days": 1},
        ])
        self.assertIsNone(report["period"])
        self.assertEqual(report["sessions"], 2)

    def test_working_time_skips_a_transcript_written_before_the_span(self):
        self.two_months()
        report = worktime.build_report(span=AUGUST)
        self.assertNotIn("/tmp/alpha", [p["label"] for p in report["projects"]])

    def test_an_empty_span_is_an_empty_report(self):
        self.two_months()
        report = worktime.build_report(span=(date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(report["active_days"], 0)
        self.assertEqual(report["months"], [])
        self.assertEqual(worktime.verdict_key(report), "worktime.verdict.empty")

    def test_the_dashboard_counts_only_lines_inside(self):
        self.two_months()
        report = analytics.build_report(span=JULY)
        self.assertEqual(report["events"], 3)
        self.assertEqual(report["own_messages"], 3)
        self.assertEqual(report["sessions"], 2)
        self.assertEqual(report["last_day"], "2026-07-31")

        august = analytics.build_report(span=AUGUST)
        self.assertEqual(august["events"], 2)
        self.assertEqual(august["sessions"], 1)
        self.assertEqual(august["transcripts"], 1)
        self.assertEqual([p["label"] for p in august["projects"]], ["/tmp/beta"])
        self.assertEqual(august["period"], ["2026-08-01", "2026-08-31"])

    def test_the_dashboard_leaves_the_whole_history_as_it_was(self):
        self.two_months()
        report = analytics.build_report()
        self.assertEqual(report["events"], 5)
        self.assertEqual(report["sessions"], 2)
        self.assertIsNone(report["period"])

    def test_the_observer_goes_by_the_date_a_transcript_was_last_written(self):
        self.two_months()
        july = observer.build_report(span=JULY)
        self.assertEqual(july["sessions"], 1)
        self.assertEqual([p["label"] for p in july["projects"]], ["/tmp/alpha"])
        self.assertEqual(july["period"], ["2026-07-01", "2026-07-31"])
        self.assertEqual(observer.build_report()["sessions"], 2)


if __name__ == "__main__":
    unittest.main()
