"""The admin dashboard: the arithmetic behind every figure it shows.

This view makes claims about a person to their employer, so the numbers have to
be right in both directions. What is pinned here is what would be embarrassing
to get wrong: the price table, what counts as a session, what counts as a line
a person typed, and the fact that a model with no published rate is reported as
unpriced rather than quietly valued at zero.

The time zone is fixed for the duration: transcript timestamps are UTC, every
figure the view shows is local, and a test that ran in whatever zone the
machine happens to use would prove nothing.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check import analytics, data  # noqa: E402

TZ = "Europe/Berlin"

# Claude Code writes compact JSON, one record per line, and both this module and
# the working-time view classify a line by looking for the field markers in the
# raw bytes. A fixture with spaces after the colons would not be the format
# either of them reads, so every helper here writes it the way the real files
# are written.
COMPACT = {"separators": (",", ":")}


def line(record):
    return json.dumps(record, **COMPACT) + "\n"


def user(text, stamp="2026-07-06T08:00:00.000Z"):
    return line({"type": "user", "timestamp": stamp,
                 "message": {"role": "user", "content": text}})


def tool_result(stamp="2026-07-06T08:01:00.000Z"):
    """A tool result is recorded as a user turn and is not a typed prompt."""
    return line({"type": "user", "timestamp": stamp,
                 "toolUseResult": {"ok": True},
                 "message": {"role": "user", "content": "…"}})


def sidechain(stamp="2026-07-06T08:02:00.000Z"):
    """A subagent's own turn, likewise not something this person typed."""
    return line({"type": "user", "timestamp": stamp, "isSidechain": True,
                 "message": {"role": "user", "content": "go"}})


