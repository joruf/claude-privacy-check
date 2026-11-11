"""The single-file executable: its name, its desktop integration, its build and its workflow.

The executable itself is never started here - ``build-exe.py`` does that after every build.
These tests pretend to be frozen and check that nothing in that mode points at files that only
exist in a checkout or inside the unpacked archive, and that the build script and the workflow
agree on names.
"""

import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from claude_privacy_check import __build__, __version__, install, watch

ROOT = Path(__file__).resolve().parent.parent

#: How the Loresoft site reads a release asset: <app>-<os>-<arch>-<version>[-build<build>][.exe]
ASSET_PATTERN = re.compile(
    r"^claude-privacy-check-(linux|windows)-[a-z0-9_]+-\d+\.\d+\.\d+(-build\d+)?(\.exe)?$")


def _build_script():
    """Import ``build-exe.py`` (its name is not a module name)."""
    spec = importlib.util.spec_from_file_location("build_exe", str(ROOT / "build-exe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExecutableName(unittest.TestCase):
    def setUp(self):
        self.build = _build_script()

    def test_the_name_carries_platform_and_version(self):
        for machine, expected in (("x86_64", "claude-privacy-check-linux-x86_64-1.1.0-build3"),
                                  ("aarch64", "claude-privacy-check-linux-aarch64-1.1.0-build3"),
                                  ("AMD64", "claude-privacy-check-linux-x86_64-1.1.0-build3")):
            with self.subTest(machine=machine), \
                    mock.patch.object(self.build.platform, "machine", return_value=machine):
                name = self.build.executable_name("1.1.0", 3)
                self.assertEqual(name, expected)
                self.assertRegex(name, ASSET_PATTERN)

    def test_the_name_defaults_to_this_version(self):
        self.assertIn("-{0}-build{1}".format(__version__, __build__), self.build.executable_name())


class FrozenDesktopIntegration(unittest.TestCase):
    """A file has no run.py to link to; it must neither offer nor create such links."""

    def test_the_executable_has_nothing_to_install(self):
        with mock.patch.object(install, "IS_FROZEN", True), \
                mock.patch.object(install, "_install_link", side_effect=AssertionError("no link")), \
                mock.patch.object(install, "_install_desktop", side_effect=AssertionError("no entry")):
            self.assertEqual(install.pending_steps(), [])
            self.assertEqual(install.ensure(force=True), [])

    def test_the_timer_unit_starts_the_executable_itself(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", "/opt/cpc/claude-privacy-check-linux-x86_64-1.1.0-build3"):
            self.assertEqual(watch.entry_command(), "/opt/cpc/claude-privacy-check-linux-x86_64-1.1.0-build3")

    def test_a_checkout_keeps_its_old_behaviour(self):
        with mock.patch.object(watch.shutil, "which", return_value=None):
            self.assertTrue(watch.entry_command().endswith("run.py"))


class Build(unittest.TestCase):
    def setUp(self):
        self.build = _build_script()

    def test_the_spec_carries_translations_and_icons(self):
        text = self.build.spec_text()
        self.assertIn("locales", text)
        self.assertIn("icons", text)
        self.assertIn("claude_privacy_check", text)

    def test_the_entry_script_compiles_and_gives_children_the_system_library_path(self):
        template = self.build.ENTRY_TEMPLATE
        compile(template, "entry.py", "exec")
        namespace = {}
        exec(template.split("if getattr(sys")[0], namespace)
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI1", "LD_LIBRARY_PATH_ORIG": "/usr/lib/x"}):
            namespace["use_system_library_path"]()
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/usr/lib/x")
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI1"}):
            os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
            namespace["use_system_library_path"]()
            self.assertNotIn("LD_LIBRARY_PATH", os.environ)

    def test_every_data_directory_exists(self):
        for source, _target in self.build.DATA_FILES:
            self.assertTrue((ROOT / source).is_dir(), source)

    def test_build_output_is_ignored_by_git(self):
        lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("build/", lines)
        self.assertIn("dist/", lines)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "release-exe.yml").read_text(encoding="utf-8")

    def test_it_builds_on_the_oldest_supported_ubuntu(self):
        self.assertIn("ubuntu-22.04", self.text)

    def test_the_release_is_tagged_with_version_and_build(self):
        self.assertIn('TAG="v${VERSION}-build${BUILD}"', self.text)
        self.assertIn("build-exe.py --print-version", self.text)

    def test_it_uploads_what_the_build_writes(self):
        self.assertIn("dist/claude-privacy-check-*", self.text)


if __name__ == "__main__":
    unittest.main()
