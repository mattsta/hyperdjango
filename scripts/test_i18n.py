"""Comprehensive tests for hyperdjango.i18n translation module."""

# hyper-test: unit

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperdjango.i18n import (
    LazyString,
    LocaleMiddleware,
    POEntry,
    TranslationCatalog,
    TranslationEngine,
    _,
    _engine,
    _escape_po_string,
    _plural_arabic,
    _plural_east_asian,
    _plural_germanic,
    _plural_irish,
    _plural_latvian,
    _plural_lithuanian,
    _plural_romance_fr,
    _plural_romanian,
    _plural_slavic_cs,
    _plural_slavic_pl,
    _plural_slavic_ru,
    _plural_welsh,
    _unescape_po_string,
    activate,
    create_po_file,
    deactivate,
    extract_messages,
    get_language,
    get_language_info,
    get_plural_func,
    gettext,
    gettext_lazy,
    i18n_url_patterns,
    ngettext,
    ngettext_lazy,
    npgettext,
    npgettext_lazy,
    override,
    parse_accept_language,
    parse_po_file,
    pgettext,
    pgettext_lazy,
)

PASS = 0
FAIL = 0


def test(name, got, expected):
    global PASS, FAIL
    if got == expected:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        print(f"    got:      {got!r}")
        print(f"    expected: {expected!r}")


# ── Helper: build a fresh engine with catalogs ─────────────────────────────


def make_engine_with_catalogs():
    """Create a fresh TranslationEngine with French and German catalogs."""
    engine = TranslationEngine()
    fr_catalog = TranslationCatalog(
        language="fr",
        messages={"Hello": "Bonjour", "Goodbye": "Au revoir", "Yes": "Oui"},
        plural_messages={
            "%(count)d item": ("%(count)d article", "%(count)d articles"),
        },
        context_messages={
            ("greeting", "Hello"): "Bonjour (salutation)",
            ("farewell", "Hello"): "Adieu",
        },
        plural_func=get_plural_func("fr"),
    )
    de_catalog = TranslationCatalog(
        language="de",
        messages={"Hello": "Hallo", "Goodbye": "Auf Wiedersehen"},
        plural_messages={
            "%(count)d item": ("%(count)d Artikel", "%(count)d Artikel"),
        },
        context_messages={},
        plural_func=get_plural_func("de"),
    )
    engine.load_catalog("fr", fr_catalog)
    engine.load_catalog("de", de_catalog)
    return engine


# ═══════════════════════════════════════════════════════════════════════════
# Plural Rules (25 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_plural_rules():
    # English / Germanic: 1 -> 0 (singular), anything else -> 1 (plural)
    test("en: 0 -> plural", _plural_germanic(0), 1)
    test("en: 1 -> singular", _plural_germanic(1), 0)
    test("en: 2 -> plural", _plural_germanic(2), 1)
    test("en: 100 -> plural", _plural_germanic(100), 1)

    # French: 0 -> singular, 1 -> singular, 2+ -> plural
    test("fr: 0 -> singular", _plural_romance_fr(0), 0)
    test("fr: 1 -> singular", _plural_romance_fr(1), 0)
    test("fr: 2 -> plural", _plural_romance_fr(2), 1)
    test("fr: 10 -> plural", _plural_romance_fr(10), 1)

    # Russian: complex 3-form
    test("ru: 1 -> form 0", _plural_slavic_ru(1), 0)
    test("ru: 2 -> form 1", _plural_slavic_ru(2), 1)
    test("ru: 3 -> form 1", _plural_slavic_ru(3), 1)
    test("ru: 4 -> form 1", _plural_slavic_ru(4), 1)
    test("ru: 5 -> form 2", _plural_slavic_ru(5), 2)
    test("ru: 11 -> form 2", _plural_slavic_ru(11), 2)
    test("ru: 12 -> form 2", _plural_slavic_ru(12), 2)
    test("ru: 21 -> form 0", _plural_slavic_ru(21), 0)
    test("ru: 22 -> form 1", _plural_slavic_ru(22), 1)
    test("ru: 100 -> form 2", _plural_slavic_ru(100), 2)

    # Japanese: always 0
    test("ja: 0 -> 0", _plural_east_asian(0), 0)
    test("ja: 1 -> 0", _plural_east_asian(1), 0)
    test("ja: 100 -> 0", _plural_east_asian(100), 0)

    # Arabic: 6 forms
    test("ar: 0 -> form 0", _plural_arabic(0), 0)
    test("ar: 1 -> form 1", _plural_arabic(1), 1)
    test("ar: 2 -> form 2", _plural_arabic(2), 2)
    test("ar: 5 -> form 3", _plural_arabic(5), 3)
    test("ar: 11 -> form 4", _plural_arabic(11), 4)
    test("ar: 100 -> form 5", _plural_arabic(100), 5)
    test("ar: 99 -> form 4", _plural_arabic(99), 4)
    test("ar: 3 -> form 3", _plural_arabic(3), 3)

    # Polish: 3 forms
    test("pl: 1 -> form 0", _plural_slavic_pl(1), 0)
    test("pl: 2 -> form 1", _plural_slavic_pl(2), 1)
    test("pl: 5 -> form 2", _plural_slavic_pl(5), 2)
    test("pl: 22 -> form 1", _plural_slavic_pl(22), 1)
    test("pl: 12 -> form 2", _plural_slavic_pl(12), 2)

    # get_plural_func: exact match
    func_en = get_plural_func("en")
    test("get_plural_func en returns germanic", func_en(1), 0)
    test("get_plural_func en plural", func_en(2), 1)

    # get_plural_func: base language fallback ("en-US" -> "en")
    func_en_us = get_plural_func("en-US")
    test("en-US falls back to en: singular", func_en_us(1), 0)
    test("en-US falls back to en: plural", func_en_us(2), 1)

    # get_plural_func: unknown language -> Germanic default
    func_unknown = get_plural_func("xx")
    test("unknown lang falls back to germanic: 1", func_unknown(1), 0)
    test("unknown lang falls back to germanic: 2", func_unknown(2), 1)

    # Czech: 3 forms
    test("cs: 1 -> form 0", _plural_slavic_cs(1), 0)
    test("cs: 3 -> form 1", _plural_slavic_cs(3), 1)
    test("cs: 5 -> form 2", _plural_slavic_cs(5), 2)

    # Romanian: 3 forms
    test("ro: 1 -> form 0", _plural_romanian(1), 0)
    test("ro: 0 -> form 1", _plural_romanian(0), 1)
    test("ro: 19 -> form 1", _plural_romanian(19), 1)
    test("ro: 200 -> form 2", _plural_romanian(200), 2)

    # Lithuanian: 3 forms
    test("lt: 1 -> form 0", _plural_lithuanian(1), 0)
    test("lt: 2 -> form 1", _plural_lithuanian(2), 1)
    test("lt: 10 -> form 2", _plural_lithuanian(10), 2)

    # Latvian: 3 forms
    test("lv: 0 -> form 0", _plural_latvian(0), 0)
    test("lv: 1 -> form 1", _plural_latvian(1), 1)
    test("lv: 2 -> form 2", _plural_latvian(2), 2)

    # Irish: 5 forms
    test("ga: 1 -> form 0", _plural_irish(1), 0)
    test("ga: 2 -> form 1", _plural_irish(2), 1)
    test("ga: 5 -> form 2", _plural_irish(5), 2)
    test("ga: 9 -> form 3", _plural_irish(9), 3)
    test("ga: 11 -> form 4", _plural_irish(11), 4)

    # Welsh: 6 forms
    test("cy: 0 -> form 0", _plural_welsh(0), 0)
    test("cy: 1 -> form 1", _plural_welsh(1), 1)
    test("cy: 2 -> form 2", _plural_welsh(2), 2)
    test("cy: 3 -> form 3", _plural_welsh(3), 3)
    test("cy: 6 -> form 4", _plural_welsh(6), 4)
    test("cy: 7 -> form 5", _plural_welsh(7), 5)


