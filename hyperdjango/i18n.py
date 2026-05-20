"""
Internationalization and translation framework for HyperDjango.

Complete i18n system: translation catalogs, PO file parsing, plural rules,
lazy strings, per-thread language activation, locale middleware, template
integration, URL prefixing, and message extraction.

gettext / ngettext / pgettext / npgettext translation API with full
support for .po file format, BCP 47 language codes, and Accept-Language
header parsing.

Usage:
    from hyperdjango.i18n import gettext as _, ngettext, activate, override

    # Activate a language
    activate("fr")

    # Translate strings
    print(_("Hello"))  # "Bonjour"

    # Pluralization
    print(ngettext("%(count)d item", "%(count)d items", 3) % {"count": 3})

    # Temporary language switch
    with override("de"):
        print(_("Hello"))  # "Hallo"

    # Lazy strings for module-level definitions
    from hyperdjango.i18n import gettext_lazy
    label = gettext_lazy("Username")  # Translated when str() is called
"""

import contextvars
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from hyperdjango._hyperdjango_native import _template_set_i18n_callback
from hyperdjango.conf import get_setting

__all__ = [
    "TranslationCatalog",
    "TranslationEngine",
    "LazyString",
    "LanguageInfo",
    "POEntry",
    "LocaleMiddleware",
    "gettext",
    "ngettext",
    "pgettext",
    "npgettext",
    "gettext_lazy",
    "ngettext_lazy",
    "pgettext_lazy",
    "npgettext_lazy",
    "_",
    "activate",
    "deactivate",
    "get_language",
    "get_language_info",
    "override",
    "parse_po_file",
    "load_po_file",
    "get_plural_func",
    "discover_translations",
    "parse_accept_language",
    "setup_template_i18n",
    "i18n_url_patterns",
    "extract_messages",
    "create_po_file",
    "load_translations",
]


# ── Plural Rules ────────────────────────────────────────────────────────────

# Plural rule functions: language code -> (n -> plural form index)
# Covers the major plural rule families per CLDR.


def _plural_germanic(n: int) -> int:
    """Germanic: one/other (en, de, nl, sv, da, nb, nn, is, fo)."""
    return 0 if n == 1 else 1


def _plural_romance_fr(n: int) -> int:
    """French-style: 0 and 1 are singular (fr, pt-BR)."""
    return 0 if n in (0, 1) else 1


def _plural_romance_es(n: int) -> int:
    """Spanish/Italian/Portuguese: one/other (es, it, pt, ca, gl)."""
    return 0 if n == 1 else 1


def _plural_slavic_ru(n: int) -> int:
    """Russian/Ukrainian/Serbian/Croatian/Bosnian (ru, uk, sr, hr, bs)."""
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        return 0
    if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        return 1
    return 2


def _plural_slavic_pl(n: int) -> int:
    """Polish (pl)."""
    if n == 1:
        return 0
    mod10 = n % 10
    mod100 = n % 100
    if 2 <= mod10 <= 4 and not (12 <= mod100 <= 14):
        return 1
    return 2


def _plural_slavic_cs(n: int) -> int:
    """Czech/Slovak (cs, sk)."""
    if n == 1:
        return 0
    if 2 <= n <= 4:
        return 1
    return 2


def _plural_east_asian(n: int) -> int:
    """East Asian: no plurals (zh, ja, ko, vi, th, id, ms, tr)."""
    return 0


def _plural_arabic(n: int) -> int:
    """Arabic: 6 plural forms (ar)."""
    if n == 0:
        return 0
    if n == 1:
        return 1
    if n == 2:
        return 2
    mod100 = n % 100
    if 3 <= mod100 <= 10:
        return 3
    if 11 <= mod100 <= 99:
        return 4
    return 5


def _plural_romanian(n: int) -> int:
    """Romanian (ro)."""
    if n == 1:
        return 0
    mod100 = n % 100
    if n == 0 or (1 <= mod100 <= 19):
        return 1
    return 2


def _plural_lithuanian(n: int) -> int:
    """Lithuanian (lt)."""
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        return 0
    if 2 <= mod10 <= 9 and not (12 <= mod100 <= 19):
        return 1
    return 2


def _plural_latvian(n: int) -> int:
    """Latvian (lv)."""
    if n == 0:
        return 0
    if n % 10 == 1 and n % 100 != 11:
        return 1
    return 2


def _plural_irish(n: int) -> int:
    """Irish (ga)."""
    if n == 1:
        return 0
    if n == 2:
        return 1
    if 3 <= n <= 6:
        return 2
    if 7 <= n <= 10:
        return 3
    return 4


def _plural_welsh(n: int) -> int:
    """Welsh (cy)."""
    if n == 0:
        return 0
    if n == 1:
        return 1
    if n == 2:
        return 2
    if n == 3:
        return 3
    if n == 6:
        return 4
    return 5


_PLURAL_RULES: dict[str, Callable[[int], int]] = {
    # Germanic
    "en": _plural_germanic,
    "de": _plural_germanic,
    "nl": _plural_germanic,
    "sv": _plural_germanic,
    "da": _plural_germanic,
    "nb": _plural_germanic,
    "nn": _plural_germanic,
    "is": _plural_germanic,
    "fo": _plural_germanic,
    # Romance — French style
    "fr": _plural_romance_fr,
    "pt-BR": _plural_romance_fr,
    # Romance — Spanish/Italian style
    "es": _plural_romance_es,
    "it": _plural_romance_es,
    "pt": _plural_romance_es,
    "ca": _plural_romance_es,
    "gl": _plural_romance_es,
    # Slavic — Russian style
    "ru": _plural_slavic_ru,
    "uk": _plural_slavic_ru,
    "sr": _plural_slavic_ru,
    "hr": _plural_slavic_ru,
    "bs": _plural_slavic_ru,
    # Slavic — Polish
    "pl": _plural_slavic_pl,
    # Slavic — Czech/Slovak
    "cs": _plural_slavic_cs,
    "sk": _plural_slavic_cs,
    # East Asian / isolating
    "zh": _plural_east_asian,
    "ja": _plural_east_asian,
    "ko": _plural_east_asian,
    "vi": _plural_east_asian,
    "th": _plural_east_asian,
    "id": _plural_east_asian,
    "ms": _plural_east_asian,
    "tr": _plural_east_asian,
    # Arabic
    "ar": _plural_arabic,
    # Romanian
    "ro": _plural_romanian,
    # Lithuanian
    "lt": _plural_lithuanian,
    # Latvian
    "lv": _plural_latvian,
    # Irish
    "ga": _plural_irish,
    # Welsh
    "cy": _plural_welsh,
    # Hungarian — no plurals (when count is shown)
    "hu": _plural_east_asian,
    # Finnish
    "fi": _plural_germanic,
    # Estonian
    "et": _plural_germanic,
    # Greek
    "el": _plural_germanic,
    # Hebrew
    "he": _plural_germanic,
    # Hindi
    "hi": _plural_romance_fr,
    # Bengali
    "bn": _plural_romance_fr,
}


