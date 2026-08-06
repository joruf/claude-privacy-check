"""Writing an instruction file back is the one place this program touches
somebody's content, so the write path gets its own tests.

Everything here works on throwaway paths; nothing touches a real ~/.claude.
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check import instructions  # noqa: E402


class Describe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name="CLAUDE.md", content="hello\n"):
        path = self.dir / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_a_writable_markdown_file_is_editable(self):
        entry = instructions._describe(str(self._file()), "user", "instructions")
        self.assertTrue(entry["editable"])
        self.assertIsNone(entry["locked"])

    def test_a_read_only_file_says_why(self):
        path = self._file()
        os.chmod(path, stat.S_IRUSR)
        try:
            entry = instructions._describe(str(path), "user", "instructions")
        finally:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self.assertFalse(entry["editable"])
        self.assertEqual(entry["locked"], "permission")

    @unittest.skipIf(os.geteuid() == 0, "root ignores the write bit")
    def test_root_would_skew_the_permission_test(self):
        """Guards the guard above -- as root every file looks writable."""
        self.assertNotEqual(os.geteuid(), 0)

    def test_an_oversized_file_is_preview_only(self):
        path = self._file(content="x" * (instructions.MAX_EDIT_BYTES + 1))
        entry = instructions._describe(str(path), "user", "instructions")
        self.assertEqual(entry["locked"], "too_large")

    def test_organisation_pushed_instructions_are_never_editable(self):
        report = instructions.collect([])
        for entry in report["entries"]:
            if entry["origin"] == "org":
                self.assertFalse(entry["editable"])
                self.assertEqual(entry["locked"], "org")


class SaveText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "CLAUDE.md"
        self.path.write_text("first line\nsecond line\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_is_exact(self):
        text = instructions.read_text(str(self.path))
        instructions.save_text(str(self.path), text)
        self.assertEqual(self.path.read_text(encoding="utf-8"), text)

    def test_writes_the_new_content(self):
        instructions.save_text(str(self.path), "rewritten\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "rewritten\n")

    def test_keeps_the_file_mode(self):
        os.chmod(self.path, 0o640)
        instructions.save_text(str(self.path), "changed\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_leaves_no_temporary_behind(self):
        instructions.save_text(str(self.path), "changed\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["CLAUDE.md"])

    def test_a_symlink_keeps_pointing_at_its_target(self):
        """Dotfiles are often symlinked into a checkout. Replacing the link
        with a regular file would quietly detach it from the original."""
        link = self.dir / "linked.md"
        link.symlink_to(self.path)
        instructions.save_text(str(link), "through the link\n")
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "through the link\n")

    def test_refuses_a_path_that_is_not_a_file(self):
        with self.assertRaises(OSError):
            instructions.save_text(str(self.dir), "nope")
        with self.assertRaises(OSError):
            instructions.save_text(str(self.dir / "absent.md"), "nope")

    def test_returns_the_new_mtime(self):
        mtime = instructions.save_text(str(self.path), "changed\n")
        self.assertEqual(mtime, os.path.getmtime(self.path))

    def test_reading_refuses_to_guess_at_broken_encoding(self):
        """The preview decodes with replacements; an editor must not, or
        saving would write those replacements back over the original bytes."""
        self.path.write_bytes(b"caf\xe9 latte\n")
        with self.assertRaises(UnicodeDecodeError):
            instructions.read_text(str(self.path))

    def test_restat_updates_size_and_stays_on_the_same_entry(self):
        entry = instructions._describe(str(self.path), "user", "instructions")
        entry["name"] = "skill/SKILL.md"          # a skill carries its directory
        instructions.save_text(str(self.path), "a much longer body than before\n")
        instructions.restat(entry)
        self.assertEqual(entry["bytes"], os.path.getsize(self.path))
        self.assertEqual(entry["preview"], "a much longer body than before\n")
        self.assertEqual(entry["name"], "skill/SKILL.md")


if __name__ == "__main__":
    unittest.main()