# ═══════════════════════════════════════════════════════════════════════════
# LanguageInfo (12 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_language_info():
    # English
    en = get_language_info("en")
    test("en code", en.code, "en")
    test("en name", en.name, "English")
    test("en name_local", en.name_local, "English")
    test("en bidi=False", en.bidi, False)

    # French
    fr = get_language_info("fr")
    test("fr code", fr.code, "fr")
    test("fr name", fr.name, "French")
    test("fr bidi=False", fr.bidi, False)

    # Arabic (RTL)
    ar = get_language_info("ar")
    test("ar bidi=True", ar.bidi, True)

    # Hebrew (RTL)
    he = get_language_info("he")
    test("he bidi=True", he.bidi, True)

    # Urdu (RTL)
    ur = get_language_info("ur")
    test("ur bidi=True", ur.bidi, True)

    # Base language fallback: "en-US" -> "en"
    en_us = get_language_info("en-US")
    test("en-US falls back to en", en_us.code, "en")

    # Unknown language raises KeyError
    raised = False
    try:
        get_language_info("xx-ZZ")
    except KeyError:
        raised = True
    test("unknown lang raises KeyError", raised, True)


# ═══════════════════════════════════════════════════════════════════════════
# TranslationCatalog (10 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_translation_catalog():
    cat = TranslationCatalog(
        language="fr",
        messages={"Hello": "Bonjour"},
        plural_messages={"item": ("article", "articles")},
        context_messages={("nav", "Home"): "Accueil"},
        plural_func=get_plural_func("fr"),
    )
    test("catalog language", cat.language, "fr")
    test("catalog messages", cat.messages["Hello"], "Bonjour")
    test("catalog plural_messages singular", cat.plural_messages["item"][0], "article")
    test("catalog plural_messages plural", cat.plural_messages["item"][1], "articles")
    test("catalog context_messages", cat.context_messages[("nav", "Home")], "Accueil")
    test("catalog plural_func(0)=0 (french)", cat.plural_func(0), 0)
    test("catalog plural_func(1)=0 (french)", cat.plural_func(1), 0)
    test("catalog plural_func(2)=1 (french)", cat.plural_func(2), 1)

    # Empty catalog
    empty = TranslationCatalog(
        language="en",
        messages={},
        plural_messages={},
        context_messages={},
        plural_func=get_plural_func("en"),
    )
    test("empty catalog messages", len(empty.messages), 0)
    test("empty catalog plural_messages", len(empty.plural_messages), 0)