def get_plural_func(language: str) -> Callable[[int], int]:
    """Get the plural rule function for a language.

    Tries exact match first, then base language (e.g. "pt-BR" -> "pt"),
    then falls back to Germanic (one/other) rule.

    Args:
        language: BCP 47 language code.

    Returns:
        Function mapping count n to plural form index.
    """
    func = _PLURAL_RULES.get(language)
    if func is not None:
        return func
    # Try base language (e.g. "en-US" -> "en")
    if "-" in language:
        base = language.split("-", 1)[0]
        func = _PLURAL_RULES.get(base)
        if func is not None:
            return func
    # Default: Germanic one/other
    return _plural_germanic


# ── Language Info ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class LanguageInfo:
    """Metadata about a language."""

    code: str  # BCP 47 code: "fr"
    name: str  # English name: "French"
    name_local: str  # Native name: "Fran\u00e7ais"
    bidi: bool  # True for RTL languages (Arabic, Hebrew)


# ~50 common languages with their info.
_LANGUAGES: dict[str, LanguageInfo] = {
    "af": LanguageInfo("af", "Afrikaans", "Afrikaans", False),
    "ar": LanguageInfo(
        "ar", "Arabic", "\u0627\u0644\u0639\u0631\u0628\u064a\u0629", True
    ),
    "bg": LanguageInfo(
        "bg",
        "Bulgarian",
        "\u0431\u044a\u043b\u0433\u0430\u0440\u0441\u043a\u0438",
        False,
    ),
    "bn": LanguageInfo("bn", "Bengali", "\u09ac\u09be\u0982\u09b2\u09be", False),
    "bs": LanguageInfo("bs", "Bosnian", "bosanski", False),
    "ca": LanguageInfo("ca", "Catalan", "catal\u00e0", False),
    "cs": LanguageInfo("cs", "Czech", "\u010de\u0161tina", False),
    "cy": LanguageInfo("cy", "Welsh", "Cymraeg", False),
    "da": LanguageInfo("da", "Danish", "dansk", False),
    "de": LanguageInfo("de", "German", "Deutsch", False),
    "el": LanguageInfo(
        "el", "Greek", "\u0395\u03bb\u03bb\u03b7\u03bd\u03b9\u03ba\u03ac", False
    ),
    "en": LanguageInfo("en", "English", "English", False),
    "es": LanguageInfo("es", "Spanish", "espa\u00f1ol", False),
    "et": LanguageInfo("et", "Estonian", "eesti", False),
    "fa": LanguageInfo("fa", "Persian", "\u0641\u0627\u0631\u0633\u06cc", True),
    "fi": LanguageInfo("fi", "Finnish", "suomi", False),
    "fr": LanguageInfo("fr", "French", "fran\u00e7ais", False),
    "ga": LanguageInfo("ga", "Irish", "Gaeilge", False),
    "gl": LanguageInfo("gl", "Galician", "galego", False),
    "he": LanguageInfo("he", "Hebrew", "\u05e2\u05d1\u05e8\u05d9\u05ea", True),
    "hi": LanguageInfo("hi", "Hindi", "\u0939\u093f\u0928\u094d\u0926\u0940", False),
    "hr": LanguageInfo("hr", "Croatian", "hrvatski", False),
    "hu": LanguageInfo("hu", "Hungarian", "magyar", False),
    "id": LanguageInfo("id", "Indonesian", "Bahasa Indonesia", False),
    "is": LanguageInfo("is", "Icelandic", "\u00edslenska", False),
    "it": LanguageInfo("it", "Italian", "italiano", False),
    "ja": LanguageInfo("ja", "Japanese", "\u65e5\u672c\u8a9e", False),
    "ko": LanguageInfo("ko", "Korean", "\ud55c\uad6d\uc5b4", False),
    "lt": LanguageInfo("lt", "Lithuanian", "lietuvi\u0173", False),
    "lv": LanguageInfo("lv", "Latvian", "latvie\u0161u", False),
    "mk": LanguageInfo(
        "mk",
        "Macedonian",
        "\u043c\u0430\u043a\u0435\u0434\u043e\u043d\u0441\u043a\u0438",
        False,
    ),
    "ms": LanguageInfo("ms", "Malay", "Bahasa Melayu", False),
    "nb": LanguageInfo("nb", "Norwegian Bokm\u00e5l", "norsk bokm\u00e5l", False),
    "nl": LanguageInfo("nl", "Dutch", "Nederlands", False),
    "nn": LanguageInfo("nn", "Norwegian Nynorsk", "norsk nynorsk", False),
    "pl": LanguageInfo("pl", "Polish", "polski", False),
    "pt": LanguageInfo("pt", "Portuguese", "portugu\u00eas", False),
    "pt-BR": LanguageInfo(
        "pt-BR", "Brazilian Portuguese", "portugu\u00eas (Brasil)", False
    ),
    "ro": LanguageInfo("ro", "Romanian", "rom\u00e2n\u0103", False),
    "ru": LanguageInfo(
        "ru", "Russian", "\u0440\u0443\u0441\u0441\u043a\u0438\u0439", False
    ),
    "sk": LanguageInfo("sk", "Slovak", "sloven\u010dina", False),
    "sl": LanguageInfo("sl", "Slovenian", "sloven\u0161\u010dina", False),
    "sr": LanguageInfo("sr", "Serbian", "\u0441\u0440\u043f\u0441\u043a\u0438", False),
    "sv": LanguageInfo("sv", "Swedish", "svenska", False),
    "sw": LanguageInfo("sw", "Swahili", "Kiswahili", False),
    "th": LanguageInfo("th", "Thai", "\u0e44\u0e17\u0e22", False),
    "tr": LanguageInfo("tr", "Turkish", "T\u00fcrk\u00e7e", False),
    "uk": LanguageInfo(
        "uk",
        "Ukrainian",
        "\u0443\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430",
        False,
    ),
    "ur": LanguageInfo("ur", "Urdu", "\u0627\u0631\u062f\u0648", True),
    "vi": LanguageInfo("vi", "Vietnamese", "Ti\u1ebfng Vi\u1ec7t", False),
    "zh": LanguageInfo("zh", "Chinese", "\u4e2d\u6587", False),
    "zh-Hans": LanguageInfo(
        "zh-Hans", "Simplified Chinese", "\u7b80\u4f53\u4e2d\u6587", False
    ),
    "zh-Hant": LanguageInfo(
        "zh-Hant", "Traditional Chinese", "\u7e41\u9ad4\u4e2d\u6587", False
    ),
}

