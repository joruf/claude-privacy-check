"""Unit tests for About constants, URL normalisation and dialog content."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_privacy_check import __version__                      # noqa: E402
from claude_privacy_check.about import (                          # noqa: E402
    ABOUT_AUTHOR, ABOUT_GITHUB, ABOUT_WEBSITE, APP_LICENSE, APP_NAME,
    about_rows, build_about_text, normalize_about_url,
)


class AboutConstants(unittest.TestCase):
    def test_normalize_adds_https_when_missing(self):
        self.assertEqual(normalize_about_url("loresoft.de"), "https://loresoft.de")

    def test_normalize_keeps_an_existing_scheme(self):
        for url in ("https://example.org", "http://example.org"):
            self.assertEqual(normalize_about_url(url), url)

    def test_normalize_trims_whitespace(self):
        self.assertEqual(normalize_about_url("  loresoft.de  "), "https://loresoft.de")

    def test_normalize_handles_empty_input(self):
        self.assertEqual(normalize_about_url("   "), "")

    def test_version_matches_the_package(self):
        from claude_privacy_check.about import APP_VERSION
        self.assertEqual(APP_VERSION, __version__)

    def test_licence_matches_pyproject(self):
        pyproject = (Path(__file__).resolve().parent.parent
                     / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(APP_LICENSE, pyproject)


class AboutRows(unittest.TestCase):
    def test_rows_cover_author_website_and_repository(self):
        values = [value for _key, value, _link in about_rows()]
        self.assertIn(ABOUT_AUTHOR, values)
        self.assertIn(ABOUT_WEBSITE, values)
        self.assertIn(ABOUT_GITHUB, values)

    def test_link_rows_are_absolute_urls(self):
        for _key, _value, link in about_rows():
            if link:
                self.assertTrue(link.startswith("https://"), link)

    def test_every_row_label_is_translated(self):
        import json
        locales = (Path(__file__).resolve().parent.parent
                   / "claude_privacy_check" / "locales")
        for code in ("en", "de"):
            catalog = json.loads((locales / f"{code}.json").read_text(encoding="utf-8"))
            for key, _value, _link in about_rows():
                self.assertIn(key, catalog, f"{key} missing from {code}.json")

    def test_text_rendering_contains_name_and_links(self):
        text = build_about_text()
        self.assertIn(APP_NAME, text)
        self.assertIn(ABOUT_AUTHOR, text)
        self.assertIn(ABOUT_GITHUB, text)


if __name__ == "__main__":
    unittest.main()