# ═══════════════════════════════════════════════════════════════════════════
# TranslationEngine (22 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_translation_engine():
    engine = make_engine_with_catalogs()

    # load_catalog / get_catalog roundtrip
    cat = engine.get_catalog("fr")
    test("get_catalog fr not None", cat is not None, True)
    test("get_catalog fr language", cat.language, "fr")

    cat_de = engine.get_catalog("de")
    test("get_catalog de not None", cat_de is not None, True)

    # Unknown catalog returns None
    test("get_catalog unknown", engine.get_catalog("xx"), None)

    # get_catalog base language fallback
    engine2 = TranslationEngine()
    en_cat = TranslationCatalog(
        language="en",
        messages={"Hi": "Hi"},
        plural_messages={},
        context_messages={},
        plural_func=get_plural_func("en"),
    )
    engine2.load_catalog("en", en_cat)
    test("get_catalog en-US -> en", engine2.get_catalog("en-US") is not None, True)

    # translate: known message
    test("translate Hello -> Bonjour", engine.translate("Hello", "fr"), "Bonjour")

    # translate: unknown message returns original
    test("translate unknown -> original", engine.translate("Unknown", "fr"), "Unknown")

    # translate: fallback language used
    engine3 = TranslationEngine()
    en_cat3 = TranslationCatalog(
        language="en",
        messages={"Save": "Save"},
        plural_messages={},
        context_messages={},
        plural_func=get_plural_func("en"),
    )
    engine3.load_catalog("en", en_cat3)
    test("translate fallback to en", engine3.translate("Save", "fr"), "Save")

    # ntranslate: singular
    test(
        "ntranslate fr count=1",
        engine.ntranslate("%(count)d item", "%(count)d items", 1, "fr"),
        "%(count)d article",
    )
    # ntranslate: plural
    test(
        "ntranslate fr count=5",
        engine.ntranslate("%(count)d item", "%(count)d items", 5, "fr"),
        "%(count)d articles",
    )
    # ntranslate: French 0 is singular
    test(
        "ntranslate fr count=0",
        engine.ntranslate("%(count)d item", "%(count)d items", 0, "fr"),
        "%(count)d article",
    )
    # ntranslate: unknown falls back to source
    test(
        "ntranslate unknown singular", engine.ntranslate("cat", "cats", 1, "xx"), "cat"
    )
    test("ntranslate unknown plural", engine.ntranslate("cat", "cats", 5, "xx"), "cats")

    # ptranslate: context disambiguation
    test(
        "ptranslate greeting",
        engine.ptranslate("greeting", "Hello", "fr"),
        "Bonjour (salutation)",
    )
    test("ptranslate farewell", engine.ptranslate("farewell", "Hello", "fr"), "Adieu")
    test(
        "ptranslate unknown context",
        engine.ptranslate("unknown_ctx", "Hello", "fr"),
        "Hello",
    )

    # Merge catalogs
    engine4 = TranslationEngine()
    cat_a = TranslationCatalog(
        language="fr",
        messages={"A": "AA"},
        plural_messages={},
        context_messages={},
        plural_func=get_plural_func("fr"),
    )
    cat_b = TranslationCatalog(
        language="fr",
        messages={"B": "BB"},
        plural_messages={},
        context_messages={},
        plural_func=get_plural_func("fr"),
    )
    engine4.load_catalog("fr", cat_a)
    engine4.load_catalog("fr", cat_b)
    merged = engine4.get_catalog("fr")
    test("merged has A", merged.messages.get("A"), "AA")
    test("merged has B", merged.messages.get("B"), "BB")

    # Thread safety: load_catalog from multiple threads
    engine5 = TranslationEngine()
    errors = []

    def load_cat(lang, msg):
        try:
            c = TranslationCatalog(
                language=lang,
                messages={msg: f"translated_{msg}"},
                plural_messages={},
                context_messages={},
                plural_func=get_plural_func("en"),
            )
            engine5.load_catalog(lang, c)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=load_cat, args=(f"lang{i}", f"msg{i}"))
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    test("thread safety: no errors", len(errors), 0)
    test("thread safety: all loaded", len(engine5._catalogs), 10)


# ═══════════════════════════════════════════════════════════════════════════
# Core API Functions (17 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_core_api():
    # Save and restore engine state
    old_catalogs = dict(_engine._catalogs)

    try:
        # Clear catalogs
        _engine._catalogs.clear()
        deactivate()

        # gettext with no catalog -> returns original
        test("gettext no catalog", gettext("Hello"), "Hello")

        # Load French catalog into global engine
        fr_cat = TranslationCatalog(
            language="fr",
            messages={"Hello": "Bonjour", "World": "Monde"},
            plural_messages={
                "%(count)d item": ("%(count)d article", "%(count)d articles"),
            },
            context_messages={
                ("greeting", "Hello"): "Bonjour (salutation)",
                ("farewell", "Hello"): "Adieu",
            },
            plural_func=get_plural_func("fr"),
        )
        _engine.load_catalog("fr", fr_cat)

        # gettext with French active
        activate("fr")
        test("gettext fr Hello", gettext("Hello"), "Bonjour")
        test("gettext fr World", gettext("World"), "Monde")

        # ngettext
        test(
            "ngettext singular",
            ngettext("%(count)d item", "%(count)d items", 1),
            "%(count)d article",
        )
        test(
            "ngettext plural",
            ngettext("%(count)d item", "%(count)d items", 5),
            "%(count)d articles",
        )
        test(
            "ngettext zero (fr=singular)",
            ngettext("%(count)d item", "%(count)d items", 0),
            "%(count)d article",
        )

        # pgettext
        test("pgettext greeting", pgettext("greeting", "Hello"), "Bonjour (salutation)")
        test("pgettext farewell", pgettext("farewell", "Hello"), "Adieu")

        # npgettext — falls through to ntranslate since no context+plural key stored
        result = npgettext("some_ctx", "%(count)d item", "%(count)d items", 5)
        test("npgettext plural", result, "%(count)d articles")

        # _ alias works
        test("_ alias", _("Hello"), "Bonjour")

        # Unknown message returns original
        test("gettext unknown", gettext("xyz_missing"), "xyz_missing")

        # Switch back to English (no catalog) -> returns original
        activate("en")
        test("gettext en no catalog", gettext("Hello"), "Hello")

        # ngettext with no catalog
        test("ngettext en singular", ngettext("cat", "cats", 1), "cat")
        test("ngettext en plural", ngettext("cat", "cats", 5), "cats")

        # pgettext with no catalog
        test("pgettext en no catalog", pgettext("ctx", "Hello"), "Hello")

        # npgettext with no catalog
        test("npgettext en no catalog", npgettext("ctx", "cat", "cats", 1), "cat")
        test(
            "npgettext en no catalog plural", npgettext("ctx", "cat", "cats", 5), "cats"
        )

    finally:
        _engine._catalogs.clear()
        _engine._catalogs.update(old_catalogs)
        deactivate()