# Codes for RTL languages (fast membership check)
_BIDI_LANGUAGES: frozenset[str] = frozenset(
    info.code for info in _LANGUAGES.values() if info.bidi
)


def get_language_info(language: str) -> LanguageInfo:
    """Get metadata about a language.

    Args:
        language: BCP 47 language code.

    Returns:
        LanguageInfo for the language.

    Raises:
        KeyError: If the language code is not recognized.
    """
    info = _LANGUAGES.get(language)
    if info is not None:
        return info
    # Try base language
    if "-" in language:
        base = language.split("-", 1)[0]
        info = _LANGUAGES.get(base)
        if info is not None:
            return info
    raise KeyError(f"Unknown language code: {language!r}")


# ── Translation Catalog ─────────────────────────────────────────────────────


@dataclass(slots=True)
class TranslationCatalog:
    """Holds translations for a single language."""

    language: str  # BCP 47 code
    messages: dict[str, str]  # msgid -> msgstr
    plural_messages: dict[str, tuple[str, ...]]  # msgid -> (form0, form1, ...)
    context_messages: dict[tuple[str, str], str]  # (context, msgid) -> msgstr
    plural_func: Callable[[int], int]  # n -> plural form index


@dataclass(slots=True)
class TranslationEngine:
    """Thread-safe translation engine with catalog management.

    Manages loaded translation catalogs and provides the core
    translate/ntranslate/ptranslate operations used by the public API.

    Concurrency model (free-threaded 3.14t, no GIL): the catalog mapping
    (``_catalogs``) is treated as an immutable snapshot. Readers on the hot
    path (translate/ntranslate/ptranslate) read the current snapshot with
    NO lock — a single attribute load that always observes a fully-built,
    self-consistent dict. Writers (``load_catalog``) build a brand-new dict
    (and new merged catalog objects) under ``_lock`` and atomically swap the
    reference. Published catalogs are never mutated in place, so readers can
    never observe a half-updated catalog. This removes the per-``gettext()``
    lock that serialized every translation (measured ~14× slowdown at 8
    threads on 3.14t).
    """

    _catalogs: dict[str, TranslationCatalog] = field(default_factory=dict)
    _fallback_language: str = "en"
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def load_catalog(self, language: str, catalog: TranslationCatalog) -> None:
        """Register a translation catalog for a language.

        If a catalog already exists for the language, it is merged:
        new entries overwrite existing ones. The merge builds a fresh
        catalog and a fresh mapping, then atomically swaps ``_catalogs`` so
        lock-free readers only ever see complete snapshots.

        Args:
            language: BCP 47 language code.
            catalog: The TranslationCatalog to register.
        """
        with self._lock:
            snapshot = dict(self._catalogs)  # copy the mapping (immutable swap)
            existing = snapshot.get(language)
            if existing is not None:
                merged_messages = dict(existing.messages)
                merged_messages.update(catalog.messages)
                merged_plural = dict(existing.plural_messages)
                merged_plural.update(catalog.plural_messages)
                merged_context = dict(existing.context_messages)
                merged_context.update(catalog.context_messages)
                snapshot[language] = TranslationCatalog(
                    language=language,
                    messages=merged_messages,
                    plural_messages=merged_plural,
                    context_messages=merged_context,
                    plural_func=catalog.plural_func,
                )
            else:
                snapshot[language] = catalog
            # Atomic reference swap — readers see old-or-new, never partial.
            self._catalogs = snapshot

    def get_catalog(self, language: str) -> TranslationCatalog | None:
        """Get the catalog for a language, or None if not loaded.

        Lock-free: reads the current immutable ``_catalogs`` snapshot with a
        single attribute load. Safe under free-threading because writers only
        ever swap in a fully-built replacement dict (see class docstring).

        Args:
            language: BCP 47 language code.

        Returns:
            TranslationCatalog or None.
        """
        catalogs = self._catalogs
        catalog = catalogs.get(language)
        if catalog is not None:
            return catalog
        # Try base language
        if "-" in language:
            base = language.split("-", 1)[0]
            return catalogs.get(base)
        return None

    def get_loaded_languages(self) -> list[str]:
        """Return list of language codes with loaded catalogs.

        Lock-free accessor for catalog keys, used by i18n_url_patterns
        and _is_valid_language instead of accessing _catalogs directly.

        Returns:
            Sorted list of language codes.
        """
        return sorted(self._catalogs.keys())

    def has_language(self, code: str) -> bool:
        """Check if a catalog is loaded for the given language code.

        Lock-free check used by LocaleMiddleware._is_valid_language.

        Args:
            code: BCP 47 language code.

        Returns:
            True if a catalog exists for this code.
        """
        return code in self._catalogs

    def translate(self, msgid: str, language: str | None = None) -> str:
        """Translate a message string.

        Args:
            msgid: The message to translate.
            language: Target language. None uses the active language.

        Returns:
            Translated string, or the original msgid if no translation found.
        """
        lang = language if language is not None else get_language()
        catalog = self.get_catalog(lang)
        if catalog is not None:
            result = catalog.messages.get(msgid)
            if result is not None and result != "":
                return result
        # Fallback to default language
        if lang != self._fallback_language:
            catalog = self.get_catalog(self._fallback_language)
            if catalog is not None:
                result = catalog.messages.get(msgid)
                if result is not None and result != "":
                    return result
        return msgid

    def ntranslate(
        self, singular: str, plural: str, count: int, language: str | None = None
    ) -> str:
        """Translate with pluralization.

        Args:
            singular: Singular form message.
            plural: Plural form message.
            count: The count determining which plural form to use.
            language: Target language. None uses the active language.

        Returns:
            The appropriate plural form translation, or the English
            singular/plural if no translation found.
        """
        lang = language if language is not None else get_language()
        catalog = self.get_catalog(lang)
        if catalog is not None:
            forms = catalog.plural_messages.get(singular)
            if forms is not None:
                idx = catalog.plural_func(count)
                if 0 <= idx < len(forms) and forms[idx] != "":
                    return forms[idx]
        # Fallback to default language catalog
        if lang != self._fallback_language:
            catalog = self.get_catalog(self._fallback_language)
            if catalog is not None:
                forms = catalog.plural_messages.get(singular)
                if forms is not None:
                    idx = catalog.plural_func(count)
                    if 0 <= idx < len(forms) and forms[idx] != "":
                        return forms[idx]
        # Final fallback: use the source strings with English plural logic
        return singular if count == 1 else plural

    def ptranslate(self, context: str, msgid: str, language: str | None = None) -> str:
        """Translate with disambiguation context.

        Args:
            context: Context string for disambiguation.
            msgid: The message to translate.
            language: Target language. None uses the active language.

        Returns:
            Translated string, or the original msgid if no translation found.
        """
        lang = language if language is not None else get_language()
        key = (context, msgid)
        catalog = self.get_catalog(lang)
        if catalog is not None:
            result = catalog.context_messages.get(key)
            if result is not None and result != "":
                return result
        if lang != self._fallback_language:
            catalog = self.get_catalog(self._fallback_language)
            if catalog is not None:
                result = catalog.context_messages.get(key)
                if result is not None and result != "":
                    return result
        return msgid

    def nptranslate(
        self,
        context: str,
        singular: str,
        plural: str,
        count: int,
        language: str | None = None,
    ) -> str:
        """Translate with context and pluralization.

        Looks up the context+singular key in context_messages first,
        then falls back to plural_messages, then to source strings.

        Args:
            context: Context string for disambiguation.
            singular: Singular form message.
            plural: Plural form message.
            count: The count determining which plural form to use.
            language: Target language. None uses the active language.

        Returns:
            The appropriate translated plural form.
        """
        lang = language if language is not None else get_language()
        # Context+plural entries are stored under a context-prefixed key
        # (``{context}\x04{singular}``) in plural_messages by load_po_file.
        # Look that up directly — the msgstr of a context+plural PO entry is
        # empty, so keying off context_messages would always miss.
        ctx_plural_key = f"{context}\x04{singular}"
        catalog = self.get_catalog(lang)
        if catalog is not None:
            forms = catalog.plural_messages.get(ctx_plural_key)
            if forms is not None:
                idx = catalog.plural_func(count)
                if 0 <= idx < len(forms) and forms[idx] != "":
                    return forms[idx]
        # Fallback to default language catalog for the context+plural key
        if lang != self._fallback_language:
            fb_catalog = self.get_catalog(self._fallback_language)
            if fb_catalog is not None:
                forms = fb_catalog.plural_messages.get(ctx_plural_key)
                if forms is not None:
                    idx = fb_catalog.plural_func(count)
                    if 0 <= idx < len(forms) and forms[idx] != "":
                        return forms[idx]
        # Fall through to non-context plural
        return self.ntranslate(singular, plural, count, language)


