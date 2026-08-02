"""Language files must stay in step with the code.

A missing key degrades to English, a mismatched placeholder degrades to the
unformatted template -- neither crashes, but both are bugs worth catching here
rather than in front of a user.
"""

import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / "claude_privacy_check"
LOCALES = PKG / "locales"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Keys assembled at runtime rather than written as a literal inside t(...).
DYNAMIC_KEYS = (
    {f"severity.{s}" for s in ("CRITICAL", "HIGH", "MEDIUM", "INFO")}
    | {f"status.{s}" for s in ("OK", "CHANGED", "CRITICAL", "NO_BASELINE")}
    | {f"status.detail.{s}" for s in ("OK", "CHANGED", "CRITICAL", "NO_BASELINE")}
)


def literal_keys():
    """Every t("literal") in the package."""
    found = set()
    for path in PKG.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "t" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                found.add(node.args[0].value)
    return found


def finding_keys():
    """finding.<name>.title/.detail for every finding the assessment can emit."""
    source = (PKG / "core.py").read_text(encoding="utf-8")
    names = set(re.findall(r'"finding\.(\w+)"', source))
    return {f"finding.{n}.{suffix}" for n in names for suffix in ("title", "detail")}


def store_keys():
    from claude_privacy_check.data import DATA_STORES
    return {f"{label}.{suffix}" for _, label in DATA_STORES
            for suffix in ("name", "desc")}


def error_keys():
    source = (PKG / "data.py").read_text(encoding="utf-8")
    return set(re.findall(r'NotDeletable\("([\w.]+)"', source))


def required_keys():
    return literal_keys() | DYNAMIC_KEYS | finding_keys() | store_keys() | error_keys()


def load(code):
    return json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))


class Locales(unittest.TestCase):
    def setUp(self):
        self.catalogs = {p.stem: load(p.stem) for p in sorted(LOCALES.glob("*.json"))}
        self.assertIn("en", self.catalogs, "English is the fallback and must exist")

    def test_every_catalog_has_a_label(self):
        for code, catalog in self.catalogs.items():
            self.assertTrue(catalog.get("_label"), f"{code}.json needs a _label")

    def test_english_covers_every_key_used_in_code(self):
        missing = sorted(required_keys() - set(self.catalogs["en"]))
        self.assertEqual(missing, [], f"missing from en.json: {missing}")

    def test_translations_cover_the_english_keys(self):
        english = set(self.catalogs["en"]) - {"_label"}
        for code, catalog in self.catalogs.items():
            if code == "en":
                continue
            missing = sorted(english - set(catalog))
            self.assertEqual(missing, [], f"missing from {code}.json: {missing}")

    def test_no_stray_keys_in_translations(self):
        english = set(self.catalogs["en"])
        for code, catalog in self.catalogs.items():
            if code == "en":
                continue
            stray = sorted(set(catalog) - english)
            self.assertEqual(stray, [], f"unknown keys in {code}.json: {stray}")

    def test_placeholders_match_across_languages(self):
        english = self.catalogs["en"]
        for code, catalog in self.catalogs.items():
            if code == "en":
                continue
            for key, text in catalog.items():
                if key not in english:
                    continue
                self.assertEqual(
                    set(PLACEHOLDER.findall(text)),
                    set(PLACEHOLDER.findall(english[key])),
                    f"{code}.json[{key}] placeholders differ from English")

    def test_fallback_returns_key_when_unknown(self):
        from claude_privacy_check.i18n import t
        self.assertEqual(t("no.such.key.exists"), "no.such.key.exists")

    def test_switching_language_changes_output(self):
        from claude_privacy_check import i18n
        before = i18n.current_language()
        try:
            i18n.set_language("en")
            english = i18n.t("btn.delete")
            i18n.set_language("de")
            german = i18n.t("btn.delete")
            self.assertNotEqual(english, german)
        finally:
            i18n.set_language(before)

    def test_unknown_language_keeps_the_current_one(self):
        from claude_privacy_check import i18n
        before = i18n.current_language()
        i18n.set_language("zz")
        self.assertEqual(i18n.current_language(), before)


class ObserverPatterns(unittest.TestCase):
    """The low-confidence patterns are matched against a lowercased buffer.

    They must therefore contain no uppercase *literal*. Escape sequences are
    exempt -- \\S is case-independent and must survive. That distinction is
    exactly why the patterns are not lowercased programmatically: doing so
    would turn \\S into \\s and silently change what they match.
    """

    ESCAPE = re.compile(r"\\[a-zA-Z]")

    def test_low_confidence_patterns_have_no_uppercase_literal(self):
        from claude_privacy_check.observer import CATEGORIES
        for slug, _key, confidence, patterns in CATEGORIES:
            if confidence != "low":
                continue
            for pattern in patterns:
                literals = self.ESCAPE.sub("", pattern)
                self.assertEqual(
                    literals, literals.lower(),
                    f"{slug}: {pattern!r} has an uppercase literal and would "
                    f"never match the lowercased buffer")

    def test_the_escape_exemption_is_real(self):
        """Guards the guard: \\S must not be reported as uppercase."""
        self.assertEqual(self.ESCAPE.sub("", r"\bfoo\s*[:=]\s*\S"), r"foo*[:=]*")

    def test_every_category_name_is_translated(self):
        from claude_privacy_check.observer import CATEGORIES
        english = load("en")
        for _slug, key, _conf, _patterns in CATEGORIES:
            self.assertIn(key, english)

    def test_both_regexes_compile(self):
        from claude_privacy_check.observer import _combined
        cased, folded = _combined()
        self.assertTrue(cased.groupindex)
        self.assertTrue(folded.groupindex)


if __name__ == "__main__":
    unittest.main()


class NoPersonalStateInCheckout(unittest.TestCase):
    """The checkout must never become a place where personal state lives.

    An earlier version kept the baseline in ``data/`` next to the code, which
    put an account e-mail, an organisation id and every watched path inside a
    directory people clone.
    """

    def test_baseline_is_outside_the_project(self):
        from claude_privacy_check import core
        self.assertFalse(
            core.BASELINE.startswith(core.APP_DIR + "/"),
            f"baseline must not live in the checkout: {core.BASELINE}")

    def test_baseline_lands_under_a_data_home(self):
        from claude_privacy_check import core
        self.assertIn("claude-privacy-check", core.BASELINE)
        self.assertTrue(core.BASELINE.endswith("baseline.json"))

    def test_a_legacy_baseline_is_migrated_away(self):
        from claude_privacy_check import core
        self.assertTrue(hasattr(core, "migrate_legacy_baseline"))
        self.assertTrue(core.LEGACY_BASELINE.startswith(core.APP_DIR))