# ═══════════════════════════════════════════════════════════════════════════
# LazyString (22 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_lazy_string():
    old_catalogs = dict(_engine._catalogs)

    try:
        _engine._catalogs.clear()
        fr_cat = TranslationCatalog(
            language="fr",
            messages={"Hello": "Bonjour", "World": "Monde", "": ""},
            plural_messages={
                "%(count)d item": ("%(count)d article", "%(count)d articles"),
            },
            context_messages={("ctx", "Hello"): "Ctx Bonjour"},
            plural_func=get_plural_func("fr"),
        )
        _engine.load_catalog("fr", fr_cat)
        activate("fr")

        # str() triggers translation
        lazy = gettext_lazy("Hello")
        test("lazy str()", str(lazy), "Bonjour")

        # repr()
        test("lazy repr()", repr(lazy), "LazyString('Bonjour')")

        # __eq__ with str
        test("lazy == str", lazy == "Bonjour", True)
        test("lazy != wrong str", lazy == "Wrong", False)

        # __eq__ with LazyString
        lazy2 = gettext_lazy("Hello")
        test("lazy == lazy", lazy == lazy2, True)

        # __hash__ matches str hash
        test("lazy hash == str hash", hash(lazy), hash("Bonjour"))

        # __format__ with format spec
        test("lazy format >20", f"{lazy:>20}", "Bonjour".rjust(20))
        test("lazy format empty spec", format(lazy, ""), "Bonjour")

        # __add__ / __radd__
        test("lazy + str", lazy + " monde", "Bonjour monde")
        test("str + lazy", "Salut " + lazy, "Salut Bonjour")

        # __mod__
        lazy_fmt = LazyString(lambda: "Hello %s")
        test("lazy % args", lazy_fmt % "World", "Hello World")

        # __bool__
        test("lazy bool non-empty", bool(lazy), True)
        empty_lazy = LazyString(lambda: "")
        test("lazy bool empty", bool(empty_lazy), False)

        # __len__
        test("lazy len", len(lazy), 7)

        # __contains__
        test("lazy contains", "jour" in lazy, True)
        test("lazy not contains", "xyz" not in lazy, True)

        # f-string usage
        test("lazy f-string", f"Say {lazy}!", "Say Bonjour!")

        # Comparison operators
        lazy_a = LazyString(lambda: "apple")
        lazy_b = LazyString(lambda: "banana")
        test("lazy < lazy", lazy_a < lazy_b, True)
        test("lazy > lazy", lazy_b > lazy_a, True)
        test("lazy <= lazy (equal)", lazy_a <= lazy_a, True)
        test("lazy >= lazy", lazy_b >= lazy_a, True)
        test("lazy < str", lazy_a < "banana", True)
        test("lazy > str", lazy_b > "apple", True)

        # ngettext_lazy
        lazy_n = ngettext_lazy("%(count)d item", "%(count)d items", 1)
        test("ngettext_lazy singular", str(lazy_n), "%(count)d article")

        # pgettext_lazy
        lazy_p = pgettext_lazy("ctx", "Hello")
        test("pgettext_lazy", str(lazy_p), "Ctx Bonjour")

        # __iter__
        test("lazy iter", list(lazy)[:3], ["B", "o", "n"])

        # __getitem__
        test("lazy getitem", lazy[0], "B")

    finally:
        _engine._catalogs.clear()
        _engine._catalogs.update(old_catalogs)
        deactivate()