# ── Global Engine ────────────────────────────────────────────────────────────

_engine: TranslationEngine = TranslationEngine()


def _get_engine() -> TranslationEngine:
    """Return the global translation engine."""
    return _engine


# ── Request-Scoped Language Activation ───────────────────────────────────────
#
# The active language is stored in a ContextVar, NOT threading.local. Under
# an async server a single OS thread runs the event loop and interleaves many
# concurrent requests; threading.local would make them all share one slot, so
# request A's activate()/deactivate() would clobber request B's language
# (cross-request language bleed). A ContextVar is copied per asyncio Task, so
# each request handler observes its own independent value — the same pattern
# tenancy.py uses for the current tenant.

_active_language: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hyper_active_language", default=None
)


def activate(language: str) -> None:
    """Set the active language for the current request context.

    Args:
        language: BCP 47 language code (e.g. "en", "fr", "de").
    """
    _active_language.set(language)


def deactivate() -> None:
    """Reset to the default language (from LANGUAGE_CODE setting)."""
    _active_language.set(None)


def get_language() -> str:
    """Get the active language code for the current request context.

    Returns:
        The active language, or LANGUAGE_CODE setting if none activated.
    """
    lang = _active_language.get()
    if lang is not None:
        return lang
    result: str = get_setting("LANGUAGE_CODE")  # type: ignore[assignment]
    return result


@contextmanager
def override(language: str | None):
    """Context manager to temporarily switch the active language.

    Args:
        language: Language to activate, or None to deactivate.

    Usage:
        with override("fr"):
            print(gettext("Hello"))  # "Bonjour"
    """
    # Token-based reset restores the exact prior value, so nested overrides
    # and per-task isolation both behave correctly.
    token = _active_language.set(language)
    try:
        yield
    finally:
        _active_language.reset(token)


# ── LazyString ───────────────────────────────────────────────────────────────