def assistant(model="claude-opus-5", usage=None, content=None,
              stamp="2026-07-06T08:03:00.000Z"):
    message = {"role": "assistant", "model": model,
               "usage": usage if usage is not None else {
                   "input_tokens": 0, "output_tokens": 0,
                   "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 0},
               "content": content or []}
    return line({"type": "assistant", "timestamp": stamp, "message": message})


def tool_use(name, payload):
    return {"type": "tool_use", "name": name, "input": payload}


class Pricing(unittest.TestCase):
    def test_a_dated_model_id_resolves_to_its_family(self):
        self.assertEqual(analytics.price_for("claude-haiku-4-5-20251001"), (1.0, 5.0))

    def test_the_longest_matching_prefix_wins(self):
        """claude-opus-5 must not be priced by a shorter claude-opus-4 entry."""
        self.assertEqual(analytics.price_for("claude-opus-5"), (5.0, 25.0))
        self.assertEqual(analytics.price_for("claude-sonnet-5"), (2.0, 10.0))
        self.assertEqual(analytics.price_for("claude-sonnet-4-6"), (3.0, 15.0))

    def test_an_unknown_model_has_no_rate(self):
        self.assertIsNone(analytics.price_for("<synthetic>"))
        self.assertEqual(analytics.spend_of("<synthetic>", {
            "input": 10 ** 9, "cache_write": 10 ** 9, "cache_read": 10 ** 9,
            "output": 10 ** 9}), 0.0)

    def test_cache_is_charged_at_its_own_multiple_of_input(self):
        """1M of each, on a $5 / $25 model: 5 + 6.25 + 0.50 + 25."""
        self.assertAlmostEqual(
            analytics.spend_of("claude-opus-5", {
                "input": 1_000_000, "cache_write": 1_000_000,
                "cache_read": 1_000_000, "output": 1_000_000}),
            5.0 + 6.25 + 0.50 + 25.0, places=6)

    def test_money_is_not_localised(self):
        """The spend column reproduces an export issued in US format."""
        self.assertEqual(analytics.money(17778.153), "$17,778.15")


class Counting(unittest.TestCase):
    def test_a_line_is_only_code_if_there_is_something_on_it(self):
        self.assertEqual(analytics._lines_of("a = 1\n\n  }\nb = 2\n"), 2)

    def test_a_missing_value_is_not_a_line(self):
        self.assertEqual(analytics._lines_of(None), 0)

    def test_human_count_shortens_at_each_step(self):
        self.assertEqual(analytics.human_count(999), "999")
        self.assertEqual(analytics.human_count(12_300), "12.3k")
        self.assertEqual(analytics.human_count(22_576_177_928), "22.6G")


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

    def write(self, project, session, lines, nested=None):
        folder = self.projects / project
        if nested:
            folder = folder / nested
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{session}.jsonl").write_text("".join(lines), encoding="utf-8")

    def test_an_empty_history_reports_nothing_rather_than_failing(self):
        report = analytics.build_report()
        self.assertEqual(report["sessions"], 0)
        self.assertEqual(report["totals"]["spend"], 0.0)
        self.assertEqual(analytics.verdict_key(report), "analytics.verdict.empty")
        self.assertEqual(analytics.pattern_rows(report), [])
        self.assertEqual(analytics.highlights(report), [])

    def test_only_typed_prompts_count_as_prompts(self):
        self.write("-tmp-project", "a", [
            user("write me a parser"), tool_result(), sidechain(),
            assistant(),
        ])
        report = analytics.build_report()
        self.assertEqual(report["own_messages"], 1)
        self.assertEqual(report["projects"][0]["own"], 1)
        # user turns and assistant turns both count as messages
        self.assertEqual(report["messages"], 4)

    def test_a_subagent_transcript_is_not_a_session(self):
        """Counting it would inflate every per-session figure on the page."""
        self.write("-tmp-project", "a", [user("go")])
        self.write("-tmp-project", "b", [user("go")], nested="subagents")
        report = analytics.build_report()
        self.assertEqual(report["sessions"], 1)
        self.assertEqual(report["subagent_transcripts"], 1)
        self.assertEqual(report["transcripts"], 2)
        self.assertEqual(report["projects"][0]["sessions"], 1)

    def test_usage_is_summed_per_model_and_per_month(self):
        self.write("-tmp-project", "a", [
            assistant("claude-opus-5", {
                "input_tokens": 1_000_000, "output_tokens": 1_000_000,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                stamp="2026-07-06T08:00:00.000Z"),
            assistant("claude-sonnet-5", {
                "input_tokens": 1_000_000, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                stamp="2026-08-06T08:00:00.000Z"),
        ])
        report = analytics.build_report()
        by_model = {row["model"]: row for row in report["models"]}
        self.assertAlmostEqual(by_model["claude-opus-5"]["spend"], 30.0, places=6)
        self.assertAlmostEqual(by_model["claude-sonnet-5"]["spend"], 2.0, places=6)
        self.assertAlmostEqual(report["totals"]["spend"], 32.0, places=6)
        self.assertEqual(report["totals"]["requests"], 2)
        self.assertEqual([month["month"] for month in report["months"]],
                         ["2026-07", "2026-08"])
        self.assertAlmostEqual(report["months"][0]["spend"], 30.0, places=6)

    def test_an_unpriced_model_is_named_rather_than_valued_at_zero(self):
        self.write("-tmp-project", "a", [
            assistant("<synthetic>", {"input_tokens": 5, "output_tokens": 5,
                                      "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0}),
        ])
        report = analytics.build_report()
        self.assertEqual(report["unpriced"], ["<synthetic>"])
        self.assertFalse(report["models"][0]["priced"])
        self.assertEqual(report["totals"]["tokens"], 10)

    def test_accepted_lines_come_from_the_editing_tools(self):
        """Blank lines and a lone bracket are not lines of code, see _lines_of."""
        self.write("-tmp-project", "a", [
            assistant(content=[
                tool_use("Write", {"content": "alpha = 1\nbeta = 2\n\n  }\n"}),
                tool_use("Edit", {"new_string": "gamma = 3\n"}),
                tool_use("MultiEdit",
                         {"edits": [{"new_string": "delta = 4\nepsilon = 5\n"}]}),
                tool_use("Bash", {"command": "ls"}),
            ]),
        ])
        report = analytics.build_report()
        self.assertEqual(report["loc"]["Write"], 2)
        self.assertEqual(report["loc"]["Edit"], 3)      # 1 from Edit, 2 from MultiEdit
        self.assertEqual(report["loc"]["total"], 5)
        self.assertEqual(report["tool_calls"], 4)
        self.assertEqual(dict(report["tools"])["Bash"], 1)

    def test_delegations_and_skill_names_are_collected(self):
        self.write("-tmp-project", "a", [
            assistant(content=[
                tool_use("Task", {"subagent_type": "quality"}),
                tool_use("Agent", {"subagent_type": "quality"}),
                tool_use("Skill", {"skill": "emis"}),
                tool_use("Skill", {"skill": "emis"}),
            ]),
        ])
        report = analytics.build_report()
        self.assertEqual(report["subagents"], 2)
        self.assertEqual(report["skills"], [("emis", 2)])

    def test_the_agentic_ratio_is_calls_per_typed_prompt(self):
        self.write("-tmp-project", "a", [
            user("go"), user("again"),
            assistant(content=[tool_use("Bash", {"command": "ls"})] * 10),
        ])
        report = analytics.build_report()
        self.assertEqual(report["own_messages"], 2)
        self.assertEqual(report["tool_calls"], 10)
        self.assertEqual(report["tools_per_prompt"], 5.0)

    def test_hours_and_weekdays_are_counted_in_local_time(self):
        """22:30 UTC on a Sunday in July is 00:30 Monday in Berlin."""
        self.write("-tmp-project", "a", [user("go", "2026-07-05T22:30:00.000Z")])
        report = analytics.build_report()
        self.assertEqual(report["hours"][0], 1)
        self.assertEqual(report["weekdays"][0], 1)      # Monday, not Sunday
        self.assertEqual(report["weekend"], 0)
        self.assertEqual(report["night"], 1)
        self.assertEqual(report["off_hours"], 1)
        self.assertEqual(report["first_day"], "2026-07-06")

    def test_adoption_is_active_days_over_the_span_they_cover(self):
        for day in ("2026-07-06", "2026-07-08", "2026-07-10"):
            self.write("-tmp-project", day, [user("go", f"{day}T08:00:00.000Z")])
        report = analytics.build_report()
        self.assertEqual(report["active_days"], 3)
        self.assertEqual(report["span_days"], 5)
        self.assertAlmostEqual(report["adoption"], 3 / 5)

    def test_tool_calls_are_split_by_what_they_were_for(self):
        """The ratio alone cannot say whether anyone was still looking."""
        self.write("-tmp-project", "a", [
            user("go"),
            assistant(content=[
                tool_use("Bash", {"command": "pytest"}),
                tool_use("Bash", {"command": "grep x"}),
                tool_use("Read", {"file_path": "/x"}),
                tool_use("Write", {"content": "alpha = 1\n"}),
                tool_use("Task", {"subagent_type": "quality"}),
                tool_use("AskUserQuestion", {}),
                tool_use("SomethingNew", {}),
            ]),
        ])
        report = analytics.build_report()
        mix = {entry["purpose"]: entry["calls"] for entry in report["tool_mix"]}
        self.assertEqual(mix, {"inspect": 3, "write": 1, "delegate": 1,
                               "confirm": 1, "other": 1})
        self.assertAlmostEqual(report["inspect_per_write"], 3.0)
        self.assertAlmostEqual(report["inspect_share"], 3 / 7 * 100)

    def test_an_empty_bucket_is_left_out_rather_than_shown_as_zero(self):
        self.write("-tmp-project", "a", [
            assistant(content=[tool_use("Bash", {"command": "ls"})]),
        ])
        report = analytics.build_report()
        self.assertEqual([entry["purpose"] for entry in report["tool_mix"]],
                         ["inspect"])
        self.assertEqual(report["inspect_per_write"], 0.0)

    def test_every_reading_states_both_sides(self):
        self.write("-tmp-project", "a", [
            user("go", "2026-07-04T23:30:00.000Z"),
            assistant(usage={"input_tokens": 1_000_000, "output_tokens": 0,
                             "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0},
                      content=[tool_use("Write", {"content": "alpha = 1\n"}),
                               tool_use("Skill", {"skill": "emis"})],
                      stamp="2026-07-04T23:31:00.000Z"),
        ])
        readings = analytics.readings(analytics.build_report())
        self.assertEqual([item["topic"] for item in readings],
                         ["spend", "agentic", "code", "pattern", "names"])
        for item in readings:
            self.assertTrue(item["up"]["key"].endswith(".up"))
            self.assertTrue(item["down"]["key"].endswith(".down"))

    def test_a_reading_without_data_behind_it_is_not_offered(self):
        """No skills, no weekend: those two readings would say nothing."""
        self.write("-tmp-project", "a", [
            user("go", "2026-07-06T08:00:00.000Z"),
            assistant(content=[tool_use("Write", {"content": "alpha = 1\n"})],
                      stamp="2026-07-06T08:01:00.000Z"),
        ])
        topics = [item["topic"] for item in analytics.readings(analytics.build_report())]
        self.assertNotIn("names", topics)
        self.assertNotIn("pattern", topics)
        self.assertIn("agentic", topics)

    def test_consequences_need_a_history_to_be_about(self):
        self.assertEqual(analytics.consequences(analytics.build_report()), [])
        self.write("-tmp-project", "a", [user("go")])
        steps = analytics.consequences(analytics.build_report())
        self.assertEqual([step["key"] for step in steps],
                         ["analytics.consequence.hidden",
                          "analytics.consequence.prepare",
                          "analytics.consequence.lower"])

    def test_a_weekend_and_a_night_reach_the_conclusion(self):
        self.write("-tmp-project", "a", [user("go", "2026-07-04T23:30:00.000Z")])
        report = analytics.build_report()
        self.assertEqual(analytics.verdict_key(report), "analytics.verdict.pattern")
        keys = [item["key"] for item in analytics.highlights(report)]
        self.assertIn("analytics.high.pattern", keys)

    def test_office_hours_alone_stay_the_quieter_conclusion(self):
        self.write("-tmp-project", "a", [
            user("go", "2026-07-06T08:00:00.000Z"),
            assistant(stamp="2026-07-06T08:01:00.000Z"),
        ])
        report = analytics.build_report()
        self.assertEqual(analytics.verdict_key(report), "analytics.verdict.usage")

    def test_an_unreadable_transcript_is_counted_not_swallowed(self):
        self.write("-tmp-project", "a", [user("go")])
        target = self.projects / "-tmp-project" / "a.jsonl"
        target.chmod(0o000)
        try:
            report = analytics.build_report()
        finally:
            target.chmod(0o644)
        if os.geteuid() == 0:                      # root reads it regardless
            self.skipTest("running as root, permissions do not apply")
        self.assertEqual(report["unreadable"], 1)

    def test_a_damaged_line_does_not_take_the_report_down(self):
        self.write("-tmp-project", "a", [
            '{"type":"assistant","timestamp":"2026-07-06T08:00:00.000Z"\n',
            assistant(),
        ])
        report = analytics.build_report()
        self.assertEqual(report["assistant_messages"], 1)


if __name__ == "__main__":
    unittest.main()
