"""Minimal locale helper for GccSlim/GccSlim.

The first i18n pass intentionally keeps the surface small: it provides a
stable translation loader, language preference helpers, and a `tr()` function
that can be adopted gradually by UI modules. Runtime logic must not depend on
translated strings.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

SUPPORTED_LANGUAGES = ("en", "ko")
DEFAULT_LANGUAGE = "en"
LANGUAGE_PREF_KEY = "ui_language"


def _script_root() -> Path:
    try:
        return Path(__file__).resolve().parents[1]
    except Exception:
        return Path.cwd()


def _candidate_i18n_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.environ.get("GCCFORK_I18N_DIR") or os.environ.get("GCCSLIM_I18N_DIR")
    if env_dir:
        dirs.append(Path(env_dir).expanduser())
    root = _script_root()
    dirs.extend(
        [
            root / "share" / "i18n",
            Path.home() / ".local" / "share" / "gccslim" / "i18n",
            Path.home() / ".local" / "share" / "gccfork" / "i18n",
            Path.cwd() / "share" / "i18n",
        ]
    )
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in dirs:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _candidate_default_language_files() -> list[Path]:
    root = _script_root()
    return [
        Path.home() / ".local" / "share" / "gccslim" / "default-language",
        Path.home() / ".local" / "share" / "gccfork" / "default-language",
        root / "share" / "gccslim" / "default-language",
        Path.cwd() / "share" / "gccslim" / "default-language",
    ]


def normalize_language(value: Any) -> str:
    lang = str(value or "").strip().lower().replace("_", "-")
    if lang.startswith("ko"):
        return "ko"
    if lang.startswith("en"):
        return "en"
    return DEFAULT_LANGUAGE


def _env_language() -> str | None:
    for key in ("GCCSLIM_LANG", "GCCFORK_LANG", "LANGUAGE", "LC_ALL", "LANG"):
        value = os.environ.get(key)
        if value:
            normalized_raw = str(value).strip().lower()
            if normalized_raw in {"c", "c.utf-8", "posix"}:
                continue
            return normalize_language(value)
    return None


def current_language(default: str = DEFAULT_LANGUAGE) -> str:
    """Return the active UI language.

    Preference order:
    1. GCCSLIM_LANG / GCCFORK_LANG / locale environment
    2. GccSlim prefs key `ui_language`
    3. caller default / English

    The import is intentionally lazy to avoid circular imports during process
    start. If prefs are unavailable, English remains the safe public default.
    """
    env_lang = _env_language()
    if env_lang:
        return env_lang
    try:
        from gccfork_sessions import load_prefs

        prefs = load_prefs()
        if isinstance(prefs, dict) and prefs.get(LANGUAGE_PREF_KEY):
            return normalize_language(prefs.get(LANGUAGE_PREF_KEY))
    except Exception:
        pass
    for path in _candidate_default_language_files():
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return normalize_language(value)
    return normalize_language(default)


@lru_cache(maxsize=16)
def _load_language(lang: str) -> dict[str, str]:
    normalized = normalize_language(lang)
    for directory in _candidate_i18n_dirs():
        path = directory / f"{normalized}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            continue
    return {}


def tr(key: str, default: str | None = None, *, lang: str | None = None, **kwargs: Any) -> str:
    """Translate `key`, falling back to English and then `default`/key."""
    active = normalize_language(lang or current_language())
    value = _load_language(active).get(key)
    if value is None and active != DEFAULT_LANGUAGE:
        value = _load_language(DEFAULT_LANGUAGE).get(key)
    if value is None:
        value = default if default is not None else key
    if kwargs:
        try:
            return value.format(**kwargs)
        except Exception:
            return value
    return value


def set_language_pref(lang: str) -> str:
    """Persist the UI language preference and return the normalized value."""
    normalized = normalize_language(lang)
    try:
        from gccfork_sessions import pref_set

        pref_set(LANGUAGE_PREF_KEY, normalized)
    except Exception:
        pass
    _load_language.cache_clear()
    return normalized
