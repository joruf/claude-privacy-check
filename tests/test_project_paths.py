"""Decoding the encoded project directory names.

Claude Code writes the working path as '-home-user-Documents-project'. Hyphens
inside a folder name are indistinguishable from separators, so the decoder has
to resolve against the real filesystem.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check.data import decode_project_path  # noqa: E402


class DecodeProjectPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def encoded(self, *parts):
        """Encode an absolute path the way Claude Code does."""
        full = self.root.joinpath(*parts)
        return str(full).replace("/", "-")

    def test_plain_path(self):
        target = self.root / "Documents" / "project"
        target.mkdir(parents=True)
        self.assertEqual(decode_project_path(self.encoded("Documents", "project")),
                         str(target))

    def test_hyphen_in_folder_name_stays_one_level(self):
        """The regression this decoder exists for."""
        target = self.root / "Documents" / "sensor-control-v2"
        target.mkdir(parents=True)
        decoded = decode_project_path(self.encoded("Documents", "sensor-control-v2"))
        self.assertEqual(decoded, str(target))
        self.assertNotIn("sensor/control", decoded)

    def test_nested_path_with_hyphens_at_two_levels(self):
        target = self.root / "my-docs" / "sub-project"
        target.mkdir(parents=True)
        self.assertEqual(decode_project_path(self.encoded("my-docs", "sub-project")),
                         str(target))

    def test_deleted_project_degrades_gracefully(self):
        """A project removed from disk must not raise -- it just cannot resolve."""
        decoded = decode_project_path(self.encoded("Documents", "gone-forever"))
        self.assertTrue(decoded.startswith("/"))
        self.assertIn("gone", decoded)


if __name__ == "__main__":
    unittest.main()
