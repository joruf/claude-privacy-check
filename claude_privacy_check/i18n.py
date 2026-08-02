"""Translation layer.

Language files are plain JSON in ``claude_privacy_check/locales``. Adding a
language means dropping another file in there -- no code change. English is the
fallback for any key a translation happens to be missing, so a partial file is
still usable.

The default is English regardless of the system locale; the choice is persisted
in ``~/.config/claude-privacy-check/config.json`` and can be overridden per run
with ``--language``.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DEFAULT_LANGUAGE = "en"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"
CONFIG_PATH = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "claude-privacy-check" / "config.json"


class Translator:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._language = DEFAULT_LANGUAGE
        self._fallback: dict[str, str] = self._read(DEFAULT_LANGUAGE)
        self._catalog: dict[str, str] = dict(self._fallback)

    # ------------------------------------------------------------- loading

    @staticmethod
    def _read(code: str) -> dict[str, str]:
        path = LOCALES_DIR / f"{code}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, str)}

    def available(self) -> list[tuple[str, str]]:
        """(code, display label) for every language file that parses."""
        found: list[tuple[str, str]] = []
        for path in sorted(LOCALES_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            found.append((path.stem, data.get("_label", path.stem.upper())))
        return found or [(DEFAULT_LANGUAGE, "English")]

    @property
    def language(self) -> str:
        with self._lock:
            return self._language

    def set_language(self, code: str) -> None:
        catalog = self._read(code)
        with self._lock:
            if not catalog and code != DEFAULT_LANGUAGE:
                return
            self._language = code
            self._catalog = catalog

    # --------------------------------------------------------- translation

    def get(self, key: str, **params) -> str:
        with self._lock:
            template = self._catalog.get(key) or self._fallback.get(key)
        if template is None:
            return key
        if not params:
            return template
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            # A malformed placeholder must not take the output down.
            return template


TRANSLATOR = Translator()


def t(key: str, **params) -> str:
    return TRANSLATOR.get(key, **params)


def set_language(code: str) -> None:
    TRANSLATOR.set_language(code)


def current_language() -> str:
    return TRANSLATOR.language


def available_languages() -> list[tuple[str, str]]:
    return TRANSLATOR.available()


# ------------------------------------------------------------ persistence

def load_preference() -> str:
    """Language from the config file, or the default."""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_LANGUAGE
    code = data.get("language")
    return code if isinstance(code, str) and code else DEFAULT_LANGUAGE


def save_preference(code: str) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except ValueError:
                data = {}
        data["language"] = code
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def apply_startup_language(override: str | None = None) -> str:
    """Explicit override wins, otherwise the stored preference, otherwise English."""
    code = override or load_preference()
    set_language(code)
    if override:
        save_preference(current_language())
    return current_language()


class Msg:
    """A deferred translation.

    Findings carry these instead of finished strings, so the same result object
    renders correctly in whatever language the output layer is using.
    """

    __slots__ = ("key", "params")

    def __init__(self, key: str, **params) -> None:
        self.key = key
        self.params = params

    def __str__(self) -> str:
        return t(self.key, **self.params)

    def __repr__(self) -> str:
        return f"Msg({self.key!r}, {self.params!r})"