@dataclass(slots=True, init=False, eq=False, repr=False)
class LazyString:
    """String that delays translation until str() is called.

    Useful for module-level strings that need translation but the language
    is not known until request time. Behaves like a string in most contexts.
    """

    _func: Callable[..., str]
    _args: tuple[object, ...]

    def __init__(self, func: Callable[..., str], *args: object) -> None:
        self._func = func
        self._args = args

    def _resolve(self) -> str:
        return self._func(*self._args)

    def __str__(self) -> str:
        return self._resolve()

    def __repr__(self) -> str:
        return f"LazyString({self._resolve()!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, LazyString):
            return self._resolve() == other._resolve()
        if isinstance(other, str):
            return self._resolve() == other
        return NotImplemented

    def __hash__(self) -> int:
        # NOTE: hash is computed from the current translation. If the active
        # language changes between hash() and == checks, the hash may not
        # match. LazyStrings should not be used as dict keys across language
        # switches.
        return hash(self._resolve())

    def __format__(self, format_spec: str) -> str:
        return format(self._resolve(), format_spec)

    def __add__(self, other: object) -> str:
        if isinstance(other, (str, LazyString)):
            return self._resolve() + str(other)
        return NotImplemented

    def __radd__(self, other: object) -> str:
        if isinstance(other, (str, LazyString)):
            return str(other) + self._resolve()
        return NotImplemented

    def __mod__(self, other: object) -> str:
        return self._resolve() % other  # type: ignore[operator]

    def __bool__(self) -> bool:
        return bool(self._resolve())

    def __len__(self) -> int:
        return len(self._resolve())

    def __contains__(self, item: object) -> bool:
        return item in self._resolve()

    def __iter__(self) -> Iterator[str]:
        return iter(self._resolve())

    def __getitem__(self, key: int | slice) -> str:
        return self._resolve()[key]

    def __lt__(self, other: object) -> bool:
        if isinstance(other, (str, LazyString)):
            return self._resolve() < str(other)
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, (str, LazyString)):
            return self._resolve() <= str(other)
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, (str, LazyString)):
            return self._resolve() > str(other)
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        if isinstance(other, (str, LazyString)):
            return self._resolve() >= str(other)
        return NotImplemented


# ── Core Translation Functions ───────────────────────────────────────────────


def gettext(message: str) -> str:
    """Translate a message using the active language.

    Args:
        message: The string to translate.

    Returns:
        Translated string, or the original if no translation found.
    """
    return _engine.translate(message)


def ngettext(singular: str, plural: str, count: int) -> str:
    """Translate with pluralization.

    Args:
        singular: Singular form of the message.
        plural: Plural form of the message.
        count: Number used to determine which plural form to use.

    Returns:
        The appropriate plural form, translated.
    """
    return _engine.ntranslate(singular, plural, count)


def pgettext(context: str, message: str) -> str:
    """Translate with disambiguation context.

    Use when the same source string has different meanings in different
    contexts (e.g. "May" as a month vs. a verb).

    Args:
        context: Context string (e.g. "month name").
        message: The string to translate.

    Returns:
        Translated string, or the original if no translation found.
    """
    return _engine.ptranslate(context, message)


def npgettext(context: str, singular: str, plural: str, count: int) -> str:
    """Translate with context and pluralization.

    Args:
        context: Context string for disambiguation.
        singular: Singular form of the message.
        plural: Plural form of the message.
        count: Number used to determine which plural form to use.

    Returns:
        The appropriate plural form, translated.
    """
    return _engine.nptranslate(context, singular, plural, count)


# Short alias (Django convention)
_ = gettext


def gettext_lazy(message: str) -> LazyString:
    """Lazy translation -- evaluated when converted to str.

    Args:
        message: The string to translate.

    Returns:
        LazyString that translates on access.
    """
    return LazyString(gettext, message)


def ngettext_lazy(singular: str, plural: str, count: int) -> LazyString:
    """Lazy plural translation.

    Args:
        singular: Singular form.
        plural: Plural form.
        count: Number for plural selection.

    Returns:
        LazyString that translates on access.
    """
    return LazyString(ngettext, singular, plural, count)


def pgettext_lazy(context: str, message: str) -> LazyString:
    """Lazy context translation.

    Args:
        context: Context string.
        message: The string to translate.

    Returns:
        LazyString that translates on access.
    """
    return LazyString(pgettext, context, message)


def npgettext_lazy(context: str, singular: str, plural: str, count: int) -> LazyString:
    """Lazy context + plural translation.

    Args:
        context: Context string.
        singular: Singular form.
        plural: Plural form.
        count: Number for plural selection.

    Returns:
        LazyString that translates on access.
    """
    return LazyString(npgettext, context, singular, plural, count)


# ── PO File Parser ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class POEntry:
    """A single entry from a .po file."""

    msgid: str
    msgstr: str
    msgid_plural: str | None = None
    msgstr_plural: dict[int, str] | None = None
    msgctxt: str | None = None


def _unescape_po_string(s: str) -> str:
    """Unescape a PO file string (handle \\n, \\r, \\t, \\\\, \\")."""
    result: list[str] = []
    i = 0
    length = len(s)
    while i < length:
        ch = s[i]
        if ch == "\\" and i + 1 < length:
            nxt = s[i + 1]
            if nxt == "n":
                result.append("\n")
                i += 2
                continue
            if nxt == "r":
                result.append("\r")
                i += 2
                continue
            if nxt == "t":
                result.append("\t")
                i += 2
                continue
            if nxt == "\\":
                result.append("\\")
                i += 2
                continue
            if nxt == '"':
                result.append('"')
                i += 2
                continue
            result.append(ch)
            i += 1
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _extract_quoted(line: str) -> str:
    """Extract the content between the first and last double-quote on a line."""
    first = line.index('"')
    last = line.rindex('"')
    if first == last:
        return ""
    return _unescape_po_string(line[first + 1 : last])


# Regex for msgstr[N] lines
_MSGSTR_PLURAL_RE = re.compile(r'^msgstr\[(\d+)\]\s+"')


