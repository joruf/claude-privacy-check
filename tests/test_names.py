"""Names and paths: the two things a session carries that nobody typed.

This view makes a claim that is easy to get wrong in either direction, so the
extraction is pinned: a tab name is found because it is a record *inside* the
transcript, an opened-file path is found because it is part of the prompt text,
and a file whose name alone gives its kind away is marked as such without the
contents ever being read.

The fixtures write the format Claude Code actually writes, compact JSON with no
spaces after the colons, because the scan classifies a line by its raw bytes.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check import data, names  # noqa: E402

COMPACT = {"separators": (",", ":")}


def line(record):
    return json.dumps(record, **COMPACT) + "\n"


def title(name, session="s1"):
    return line({"type": "custom-title", "sessionId": session, "customTitle": name})


def agent(name, session="s1"):
    return line({"type": "agent-name", "sessionId": session, "agentName": name})


def opened(path, stamp="2026-07-06T08:00:00.000Z"):
    """A prompt with the line the editor extension appends to it."""
    body = (f"<ide_opened_file>The user opened the file {path} in the IDE. "
            "This may or may not be related to the current task.</ide_opened_file>"
            "do the thing")
    return line({"type": "user", "timestamp": stamp,
                 "message": {"role": "user", "content": body}})


class Sensitive(unittest.TestCase):
    """A hit says what kind of file it was, never what was in it."""

    def test_the_usual_credential_files_are_recognised(self):
        for path in ("/home/x/project/.env", "/home/x/.env.local",
                     "/home/x/.ssh/id_rsa", "/home/x/certs/server.pem",
                     "/home/x/app/credentials.json", "/home/x/secrets.yml",
                     "/home/x/.netrc", "/home/x/store.kdbx",
                     "/home/x/config/token.json", "/home/x/.npmrc"):
            self.assertTrue(names.is_sensitive(path), path)

    def test_ordinary_source_files_are_not(self):
        """A noun has to begin a path segment, or the list flags half a repo."""
        for path in ("/home/x/src/Controller.php", "/home/x/docs/ROADMAP.md",
                     "/home/x/assets/theme.css", "/home/x/environment.md",
                     "/home/x/src/Keyboard.tsx", "/home/x/src/TokenParser.php",
                     "/home/x/src/PasswordField.tsx"):
            self.assertFalse(names.is_sensitive(path), path)


class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)
        self.original = data.PROJECTS_DIR
        data.PROJECTS_DIR = str(self.projects)

    def tearDown(self):
        data.PROJECTS_DIR = self.original
        self.tmp.cleanup()

    def write(self, project, session, lines):
        folder = self.projects / project
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{session}.jsonl").write_text("".join(lines), encoding="utf-8")

    def test_an_empty_history_finds_nothing_rather_than_failing(self):
        report = names.build_report()
        self.assertEqual(report["titles"], [])
        self.assertEqual(report["opened"], [])
        self.assertEqual(names.verdict_key(report), "names.verdict.clean")

    def test_a_tab_name_is_read_out_of_the_transcript_itself(self):
        self.write("-tmp-project", "a", [title("Voice extractor"),
                                         agent("Voice extractor")])
        report = names.build_report()
        self.assertEqual([entry["title"] for entry in report["titles"]],
                         ["Voice extractor"])
        self.assertEqual(report["title_transcripts"], 1)
        self.assertEqual(report["titles"][0]["sessions"], 1)

    def test_the_same_name_in_two_sessions_is_one_row(self):
        self.write("-tmp-project", "a", [title("argus", "s1")])
        self.write("-tmp-project", "b", [title("argus", "s2")])
        report = names.build_report()
        self.assertEqual(len(report["titles"]), 1)
        self.assertEqual(report["titles"][0]["sessions"], 2)
        self.assertEqual(report["title_transcripts"], 2)

    def test_names_are_listed_alphabetically_and_case_is_not_a_group(self):
        for index, name in enumerate(("zeta", "Alpha", "alpha")):
            self.write("-tmp-project", f"s{index}", [title(name, f"s{index}")])
        report = names.build_report()
        self.assertEqual([entry["title"] for entry in report["titles"]],
                         ["Alpha", "alpha", "zeta"])

    def test_an_opened_file_path_is_read_out_of_the_prompt(self):
        self.write("-tmp-project", "a", [
            opened("/home/joruf/Dokumente/pmtool/.env"),
            opened("/home/joruf/Dokumente/pmtool/.env"),
            opened("/home/joruf/src/app.php"),
        ])
        report = names.build_report()
        by_path = {entry["path"]: entry for entry in report["opened"]}
        self.assertEqual(by_path["/home/joruf/Dokumente/pmtool/.env"]["count"], 2)
        self.assertEqual(by_path["/home/joruf/src/app.php"]["count"], 1)
        self.assertEqual(report["opened_total"], 3)
        self.assertEqual(report["opened_transcripts"], 1)

    def test_a_credentials_path_sorts_first_and_reaches_the_conclusion(self):
        """Loudest first: the row that matters must not be below the noise."""
        self.write("-tmp-project", "a",
                   [opened("/home/joruf/src/app.php")] * 9
                   + [opened("/home/joruf/Dokumente/pmtool/.env")])
        report = names.build_report()
        self.assertEqual(report["opened"][0]["path"],
                         "/home/joruf/Dokumente/pmtool/.env")
        self.assertTrue(report["opened"][0]["sensitive"])
        self.assertEqual(len(report["sensitive"]), 1)
        self.assertEqual(names.verdict_key(report), "names.verdict.sensitive")

    def test_names_without_a_credentials_path_stay_the_quieter_conclusion(self):
        self.write("-tmp-project", "a", [title("argus"),
                                         opened("/home/joruf/src/app.php")])
        self.assertEqual(names.verdict_key(names.build_report()),
                         "names.verdict.found")

    def test_a_damaged_title_line_does_not_take_the_scan_down(self):
        self.write("-tmp-project", "a", [
            '{"type":"custom-title","customTitle":"broken\n',
            title("intact"),
        ])
        report = names.build_report()
        self.assertEqual([entry["title"] for entry in report["titles"]], ["intact"])

    def test_an_empty_title_is_not_a_name(self):
        self.write("-tmp-project", "a", [title("   "), title("real")])
        report = names.build_report()
        self.assertEqual([entry["title"] for entry in report["titles"]], ["real"])

    def test_deleting_an_opened_path_never_targets_the_file_itself(self):
        """The row names /home/.../.env. What it may remove is the transcript.

        Getting this backwards would have the button delete the user's own
        credentials file, so it is pinned rather than trusted.
        """
        self.write("-tmp-project", "a", [opened("/home/joruf/Dokumente/pmtool/.env")])
        report = names.build_report()
        entry = report["opened"][0]
        self.assertEqual(entry["path"], "/home/joruf/Dokumente/pmtool/.env")
        removable = names.removable(entry)
        self.assertNotIn("/home/joruf/Dokumente/pmtool/.env", removable)
        self.assertEqual(removable,
                         [str(self.projects / "-tmp-project" / "a.jsonl")])

    def test_a_name_offers_its_transcripts_and_the_file_beside_them(self):
        side = self.projects / "-tmp-project" / "a" / "custom-title.json"
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text('{"customTitle":"argus"}', encoding="utf-8")
        self.write("-tmp-project", "a", [title("argus")])
        self.write("-tmp-project", "b", [title("argus")])
        entry = names.build_report()["titles"][0]
        self.assertEqual(entry["files"],
                         [str(self.projects / "-tmp-project" / "a.jsonl"),
                          str(self.projects / "-tmp-project" / "b.jsonl")])
        self.assertEqual(entry["extra"], [str(side)])
        self.assertEqual(len(names.removable(entry)), 3)

    def test_removable_is_empty_for_a_row_that_carries_nothing(self):
        self.assertEqual(names.removable({}), [])

    def test_a_running_session_is_flagged_rather_than_silently_deleted(self):
        self.write("-tmp-project", "live", [title("argus")])
        original = names.active_session_ids
        names.active_session_ids = lambda: {"live"}
        try:
            report = names.build_report()
        finally:
            names.active_session_ids = original
        self.assertTrue(report["titles"][0]["active"])

    def test_places_name_all_four_and_only_the_first_travels(self):
        self.write("-tmp-project", "a", [title("argus")])
        places = names.places(names.build_report())
        self.assertEqual([place["place"] for place in places],
                         ["transcript", "title_file", "session", "ide_log"])
        self.assertEqual([place["travels"] for place in places],
                         [True, False, False, False])


if __name__ == "__main__":
    unittest.main()
