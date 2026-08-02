"""User-level install: pending detection and ensure()."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from claude_privacy_check import install


class InstallPending(unittest.TestCase):
    def test_pending_when_nothing_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = tmp
            with mock.patch.object(install, "HOME", home), \
                 mock.patch.object(install, "BIN_DIR", os.path.join(home, "bin")), \
                 mock.patch.object(install, "LINK", os.path.join(home, "bin", "app")), \
                 mock.patch.object(install, "DESKTOP_DIR",
                                   os.path.join(home, "applications")), \
                 mock.patch.object(install, "DESKTOP_FILE",
                                   os.path.join(home, "applications", "app.desktop")), \
                 mock.patch.object(install, "HICOLOR",
                                   os.path.join(home, "icons", "hicolor")):
                steps = install.pending_steps()
        ids = [s[0] for s in steps]
        self.assertEqual(ids, ["link", "icons", "desktop"])

    def test_ensure_creates_link_and_desktop(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = tmp
            bin_dir = os.path.join(home, "bin")
            link = os.path.join(bin_dir, "claude-privacy-check")
            desktop_dir = os.path.join(home, "applications")
            desktop_file = os.path.join(desktop_dir, "app.desktop")
            hicolor = os.path.join(home, "icons", "hicolor")
            messages = []
            with mock.patch.object(install, "HOME", home), \
                 mock.patch.object(install, "BIN_DIR", bin_dir), \
                 mock.patch.object(install, "LINK", link), \
                 mock.patch.object(install, "DESKTOP_DIR", desktop_dir), \
                 mock.patch.object(install, "DESKTOP_FILE", desktop_file), \
                 mock.patch.object(install, "HICOLOR", hicolor):
                done = install.ensure(progress=messages.append, force=True)
                self.assertFalse(install.needs_install())
            self.assertEqual(done, ["link", "icons", "desktop"])
            self.assertTrue(os.path.islink(link))
            self.assertTrue(os.path.isfile(desktop_file))
            self.assertTrue(any("link" in m.lower() or "Symlink" in m or "symlink" in m
                                or "Befehl" in m or "command" in m.lower()
                                for m in messages))

    def test_prepare_launch_gui_defers(self):
        with mock.patch.object(install, "pending_steps",
                               return_value=[("link", "install.step.link")]), \
             mock.patch.object(install, "ensure") as ensure_mock:
            pending = install.prepare_launch([])
            self.assertEqual(pending, [("link", "install.step.link")])
            ensure_mock.assert_not_called()
            taken = install.take_gui_pending()
            self.assertEqual(taken, [("link", "install.step.link")])

    def test_prepare_launch_cli_installs_quietly(self):
        with mock.patch.object(install, "pending_steps",
                               return_value=[("link", "install.step.link")]), \
             mock.patch.object(install, "ensure") as ensure_mock:
            pending = install.prepare_launch(["--cli"])
            self.assertEqual(pending, [])
            ensure_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