def parse_po_file(content: str) -> list[POEntry]:
    """Parse a .po file into POEntry objects.

    Handles:
    - Single-line and multi-line string concatenation
    - msgctxt (context)
    - msgid_plural and msgstr[N] (plural forms)
    - Comments (lines starting with #) are skipped

    Args:
        content: The full text content of a .po file.

    Returns:
        List of POEntry objects (metadata entry with empty msgid excluded).
    """
    entries: list[POEntry] = []
    lines = content.split("\n")
    idx = 0
    line_count = len(lines)

    while idx < line_count:
        line = lines[idx].strip()

        # Skip empty lines and comments
        if line == "" or line.startswith("#"):
            idx += 1
            continue

        current_msgctxt: str | None = None
        current_msgid: str = ""
        current_msgid_plural: str | None = None
        current_msgstr: str = ""
        current_msgstr_plural: dict[int, str] = {}
        section: str = ""  # Track which section we're reading continuations for
        plural_index: int = -1
        is_fuzzy: bool = False
        # True once this entry's msgid line has been consumed. A subsequent
        # msgid (or a msgctxt, which always precedes msgid) then signals the
        # start of the NEXT entry even with no blank-line separator between them
        # — gettext/msgfmt accept blank-line-less catalogs, so we must too.
        have_msgid: bool = False

        # Parse an entry: starts with msgctxt or msgid
        while idx < line_count:
            line = lines[idx].strip()

            # Empty line signals end of entry
            if line == "":
                idx += 1
                break

            # Track fuzzy flag; skip other comments within an entry
            if line.startswith("#"):
                if line.startswith("#,") and "fuzzy" in line:
                    is_fuzzy = True
                idx += 1
                continue

            if line.startswith("msgctxt "):
                # msgctxt always opens an entry (it precedes msgid). Seeing one
                # after this entry's msgid was already recorded means the prior
                # entry ended with no blank-line separator: break WITHOUT
                # advancing idx so the outer loop re-reads this line fresh.
                if have_msgid:
                    break
                section = "msgctxt"
                current_msgctxt = _extract_quoted(line)
                idx += 1
            elif line.startswith("msgid_plural "):
                section = "msgid_plural"
                current_msgid_plural = _extract_quoted(line)
                idx += 1
            elif line.startswith("msgid "):
                # A second msgid with no blank line in between starts the next
                # entry. Break WITHOUT advancing idx so the outer loop re-reads
                # this line as a fresh entry (otherwise the earlier entry, whose
                # msgstr is already recorded, is silently overwritten and lost).
                if have_msgid:
                    break
                section = "msgid"
                current_msgid = _extract_quoted(line)
                have_msgid = True
                idx += 1
            elif line.startswith("msgstr ") or line == 'msgstr ""':
                section = "msgstr"
                current_msgstr = _extract_quoted(line)
                idx += 1
            else:
                m = _MSGSTR_PLURAL_RE.match(line)
                if m is not None:
                    section = "msgstr_plural"
                    plural_index = int(m.group(1))
                    current_msgstr_plural[plural_index] = _extract_quoted(line)
                    idx += 1
                elif line.startswith('"') and line.endswith('"'):
                    # Continuation line
                    cont = _unescape_po_string(line[1:-1])
                    if section == "msgctxt":
                        current_msgctxt = (current_msgctxt or "") + cont
                    elif section == "msgid":
                        current_msgid += cont
                    elif section == "msgid_plural":
                        current_msgid_plural = (current_msgid_plural or "") + cont
                    elif section == "msgstr":
                        current_msgstr += cont
                    elif section == "msgstr_plural" and plural_index >= 0:
                        current_msgstr_plural[plural_index] = (
                            current_msgstr_plural.get(plural_index, "") + cont
                        )
                    idx += 1
                else:
                    # Unrecognized line — end of entry
                    idx += 1
                    break

        # Skip metadata entry (empty msgid) and fuzzy entries
        if current_msgid == "" or is_fuzzy:
            continue

        entry = POEntry(
            msgid=current_msgid,
            msgstr=current_msgstr,
            msgid_plural=current_msgid_plural,
            msgstr_plural=current_msgstr_plural or None,
            msgctxt=current_msgctxt,
        )
        entries.append(entry)

    return entries


def load_po_file(path: str | Path) -> TranslationCatalog:
    """Load a .po file and return a TranslationCatalog.

    The language code is inferred from the directory structure:
        locale/fr/LC_MESSAGES/django.po -> language = "fr"

    If the directory structure doesn't match, the filename stem is used.

    Args:
        path: Path to the .po file.

    Returns:
        A TranslationCatalog with all translations from the file.
    """
    p = Path(path)
    content = p.read_text(encoding="utf-8")
    entries = parse_po_file(content)

    # Infer language from path: .../locale/<lang>/LC_MESSAGES/<domain>.po
    parts = p.parts
    language = "en"  # fallback
    for i, part in enumerate(parts):
        if part == "LC_MESSAGES" and i > 0:
            language = parts[i - 1]
            break

    messages: dict[str, str] = {}
    plural_messages: dict[str, tuple[str, ...]] = {}
    context_messages: dict[tuple[str, str], str] = {}

    for entry in entries:
        if entry.msgctxt is not None:
            context_messages[(entry.msgctxt, entry.msgid)] = entry.msgstr
            # Store context+plural entries
            if entry.msgid_plural is not None and entry.msgstr_plural is not None:
                max_idx = (
                    max(entry.msgstr_plural.keys()) + 1 if entry.msgstr_plural else 0
                )
                forms = tuple(entry.msgstr_plural.get(i, "") for i in range(max_idx))
                ctx_key = f"{entry.msgctxt}\x04{entry.msgid}"
                plural_messages[ctx_key] = forms
        elif entry.msgid_plural is not None and entry.msgstr_plural is not None:
            max_idx = max(entry.msgstr_plural.keys()) + 1 if entry.msgstr_plural else 0
            forms = tuple(entry.msgstr_plural.get(i, "") for i in range(max_idx))
            plural_messages[entry.msgid] = forms
        else:
            messages[entry.msgid] = entry.msgstr

    return TranslationCatalog(
        language=language,
        messages=messages,
        plural_messages=plural_messages,
        context_messages=context_messages,
        plural_func=get_plural_func(language),
    )


# ── Locale Path Discovery ───────────────────────────────────────────────────


def discover_translations(
    locale_paths: list[str] | None = None,
) -> dict[str, TranslationCatalog]:
    """Discover and load all .po files from locale paths.

    Scans the standard directory structure:
        locale/<lang>/LC_MESSAGES/<domain>.po

    Args:
        locale_paths: List of directories to scan. If None, uses the
            LOCALE_PATHS setting, falling back to ["locale"].

    Returns:
        Dict mapping language code to merged TranslationCatalog.
    """
    if locale_paths is None:
        setting = get_setting("LOCALE_PATHS")
        locale_paths = list(setting) if isinstance(setting, (list, tuple)) else []
        if not locale_paths:
            locale_paths = ["locale"]

    catalogs: dict[str, TranslationCatalog] = {}

    for base_dir in locale_paths:
        base = Path(base_dir)
        if not base.is_dir():
            continue

        for lang_dir in sorted(base.iterdir()):
            if not lang_dir.is_dir():
                continue
            messages_dir = lang_dir / "LC_MESSAGES"
            if not messages_dir.is_dir():
                continue

            for po_file in sorted(messages_dir.glob("*.po")):
                catalog = load_po_file(po_file)
                language = lang_dir.name
                catalog = TranslationCatalog(
                    language=language,
                    messages=catalog.messages,
                    plural_messages=catalog.plural_messages,
                    context_messages=catalog.context_messages,
                    plural_func=get_plural_func(language),
                )
                existing = catalogs.get(language)
                if existing is not None:
                    existing.messages.update(catalog.messages)
                    existing.plural_messages.update(catalog.plural_messages)
                    existing.context_messages.update(catalog.context_messages)
                else:
                    catalogs[language] = catalog

    return catalogs