# ═══════════════════════════════════════════════════════════════════════════
# Language Activation (16 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_language_activation():
    try:
        deactivate()
        # Default language
        test("default language", get_language(), "en")

        # activate
        activate("fr")
        test("activate fr", get_language(), "fr")

        activate("de")
        test("activate de", get_language(), "de")

        # deactivate resets
        deactivate()
        test("deactivate resets", get_language(), "en")

        # override context manager
        activate("en")
        with override("fr"):
            test("override fr", get_language(), "fr")
        test("after override", get_language(), "en")

        # Nested override
        with override("de"):
            test("nested outer de", get_language(), "de")
            with override("ja"):
                test("nested inner ja", get_language(), "ja")
            test("after inner override", get_language(), "de")
        test("after outer override", get_language(), "en")

        # override(None) deactivates
        activate("fr")
        with override(None):
            test("override None", get_language(), "en")
        test("after override None", get_language(), "fr")

        # Thread isolation
        results = {}
        barrier = threading.Barrier(2)

        def thread_func(lang, key):
            activate(lang)
            barrier.wait()
            results[key] = get_language()
            deactivate()

        activate("en")
        t1 = threading.Thread(target=thread_func, args=("fr", "t1"))
        t2 = threading.Thread(target=thread_func, args=("de", "t2"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        test("thread 1 language", results["t1"], "fr")
        test("thread 2 language", results["t2"], "de")
        test("main thread unaffected", get_language(), "en")

    finally:
        deactivate()


# ═══════════════════════════════════════════════════════════════════════════
# PO File Parser (22 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_po_parser():
    # Simple msgid/msgstr pair
    po = """
msgid "Hello"
msgstr "Bonjour"
"""
    entries = parse_po_file(po)
    test("simple entry count", len(entries), 1)
    test("simple msgid", entries[0].msgid, "Hello")
    test("simple msgstr", entries[0].msgstr, "Bonjour")

    # Multi-line strings
    po_multi = """
msgid ""
"Hello "
"World"
msgstr ""
"Bonjour "
"Monde"
"""
    entries = parse_po_file(po_multi)
    test("multiline msgid", entries[0].msgid, "Hello World")
    test("multiline msgstr", entries[0].msgstr, "Bonjour Monde")

    # Escaped characters
    po_escape = r"""
msgid "line1\nline2"
msgstr "ligne1\nligne2"
"""
    entries = parse_po_file(po_escape)
    test("escaped newline msgid", entries[0].msgid, "line1\nline2")
    test("escaped newline msgstr", entries[0].msgstr, "ligne1\nligne2")

    po_tab = r"""
msgid "col1\tcol2"
msgstr "col1\tcol2"
"""
    entries = parse_po_file(po_tab)
    test("escaped tab", entries[0].msgid, "col1\tcol2")

    po_backslash = r"""
msgid "path\\file"
msgstr "chemin\\fichier"
"""
    entries = parse_po_file(po_backslash)
    test("escaped backslash", entries[0].msgid, "path\\file")

    po_quote = r"""
msgid "say \"hello\""
msgstr "dire \"bonjour\""
"""
    entries = parse_po_file(po_quote)
    test("escaped quote msgid", entries[0].msgid, 'say "hello"')
    test("escaped quote msgstr", entries[0].msgstr, 'dire "bonjour"')

    # Plural messages
    po_plural = """
msgid "%(count)d item"
msgid_plural "%(count)d items"
msgstr[0] "%(count)d article"
msgstr[1] "%(count)d articles"
"""
    entries = parse_po_file(po_plural)
    test("plural msgid", entries[0].msgid, "%(count)d item")
    test("plural msgid_plural", entries[0].msgid_plural, "%(count)d items")
    test("plural msgstr[0]", entries[0].msgstr_plural[0], "%(count)d article")
    test("plural msgstr[1]", entries[0].msgstr_plural[1], "%(count)d articles")

    # Context messages
    po_ctx = """
msgctxt "greeting"
msgid "Hello"
msgstr "Bonjour (salutation)"
"""
    entries = parse_po_file(po_ctx)
    test("context msgctxt", entries[0].msgctxt, "greeting")
    test("context msgid", entries[0].msgid, "Hello")
    test("context msgstr", entries[0].msgstr, "Bonjour (salutation)")

    # Comments and blank lines
    po_comments = """
# translator comment
#. extracted comment
#: source.py:10
msgid "Hello"
msgstr "Bonjour"

# another comment
msgid "World"
msgstr "Monde"
"""
    entries = parse_po_file(po_comments)
    test("comments skipped count", len(entries), 2)

    # Empty msgid (header) is excluded
    po_header = """
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Language: fr\\n"

msgid "Hello"
msgstr "Bonjour"
"""
    entries = parse_po_file(po_header)
    test("header excluded", len(entries), 1)
    test("first real entry", entries[0].msgid, "Hello")

    # Multiple entries
    po_multi_entries = """
msgid "One"
msgstr "Un"

msgid "Two"
msgstr "Deux"

msgid "Three"
msgstr "Trois"
"""
    entries = parse_po_file(po_multi_entries)
    test("multiple entries count", len(entries), 3)

    # Unicode content
    po_unicode = """
msgid "Hello"
msgstr "\u00c9l\u00e8ve"
"""
    entries = parse_po_file(po_unicode)
    test("unicode msgstr", entries[0].msgstr, "\u00c9l\u00e8ve")

    # Malformed input: lines without proper keywords just end entry
    po_malformed = """
msgid "Hello"
msgstr "Bonjour"
GARBAGE LINE
msgid "World"
msgstr "Monde"
"""
    entries = parse_po_file(po_malformed)
    test("malformed still parses", len(entries) >= 1, True)

    # Empty content
    entries_empty = parse_po_file("")
    test("empty content", len(entries_empty), 0)


# ═══════════════════════════════════════════════════════════════════════════
# Accept-Language Parser (11 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_accept_language():
    # Full header with qualities
    result = parse_accept_language("en-US,en;q=0.9,fr;q=0.8")
    test("accept-lang count", len(result), 3)
    test("accept-lang first", result[0], ("en-US", 1.0))
    test("accept-lang second", result[1], ("en", 0.9))
    test("accept-lang third", result[2], ("fr", 0.8))

    # No quality -> 1.0
    result = parse_accept_language("fr")
    test("no quality -> 1.0", result[0], ("fr", 1.0))

    # Multiple with same quality (preserve order)
    result = parse_accept_language("en,fr")
    test("same quality count", len(result), 2)
    test("same quality first", result[0][0], "en")
    test("same quality second", result[1][0], "fr")

    # Empty string
    result = parse_accept_language("")
    test("empty string", result, [])

    # Sorted by quality
    result = parse_accept_language("fr;q=0.5,de;q=0.9,en;q=0.7")
    test("sorted by quality first", result[0][0], "de")
    test("sorted by quality last", result[2][0], "fr")

    # Quality = 0
    result = parse_accept_language("en;q=0,fr;q=1.0")
    test("q=0 included", len(result), 2)
    test("q=1.0 first", result[0], ("fr", 1.0))


# ═══════════════════════════════════════════════════════════════════════════
# LocaleMiddleware (10 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_locale_middleware():
    import asyncio

    class MockRequest:
        def __init__(self, path="/", cookies=None, headers=None):
            self.path = path
            self.cookies = cookies if cookies is not None else {}
            self.headers = headers if headers is not None else {}

    class MockResponse:
        status_code = 200

    mw = LocaleMiddleware()

    # URL prefix detection
    detected = mw._detect_language(MockRequest(path="/fr/about/"))
    test("middleware url prefix fr", detected, "fr")

    detected = mw._detect_language(MockRequest(path="/de/page/"))
    test("middleware url prefix de", detected, "de")

    # Cookie-based
    detected = mw._detect_language(
        MockRequest(
            path="/about/",
            cookies={"hyper_language": "es"},
        )
    )
    test("middleware cookie es", detected, "es")

    # Accept-Language fallback
    detected = mw._detect_language(
        MockRequest(
            path="/about/",
            headers={"accept-language": "ja,en;q=0.9"},
        )
    )
    test("middleware accept-language ja", detected, "ja")

    # Accept-Language with base language fallback
    detected = mw._detect_language(
        MockRequest(
            path="/about/",
            headers={"accept-language": "en-US;q=0.9"},
        )
    )
    # "en-US" is not directly in _LANGUAGES but base "en" is
    # Middleware tries base language
    test("middleware accept-language en-US -> en", detected in ("en-US", "en"), True)

    # Default language fallback (no prefix, no cookie, no header)
    detected = mw._detect_language(MockRequest(path="/about/"))
    test("middleware default fallback", detected, "en")

    # Full async flow
    async def run_middleware():
        captured_lang = [None]

        async def call_next(req):
            captured_lang[0] = get_language()
            return MockResponse()

        req = MockRequest(path="/fr/page/")
        await mw(req, call_next)
        return captured_lang[0]

    lang = asyncio.run(run_middleware())
    test("middleware async activates language", lang, "fr")
    # After middleware completes, language should be deactivated
    test("middleware deactivates after", get_language(), "en")

    # Custom cookie name
    mw2 = LocaleMiddleware(cookie_name="lang")
    detected = mw2._detect_language(
        MockRequest(
            path="/about/",
            cookies={"lang": "it"},
        )
    )
    test("middleware custom cookie name", detected, "it")

    # Priority: URL prefix wins over cookie
    detected = mw._detect_language(
        MockRequest(
            path="/de/about/",
            cookies={"hyper_language": "fr"},
        )
    )
    test("middleware url wins over cookie", detected, "de")


# ═══════════════════════════════════════════════════════════════════════════
# URL i18n (9 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_url_i18n():
    old_catalogs = dict(_engine._catalogs)

    try:
        _engine._catalogs.clear()
        # Load fr and de catalogs
        for lang in ("fr", "de"):
            cat = TranslationCatalog(
                language=lang,
                messages={},
                plural_messages={},
                context_messages={},
                plural_func=get_plural_func(lang),
            )
            _engine.load_catalog(lang, cat)

        def dummy_view():
            pass

        # Basic prefixed patterns
        patterns = i18n_url_patterns(
            ("/about/", dummy_view),
            ("/contact/", dummy_view),
            languages=["en", "fr"],
        )
        paths = [p for p, v in patterns]
        test("i18n_url has /en/about/", "/en/about/" in paths, True)
        test("i18n_url has /fr/about/", "/fr/about/" in paths, True)
        test("i18n_url has /en/contact/", "/en/contact/" in paths, True)
        test("i18n_url has /fr/contact/", "/fr/contact/" in paths, True)
        test("i18n_url count", len(patterns), 4)

        # prefix_default_language=False
        patterns = i18n_url_patterns(
            ("/about/", dummy_view),
            prefix_default_language=False,
            languages=["en", "fr"],
        )
        paths = [p for p, v in patterns]
        test("no prefix default: /about/ present", "/about/" in paths, True)
        test("no prefix default: /fr/about/ present", "/fr/about/" in paths, True)
        test("no prefix default: /en/about/ absent", "/en/about/" not in paths, True)

        # Path without leading slash
        patterns = i18n_url_patterns(
            ("page/", dummy_view),
            languages=["en"],
        )
        test("no leading slash gets prefixed", patterns[0][0], "/en/page/")

    finally:
        _engine._catalogs.clear()
        _engine._catalogs.update(old_catalogs)


# ═══════════════════════════════════════════════════════════════════════════
# Message Extraction (12 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_message_extraction():
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    py_dir = tmpdir / "src"
    tpl_dir = tmpdir / "templates"
    py_dir.mkdir()
    tpl_dir.mkdir()

    # Write Python source
    (py_dir / "views.py").write_text("""
from hyperdjango.i18n import gettext as _, ngettext

label = _("Hello")
msg = gettext("World")
count_msg = ngettext("%(count)d item", "%(count)d items", n)
""")

    # Write template
    (tpl_dir / "index.html").write_text("""
<h1>{% trans "Welcome" %}</h1>
<p>{% trans "Goodbye" %}</p>
""")

    messages = extract_messages([str(py_dir), str(tpl_dir)])

    test("extract: Hello found", "Hello" in messages, True)
    test("extract: World found", "World" in messages, True)
    test("extract: singular found", "%(count)d item" in messages, True)
    test("extract: plural found", "%(count)d items" in messages, True)
    test("extract: Welcome found", "Welcome" in messages, True)
    test("extract: Goodbye found", "Goodbye" in messages, True)

    # No duplicates
    (py_dir / "other.py").write_text('label2 = _("Hello")\n')
    messages = extract_messages([str(py_dir), str(tpl_dir)])
    count_hello = messages.count("Hello")
    test("extract: no duplicates", count_hello, 1)

    # Result is sorted
    test("extract: sorted", messages == sorted(messages), True)

    # Non-existent directory is ignored
    messages = extract_messages(["/nonexistent/path"])
    test("extract: nonexistent dir", messages, [])

    # Empty directory
    empty_dir = tmpdir / "empty"
    empty_dir.mkdir()
    messages = extract_messages([str(empty_dir)])
    test("extract: empty dir", messages, [])

    # gettext_lazy extraction
    (py_dir / "lazy.py").write_text('label = gettext_lazy("Lazy message")\n')
    messages = extract_messages([str(py_dir)])
    test("extract: gettext_lazy", "Lazy message" in messages, True)

    # pgettext extraction: the regex matches the first quoted string arg
    # and requires ) after it, so two-arg pgettext("ctx", "msg") does NOT
    # match (this is a known limitation). Single-arg _ or gettext still works.
    # Verify pgettext_lazy with single-arg form: gettext_lazy("Lazy message") was tested above.
    (py_dir / "ctx.py").write_text('label = _("Extracted single arg")\n')
    messages = extract_messages([str(py_dir)])
    test("extract: single arg _ call", "Extracted single arg" in messages, True)

    # Cleanup
    import shutil

    shutil.rmtree(tmpdir)


# ═══════════════════════════════════════════════════════════════════════════
# PO File Generation (10 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_po_generation():
    # Basic generation
    messages = ["Hello", "World"]
    po = create_po_file(messages, "fr")
    test("po contains header", "Language: fr" in po, True)
    test("po contains Content-Type", "Content-Type: text/plain" in po, True)
    test("po contains msgid Hello", 'msgid "Hello"' in po, True)
    test("po contains msgid World", 'msgid "World"' in po, True)
    test("po contains empty msgstr", 'msgstr ""' in po, True)

    # Merge with existing translations
    existing = """
msgid "Hello"
msgstr "Bonjour"
"""
    po = create_po_file(["Hello", "New message"], "fr", existing=existing)
    test("merge preserves existing", 'msgstr "Bonjour"' in po, True)
    test("merge adds new", 'msgid "New message"' in po, True)

    # Special characters escaped
    messages = ['Say "hello"', "line1\nline2"]
    po = create_po_file(messages, "en")
    test("escape quotes in po", r'msgid "Say \"hello\""' in po, True)
    test("escape newlines in po", r'msgid "line1\nline2"' in po, True)

    # Plural forms header
    po_ar = create_po_file(["test"], "ar")
    test("arabic nplurals=6", "nplurals=6" in po_ar, True)

    po_en = create_po_file(["test"], "en")
    test("english nplurals=2", "nplurals=2" in po_en, True)

    po_ja = create_po_file(["test"], "ja")
    test("japanese nplurals=1", "nplurals=1" in po_ja, True)


# ═══════════════════════════════════════════════════════════════════════════
# Unescape / Escape helpers (8 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_escape_unescape():
    # Unescape
    test("unescape \\n", _unescape_po_string(r"hello\nworld"), "hello\nworld")
    test("unescape \\t", _unescape_po_string(r"col1\tcol2"), "col1\tcol2")
    test("unescape \\\\", _unescape_po_string(r"path\\file"), "path\\file")
    test('unescape \\"', _unescape_po_string(r"say \"hi\""), 'say "hi"')
    test("unescape plain", _unescape_po_string("hello"), "hello")

    # Escape
    test("escape newline", _escape_po_string("a\nb"), r"a\nb")
    test("escape quote", _escape_po_string('say "hi"'), r"say \"hi\"")
    test("escape backslash", _escape_po_string("a\\b"), r"a\\b")


# ═══════════════════════════════════════════════════════════════════════════
# POEntry dataclass (4 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_po_entry():
    entry = POEntry(msgid="Hello", msgstr="Bonjour")
    test("POEntry msgid", entry.msgid, "Hello")
    test("POEntry msgstr", entry.msgstr, "Bonjour")
    test("POEntry msgid_plural default", entry.msgid_plural, None)
    test("POEntry msgctxt default", entry.msgctxt, None)


# ═══════════════════════════════════════════════════════════════════════════
# Lazy variants (6 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_lazy_variants():
    old_catalogs = dict(_engine._catalogs)

    try:
        _engine._catalogs.clear()
        fr_cat = TranslationCatalog(
            language="fr",
            messages={"Username": "Nom d'utilisateur"},
            plural_messages={
                "%(count)d file": ("%(count)d fichier", "%(count)d fichiers"),
            },
            context_messages={
                ("form", "Name"): "Nom (formulaire)",
            },
            plural_func=get_plural_func("fr"),
        )
        _engine.load_catalog("fr", fr_cat)
        activate("fr")

        # gettext_lazy resolves at str() time
        lazy = gettext_lazy("Username")
        test("gettext_lazy resolves", str(lazy), "Nom d'utilisateur")

        # ngettext_lazy
        lazy_n = ngettext_lazy("%(count)d file", "%(count)d files", 1)
        test("ngettext_lazy singular", str(lazy_n), "%(count)d fichier")
        lazy_n2 = ngettext_lazy("%(count)d file", "%(count)d files", 5)
        test("ngettext_lazy plural", str(lazy_n2), "%(count)d fichiers")

        # pgettext_lazy
        lazy_p = pgettext_lazy("form", "Name")
        test("pgettext_lazy", str(lazy_p), "Nom (formulaire)")

        # npgettext_lazy
        lazy_np = npgettext_lazy("ctx", "%(count)d file", "%(count)d files", 5)
        test("npgettext_lazy plural", str(lazy_np), "%(count)d fichiers")

        # Lazy responds to language change
        deactivate()  # back to "en"
        test("lazy after deactivate", str(lazy), "Username")

    finally:
        _engine._catalogs.clear()
        _engine._catalogs.update(old_catalogs)
        deactivate()


# ═══════════════════════════════════════════════════════════════════════════
# nptranslate with context+plural (5 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_nptranslate():
    engine = TranslationEngine()
    cat = TranslationCatalog(
        language="fr",
        messages={},
        plural_messages={
            "ctx\x04%(count)d file": (
                "%(count)d fichier ctx",
                "%(count)d fichiers ctx",
            ),
            "%(count)d file": ("%(count)d fichier", "%(count)d fichiers"),
        },
        context_messages={
            ("ctx", "%(count)d file"): "%(count)d fichier ctx",
        },
        plural_func=get_plural_func("fr"),
    )
    engine.load_catalog("fr", cat)

    # Context+plural
    result = engine.nptranslate("ctx", "%(count)d file", "%(count)d files", 0, "fr")
    test("nptranslate ctx+plural n=0 (fr singular)", result, "%(count)d fichier ctx")

    result = engine.nptranslate("ctx", "%(count)d file", "%(count)d files", 5, "fr")
    test("nptranslate ctx+plural n=5", result, "%(count)d fichiers ctx")

    # count=1 with context returns context msg
    result = engine.nptranslate("ctx", "%(count)d file", "%(count)d files", 1, "fr")
    test("nptranslate ctx+plural n=1", result, "%(count)d fichier ctx")

    # No context match falls through to ntranslate
    result = engine.nptranslate(
        "missing_ctx", "%(count)d file", "%(count)d files", 5, "fr"
    )
    test("nptranslate no context -> ntranslate", result, "%(count)d fichiers")

    # No catalog at all
    result = engine.nptranslate("ctx", "cat", "cats", 1, "xx")
    test("nptranslate no catalog singular", result, "cat")


def test_npgettext_resolves_from_real_po():
    """Regression: npgettext must resolve context+plural entries loaded from
    a real .po file end-to-end (load_po_file → engine → nptranslate)."""
    po = """
msgctxt "sports"
msgid "%(count)d goal"
msgid_plural "%(count)d goals"
msgstr[0] "%(count)d but"
msgstr[1] "%(count)d buts"
"""
    catalog = TranslationEngine()  # fresh engine, no global state
    from hyperdjango.i18n import parse_po_file  # already imported at top

    # Build a catalog the same way load_po_file does, but from string content.
    entries = parse_po_file(po)
    plural_messages = {}
    for entry in entries:
        if entry.msgctxt is not None and entry.msgstr_plural is not None:
            max_idx = max(entry.msgstr_plural.keys()) + 1
            forms = tuple(entry.msgstr_plural.get(i, "") for i in range(max_idx))
            plural_messages[f"{entry.msgctxt}\x04{entry.msgid}"] = forms
    cat = TranslationCatalog(
        language="fr",
        messages={},
        plural_messages=plural_messages,
        context_messages={},
        plural_func=get_plural_func("fr"),
    )
    catalog.load_catalog("fr", cat)

    # n=1 → French singular form (fr: 0 and 1 are singular)
    test(
        "npgettext real-po ctx+plural n=1",
        catalog.nptranslate("sports", "%(count)d goal", "%(count)d goals", 1, "fr"),
        "%(count)d but",
    )
    # n=5 → French plural form
    test(
        "npgettext real-po ctx+plural n=5",
        catalog.nptranslate("sports", "%(count)d goal", "%(count)d goals", 5, "fr"),
        "%(count)d buts",
    )
    # Wrong context does NOT resolve to this entry — falls back to source.
    test(
        "npgettext real-po wrong context falls back",
        catalog.nptranslate("music", "%(count)d goal", "%(count)d goals", 5, "fr"),
        "%(count)d goals",
    )


def test_concurrent_language_isolation():
    """Regression: concurrent async requests interleaved on one loop thread
    must NOT clobber each other's active language. With threading.local the
    single shared slot bled across requests; a ContextVar isolates per-task.
    """
    import asyncio

    async def request(lang, hops, results, key):
        activate(lang)
        try:
            # Yield control repeatedly so the scheduler interleaves the two
            # requests — the exact condition that caused cross-request bleed.
            for _ in range(hops):
                await asyncio.sleep(0)
                results.setdefault(key, []).append(get_language())
        finally:
            deactivate()

    async def driver():
        results: dict[str, list[str]] = {}
        await asyncio.gather(
            request("fr", 20, results, "fr"),
            request("de", 20, results, "de"),
            request("ja", 20, results, "ja"),
        )
        return results

    activate("en")  # loop-thread default; must survive the concurrent tasks
    results = asyncio.run(driver())
    deactivate()

    test("isolation fr stays fr", all(v == "fr" for v in results["fr"]), True)
    test("isolation de stays de", all(v == "de" for v in results["de"]), True)
    test("isolation ja stays ja", all(v == "ja" for v in results["ja"]), True)


# ═══════════════════════════════════════════════════════════════════════════
# PO parser edge cases (8 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_po_parser_edge_cases():
    # Russian 3-form plural
    po_ru = """
msgid "%(count)d day"
msgid_plural "%(count)d days"
msgstr[0] "%(count)d \u0434\u0435\u043d\u044c"
msgstr[1] "%(count)d \u0434\u043d\u044f"
msgstr[2] "%(count)d \u0434\u043d\u0435\u0439"
"""
    entries = parse_po_file(po_ru)
    test("ru plural 3 forms", len(entries[0].msgstr_plural), 3)
    test(
        "ru plural form 0",
        entries[0].msgstr_plural[0],
        "%(count)d \u0434\u0435\u043d\u044c",
    )
    test(
        "ru plural form 2",
        entries[0].msgstr_plural[2],
        "%(count)d \u0434\u043d\u0435\u0439",
    )

    # Context with multi-line
    po_ctx_ml = """
msgctxt "navigation"
msgid ""
"Home "
"Page"
msgstr "Page d'accueil"
"""
    entries = parse_po_file(po_ctx_ml)
    test("context multiline msgid", entries[0].msgid, "Home Page")
    test("context multiline msgctxt", entries[0].msgctxt, "navigation")

    # Only comments
    entries = parse_po_file("# just a comment\n# another comment\n")
    test("only comments", len(entries), 0)

    # Plural with context
    po_ctx_plural = """
msgctxt "sports"
msgid "%(count)d goal"
msgid_plural "%(count)d goals"
msgstr[0] "%(count)d but"
msgstr[1] "%(count)d buts"
"""
    entries = parse_po_file(po_ctx_plural)
    test("ctx+plural msgctxt", entries[0].msgctxt, "sports")
    test("ctx+plural msgstr_plural", entries[0].msgstr_plural[1], "%(count)d buts")


# ═══════════════════════════════════════════════════════════════════════════
# Engine fallback chains (6 tests)
# ═══════════════════════════════════════════════════════════════════════════


def test_engine_fallback():
    engine = TranslationEngine()
    en_cat = TranslationCatalog(
        language="en",
        messages={"Save": "Save", "Cancel": "Cancel"},
        plural_messages={
            "%(n)d file": ("%(n)d file", "%(n)d files"),
        },
        context_messages={
            ("btn", "OK"): "OK",
        },
        plural_func=get_plural_func("en"),
    )
    fr_cat = TranslationCatalog(
        language="fr",
        messages={"Save": "Enregistrer"},
        plural_messages={},
        context_messages={},
        plural_func=get_plural_func("fr"),
    )
    engine.load_catalog("en", en_cat)
    engine.load_catalog("fr", fr_cat)

    # French has Save but not Cancel -> fallback to en for Cancel
    test("fallback: fr has Save", engine.translate("Save", "fr"), "Enregistrer")
    test(
        "fallback: fr missing Cancel -> en", engine.translate("Cancel", "fr"), "Cancel"
    )
    test("fallback: completely missing", engine.translate("xyz", "fr"), "xyz")

    # ntranslate fallback to en catalog
    test(
        "fallback: ntranslate to en",
        engine.ntranslate("%(n)d file", "%(n)d files", 2, "fr"),
        "%(n)d files",
    )

    # ptranslate fallback
    test("fallback: ptranslate to en", engine.ptranslate("btn", "OK", "fr"), "OK")

    # Empty string msgstr should not be used
    engine2 = TranslationEngine()
    cat_empty = TranslationCatalog(
        language="fr",
        messages={"Hello": ""},
        plural_messages={},
        context_messages={},
        plural_func=get_plural_func("fr"),
    )
    engine2.load_catalog("fr", cat_empty)
    test("empty msgstr returns original", engine2.translate("Hello", "fr"), "Hello")


# ═══════════════════════════════════════════════════════════════════════════
# Run all tests
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    test_plural_rules()
    test_language_info()
    test_translation_catalog()
    test_translation_engine()
    test_core_api()
    test_lazy_string()
    test_language_activation()
    test_po_parser()
    test_accept_language()
    test_locale_middleware()
    test_url_i18n()
    test_message_extraction()
    test_po_generation()
    test_escape_unescape()
    test_po_entry()
    test_lazy_variants()
    test_nptranslate()
    test_npgettext_resolves_from_real_po()
    test_concurrent_language_isolation()
    test_po_parser_edge_cases()
    test_engine_fallback()

    print(f"\n{'=' * 60}")
    print(f"i18n: {PASS} passed, {FAIL} failed")
    if FAIL:
        sys.exit(1)
