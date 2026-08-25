"""
Thresherr internationalization — *arr style.

The *arr family ships translations as JSON dictionaries and lets the user
pick a UI language in Settings -> General. Thresherr does the same:

- app/i18n/<lang>.json  -> key/value dictionaries per language
- English is the default (and the fallback for missing keys)
- Templates call {{ t('some.key') }} or {{ t('some.key', n=5) }}
- The choice is persisted in the settings table (key 'ui_language')
- Log messages stay in English, exactly like the *arr family

Usage (backend):
    import i18n
    t = i18n.translator("es")
    t("queue.enqueued", count=5)
"""

import json
import os
from functools import lru_cache

I18N_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")

# Available UI languages: code -> native name (shown in the selector)
LANGUAGES = {
    "en": "English",
    "es": "Español",
}

DEFAULT_LANGUAGE = "en"


@lru_cache(maxsize=None)
def _load(lang: str) -> dict:
    """Load a language dictionary (empty dict when missing/broken)."""
    path = os.path.join(I18N_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_text(lang: str, key: str, **kwargs) -> str:
    """
    Translate a key for a language.

    Resolution order: lang dictionary -> English dictionary -> the key
    itself. Supports str.format placeholders via kwargs.
    """
    table = _load(lang)
    text = table.get(key)
    if text is None and lang != DEFAULT_LANGUAGE:
        text = _load(DEFAULT_LANGUAGE).get(key)
    if text is None:
        text = key

    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def translator(lang: str):
    """
    Return a t(key, **kwargs) callable bound to a language.
    Injected into every template context as `t`.
    """
    def t(key: str, **kwargs) -> str:
        return get_text(lang, key, **kwargs)

    return t


def is_valid_language(lang: str | None) -> bool:
    return lang in LANGUAGES