def load_translations(locale_paths: list[str] | None = None) -> None:
    """Discover and load all translations into the global engine.

    Convenience function that calls discover_translations() and loads
    all catalogs into the global TranslationEngine.

    Args:
        locale_paths: Optional list of locale directories. See discover_translations.
    """
    catalogs = discover_translations(locale_paths)
    for language, catalog in catalogs.items():
        _engine.load_catalog(language, catalog)


# ── Accept-Language Header Parser ────────────────────────────────────────────

_ACCEPT_LANG_RE = re.compile(
    r"([a-zA-Z]{1,8}(?:-[a-zA-Z0-9]{1,8})*)"
    r"(?:\s*;\s*q\s*=\s*((?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)))?"
)


_ACCEPT_LANGUAGE_MAX_LENGTH: int = 4096


def parse_accept_language(header: str) -> list[tuple[str, float]]:
    """Parse an Accept-Language header into (language, quality) pairs.

    Returns pairs sorted by quality (highest first). Languages with
    equal quality preserve their original order.

    Truncates headers longer than 4096 bytes to prevent DoS via
    extremely long Accept-Language values.

    Args:
        header: The Accept-Language header value
            (e.g. "en-US,en;q=0.9,fr;q=0.8").

    Returns:
        List of (language_code, quality) tuples sorted by quality desc.
    """
    # Truncate excessively long headers to prevent regex DoS
    if len(header) > _ACCEPT_LANGUAGE_MAX_LENGTH:
        header = header[:_ACCEPT_LANGUAGE_MAX_LENGTH]
    results: list[tuple[str, float]] = []
    for match in _ACCEPT_LANG_RE.finditer(header):
        lang = match.group(1)
        q_str = match.group(2)
        quality = float(q_str) if q_str is not None else 1.0
        results.append((lang, quality))
    # Stable sort by quality descending
    results.sort(key=lambda pair: pair[1], reverse=True)
    return results


# ── Locale Middleware ────────────────────────────────────────────────────────


@dataclass(slots=True)
class LocaleMiddleware:
    """Detect and activate language per-request.

    Detection order:
    1. URL prefix (/en/about/, /fr/about/)
    2. Cookie (configurable name, default "hyper_language")
    3. Accept-Language header
    4. LANGUAGE_CODE setting (fallback)

    After determining the language, it is activated for the thread
    handling the request.
    """

    cookie_name: str = "hyper_language"

    async def __call__(
        self, request: object, call_next: Callable[..., object]
    ) -> object:
        """Process a request, detecting and activating the appropriate language.

        Args:
            request: The incoming request object. Expected to have .path,
                .cookies (dict), and .headers (dict-like) attributes.
            call_next: Async callable to invoke the next middleware/handler.

        Returns:
            Response from call_next.
        """
        language = self._detect_language(request)
        activate(language)
        try:
            response = await call_next(request)
        finally:
            deactivate()
        return response

    def _detect_language(self, request: object) -> str:
        """Determine the best language for the request.

        Defensively accesses request attributes (.path, .cookies, .headers)
        since this is public middleware that may receive unexpected objects.

        Args:
            request: Request object with .path, .cookies, .headers.

        Returns:
            BCP 47 language code.
        """
        # 1. URL prefix: /en/..., /fr/...
        try:
            path = request.path  # type: ignore[union-attr]
        except AttributeError:
            path = None
        if isinstance(path, str) and len(path) > 1:
            # Match /xx/ or /xx-YY/ at start of path
            segments = path.split("/")
            if len(segments) >= 2 and segments[0] == "":
                candidate = segments[1]
                if self._is_valid_language(candidate):
                    return candidate

        # 2. Cookie
        try:
            cookies = request.cookies  # type: ignore[union-attr]
        except AttributeError:
            cookies = None
        if isinstance(cookies, dict):
            cookie_lang = cookies.get(self.cookie_name)
            if (
                cookie_lang is not None
                and isinstance(cookie_lang, str)
                and self._is_valid_language(cookie_lang)
            ):
                return cookie_lang

        # 3. Accept-Language header
        try:
            headers = request.headers  # type: ignore[union-attr]
        except AttributeError:
            headers = None
        accept_header = None
        if isinstance(headers, dict):
            accept_header = headers.get("accept-language") or headers.get(
                "Accept-Language"
            )
        if accept_header is not None and isinstance(accept_header, str):
            parsed = parse_accept_language(accept_header)
            for lang, _quality in parsed:
                if self._is_valid_language(lang):
                    return lang
                # Try base language
                if "-" in lang:
                    base = lang.split("-", 1)[0]
                    if self._is_valid_language(base):
                        return base

        # 4. Setting fallback
        result: str = get_setting("LANGUAGE_CODE")  # type: ignore[assignment]
        return result

    def _is_valid_language(self, code: str) -> bool:
        """Check if a language code is known.

        Args:
            code: Language code to check.

        Returns:
            True if the code matches a known language or loaded catalog.
        """
        if code in _LANGUAGES:
            return True
        if _engine.has_language(code):
            return True
        if "-" in code:
            base = code.split("-", 1)[0]
            return base in _LANGUAGES or _engine.has_language(base)
        return False


# ── Template Integration ─────────────────────────────────────────────────────


def setup_template_i18n(engine: object | None = None) -> None:
    """Register the i18n callback with the Zig template engine.

    This enables {% trans "Hello" %} in templates to call gettext().
    The Zig engine calls the callback with a string and gets the
    translation back.

    Args:
        engine: Unused; the callback is registered globally via the
            native _template_set_i18n_callback function.
    """
    _template_set_i18n_callback(gettext)


