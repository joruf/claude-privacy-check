"""The deletion guards are the part that must never be wrong.

Everything here operates on throwaway paths created for the test; nothing
touches real history.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check import data  # noqa: E402


class DeleteGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claude = Path(self.tmp.name) / ".claude"
        (self.claude / "projects").mkdir(parents=True)
        self._real_dir = data.CLAUDE_DIR
        data.CLAUDE_DIR = str(self.claude)

    def tearDown(self):
        data.CLAUDE_DIR = self._real_dir
        self.tmp.cleanup()

    def _make(self, relative, content="x"):
        path = self.claude / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)

    # ------------------------------------------------------------ rejects

    def test_rejects_claude_dir_itself(self):
        with self.assertRaises(data.NotDeletable) as ctx:
            data.check_deletable(str(self.claude))
        self.assertEqual(ctx.exception.key, "delete.err.root")

    def test_rejects_path_outside(self):
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        with self.assertRaises(data.NotDeletable) as ctx:
            data.check_deletable(str(outside))
        self.assertEqual(ctx.exception.key, "delete.err.outside")

    def test_rejects_parent_traversal(self):
        """`..` must not lead out of ~/.claude."""
        escape = str(self.claude / ".." / "elsewhere")
        (Path(self.tmp.name) / "elsewhere").mkdir()
        with self.assertRaises(data.NotDeletable) as ctx:
            data.check_deletable(escape)
        self.assertEqual(ctx.exception.key, "delete.err.outside")

    def test_rejects_symlink_escape(self):
        """A symlink inside ~/.claude pointing out of it must not be followed."""
        target = Path(self.tmp.name) / "outside-target"
        target.mkdir()
        link = self.claude / "sneaky"
        link.symlink_to(target)
        with self.assertRaises(data.NotDeletable) as ctx:
            data.check_deletable(str(link))
        self.assertEqual(ctx.exception.key, "delete.err.outside")

    def test_rejects_protected_files(self):
        for name in sorted(data.PROTECTED_NAMES):
            path = self._make(name, "{}")
            with self.assertRaises(data.NotDeletable, msg=name) as ctx:
                data.check_deletable(path)
            self.assertEqual(ctx.exception.key, "delete.err.protected", name)

    def test_rejects_missing(self):
        with self.assertRaises(data.NotDeletable) as ctx:
            data.check_deletable(str(self.claude / "does-not-exist"))
        self.assertEqual(ctx.exception.key, "delete.err.missing")

    # ------------------------------------------------------------ accepts

    def test_accepts_transcript(self):
        path = self._make("projects/-a-b/session.jsonl")
        self.assertEqual(data.check_deletable(path), os.path.realpath(path))

    def test_accepts_store_directory(self):
        self._make("file-history/old.txt")
        target = str(self.claude / "file-history")
        self.assertEqual(data.check_deletable(target), os.path.realpath(target))

    # ------------------------------------------------------------ deleting

    def test_deletes_file_and_directory(self):
        single = self._make("projects/-a-b/one.jsonl")
        self._make("file-history/deep/nested.txt")
        store = str(self.claude / "file-history")

        deleted, errors = data.delete_paths([single, store])
        self.assertEqual((deleted, errors), (2, []))
        self.assertFalse(os.path.exists(single))
        self.assertFalse(os.path.exists(store))

    def test_mixed_input_deletes_valid_and_reports_rest(self):
        """One valid target plus two forbidden ones: the valid one still goes."""
        valid = self._make("projects/-a-b/one.jsonl")
        protected = self._make("settings.json", "{}")

        deleted, errors = data.delete_paths([valid, protected, "/etc/passwd"])
        self.assertEqual(deleted, 1)
        self.assertEqual(len(errors), 2)
        self.assertFalse(os.path.exists(valid))
        self.assertTrue(os.path.exists(protected))
        self.assertTrue(os.path.exists("/etc/passwd"))


if __name__ == "__main__":
    unittest.main()