# ── URL i18n Helper ──────────────────────────────────────────────────────────


def i18n_url_patterns(
    *patterns: tuple[str, object],
    prefix_default_language: bool = True,
    languages: list[str] | None = None,
) -> list[tuple[str, object]]:
    """Wrap URL patterns with language prefix.

    Takes a set of (path, view) tuples and returns them prefixed with
    each supported language code.

    Args:
        *patterns: Tuples of (url_path, view_callable).
        prefix_default_language: If True, include prefix for the default
            language too. If False, the default language uses unprefixed URLs.
        languages: List of language codes. If None, uses all languages
            that have loaded catalogs, plus the default language.

    Returns:
        List of (prefixed_path, view) tuples.

    Usage:
        routes = i18n_url_patterns(
            ("/about/", about_view),
            ("/contact/", contact_view),
        )
        # Generates: /en/about/, /fr/about/, /en/contact/, /fr/contact/
    """
    if languages is None:
        lang_set: set[str] = set(_engine.get_loaded_languages())
        default_lang: str = get_setting("LANGUAGE_CODE")  # type: ignore[assignment]
        lang_set.add(default_lang)
        languages = sorted(lang_set)

    default_lang_code: str = get_setting("LANGUAGE_CODE")  # type: ignore[assignment]
    result: list[tuple[str, object]] = []

    for lang in languages:
        for path, view in patterns:
            if lang == default_lang_code and not prefix_default_language:
                result.append((path, view))
            else:
                # Ensure path starts with /
                if path.startswith("/"):
                    prefixed = f"/{lang}{path}"
                else:
                    prefixed = f"/{lang}/{path}"
                result.append((prefixed, view))

    return result


# ── Message Extraction ───────────────────────────────────────────────────────

# Patterns to match in Python source files
_PYTHON_GETTEXT_RE = re.compile(
    r"""(?:gettext|gettext_lazy|pgettext|pgettext_lazy|_)\s*\(\s*"""
    r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
    r"""\s*\)""",
)
_PYTHON_NGETTEXT_RE = re.compile(
    r"""(?:ngettext|ngettext_lazy)\s*\(\s*"""
    r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*,\s*"""
    r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""",
)

# Pattern to match {% trans "..." %} in templates
_TEMPLATE_TRANS_RE = re.compile(
    r"""\{%[-\s]*trans\s+["']((?:[^"'\\]|\\.)*)["']""",
)


def extract_messages(source_dirs: list[str]) -> list[str]:
    """Extract translatable strings from Python files and templates.

    Scans for:
    - gettext("..."), _("...")
    - ngettext("...", "...", n) (extracts both singular and plural)
    - pgettext("context", "...") (extracts the message, not context)
    - {% trans "..." %} in template files

    Args:
        source_dirs: List of directories to scan recursively.

    Returns:
        Deduplicated list of translatable message strings, sorted.
    """
    messages: set[str] = set()
    py_extensions: frozenset[str] = frozenset({".py"})
    template_extensions: frozenset[str] = frozenset(
        {".html", ".txt", ".xml", ".jinja", ".jinja2"}
    )

    for source_dir in source_dirs:
        base = Path(source_dir)
        if not base.is_dir():
            continue

        for file_path in base.rglob("*"):
            if not file_path.is_file():
                continue
            suffix = file_path.suffix

            if suffix in py_extensions:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                for match in _PYTHON_GETTEXT_RE.finditer(content):
                    # group(1) = double-quoted, group(2) = single-quoted
                    msg = (
                        match.group(1) if match.group(1) is not None else match.group(2)
                    )
                    messages.add(msg)
                for match in _PYTHON_NGETTEXT_RE.finditer(content):
                    # groups 1,2 = first string; groups 3,4 = second string
                    singular = (
                        match.group(1) if match.group(1) is not None else match.group(2)
                    )
                    plural = (
                        match.group(3) if match.group(3) is not None else match.group(4)
                    )
                    messages.add(singular)
                    messages.add(plural)

            elif suffix in template_extensions:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                for match in _TEMPLATE_TRANS_RE.finditer(content):
                    messages.add(match.group(1))

    return sorted(messages)


def _escape_po_string(s: str) -> str:
    """Escape a string for PO file output."""
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\n", "\\n")
    s = s.replace("\r", "\\r")
    s = s.replace("\t", "\\t")
    return s


def create_po_file(
    messages: list[str], language: str, existing: str | None = None
) -> str:
    """Generate a .po file content from extracted messages.

    If existing content is provided, merges: preserving existing
    translations and adding new messages with empty msgstr.

    Args:
        messages: List of translatable message strings.
        language: Target language code (for the PO header).
        existing: Optional existing .po file content to merge with.

    Returns:
        Complete .po file content as a string.
    """
    # Parse existing translations if provided
    existing_translations: dict[str, str] = {}
    if existing is not None:
        entries = parse_po_file(existing)
        for entry in entries:
            existing_translations[entry.msgid] = entry.msgstr

    # Build PO content
    parts: list[str] = []

    # PO header
    parts.append(f"# Translation file for language: {language}")
    parts.append("#")
    parts.append('msgid ""')
    parts.append('msgstr ""')
    parts.append('"Content-Type: text/plain; charset=UTF-8\\n"')
    parts.append('"Content-Transfer-Encoding: 8bit\\n"')
    parts.append(f'"Language: {language}\\n"')
    # Determine nplurals from the plural function
    nplurals = _detect_nplurals(language)
    parts.append(f'"Plural-Forms: nplurals={nplurals};\\n"')
    parts.append("")

    # Message entries
    for msg in sorted(messages):
        escaped = _escape_po_string(msg)
        translation = existing_translations.get(msg, "")
        escaped_translation = _escape_po_string(translation)
        parts.append(f'msgid "{escaped}"')
        parts.append(f'msgstr "{escaped_translation}"')
        parts.append("")

    return "\n".join(parts)


def _detect_nplurals(language: str) -> int:
    """Detect the number of plural forms for a language.

    Args:
        language: BCP 47 language code.

    Returns:
        Number of plural forms (1-6).
    """
    func = get_plural_func(language)
    # Test a range of numbers to find the maximum plural index
    max_form = 0
    for n in range(200):
        form = func(n)
        if form > max_form:
            max_form = form
    return max_form + 1
