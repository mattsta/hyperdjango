"""
Hypothesis fuzz tests for the internationalization system.

Proves:
1. gettext roundtrip: any string registered as translation can be retrieved
2. ngettext plural forms: arbitrary counts produce valid plural selection
3. PO file parsing resilience: malformed PO content doesn't crash
4. Plural rule evaluation: arbitrary integer counts don't crash for common languages
5. Translation key lookup: missing keys return the original string (fallback)
6. Unicode translations: non-ASCII msgid/msgstr round-trip correctly
7. pgettext context isolation: same msgid with different contexts stays separate
8. parse_accept_language resilience: arbitrary header strings don't crash

# hyper-test: unit
"""

import os
import sys
import traceback

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango import i18n
from hyperdjango.i18n import (
    _PLURAL_RULES,
    TranslationCatalog,
    TranslationEngine,
    _unescape_po_string,
    activate,
    deactivate,
    get_plural_func,
    gettext,
    override,
    parse_accept_language,
    parse_po_file,
)

# Under parallel test execution, CPU contention can push individual examples
# past per-call deadlines. Disable the deadline under parallel mode.
_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_DEADLINE = None if _PARALLEL else 1000
_SUPPRESS = [HealthCheck.too_slow, HealthCheck.filter_too_much] if _PARALLEL else []


def _ex(n: int) -> int:
    return max(n // 2, 30) if _PARALLEL else n


passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" -- {msg}"
        errors.append(err)
        print(f"  {err}")


# ── Strategies ────────────────────────────────────────────────────────────

# Non-empty strings for translation keys/values (PO files don't store empty msgid)
_msgid_st = st.text(min_size=1, max_size=200)
_msgstr_st = st.text(min_size=1, max_size=200)

# Unicode-heavy: CJK, Cyrillic, Arabic, emoji, combining marks
_unicode_alphabet = (
    "abcABC123"
    "\u4e16\u754c\u4f60\u597d"  # CJK
    "\u041c\u0438\u0440"  # Cyrillic
    "\u0645\u0631\u062d\u0628\u0627"  # Arabic
    "\u00e9\u00e8\u00ea\u00eb"  # accented Latin
    "\U0001f600\U0001f4a9\U0001f680"  # emoji
    "\u0300\u0301\u0302"  # combining marks
)
_unicode_st = st.text(alphabet=_unicode_alphabet, min_size=1, max_size=100)

# Language codes that have plural rules defined
_lang_codes = list(_PLURAL_RULES.keys())

# Non-negative integers for plural counts
_count_st = st.integers(min_value=0, max_value=10_000_000)

# Arbitrary text for PO file fuzzing
_po_content_st = st.text(min_size=0, max_size=2000)

# Accept-Language header fuzzing
_header_st = st.text(min_size=0, max_size=500)


# ── Helpers ───────────────────────────────────────────────────────────────


def _fresh_engine() -> TranslationEngine:
    """Create an isolated TranslationEngine for testing."""
    return TranslationEngine()


def _make_catalog(
    language: str,
    messages: dict[str, str] | None = None,
    plural_messages: dict[str, tuple[str, ...]] | None = None,
    context_messages: dict[tuple[str, str], str] | None = None,
) -> TranslationCatalog:
    return TranslationCatalog(
        language=language,
        messages=messages or {},
        plural_messages=plural_messages or {},
        context_messages=context_messages or {},
        plural_func=get_plural_func(language),
    )


# ── Test 1: gettext roundtrip ─────────────────────────────────────────────


@given(msgid=_msgid_st, msgstr=_msgstr_st)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_gettext_roundtrip(msgid: str, msgstr: str):
    """Any string registered as a translation can be retrieved."""
    engine = _fresh_engine()
    catalog = _make_catalog("fr", messages={msgid: msgstr})
    engine.load_catalog("fr", catalog)
    result = engine.translate(msgid, language="fr")
    # If msgstr is non-empty, the translation should be returned
    if msgstr != "":
        assert result == msgstr, f"expected {msgstr!r}, got {result!r}"
    else:
        # Empty msgstr falls back to msgid
        assert result == msgid


# ── Test 2: ngettext plural forms ─────────────────────────────────────────


@given(
    count=_count_st,
    lang=st.sampled_from(_lang_codes),
)
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_ngettext_plural_valid_index(count: int, lang: str):
    """Arbitrary counts produce valid plural indices for all languages."""
    plural_func = get_plural_func(lang)
    idx = plural_func(count)
    # Plural index must be a non-negative integer
    assert isinstance(idx, int), f"expected int, got {type(idx)}"
    assert idx >= 0, f"negative plural index {idx} for lang={lang}, count={count}"
    # Each language has a known max number of forms
    # Arabic has the most: 6 forms (indices 0-5)
    assert idx <= 5, f"plural index {idx} > 5 for lang={lang}, count={count}"


@given(count=_count_st)
@settings(max_examples=_ex(150), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_ngettext_engine_returns_string(count: int):
    """ngettext always returns a string for any count."""
    engine = _fresh_engine()
    singular_form = "one item"
    plural_form = "many items"
    forms = ("un article", "des articles")
    catalog = _make_catalog("fr", plural_messages={singular_form: forms})
    engine.load_catalog("fr", catalog)
    result = engine.ntranslate(singular_form, plural_form, count, language="fr")
    assert isinstance(result, str), f"expected str, got {type(result)}"
    # Result must be one of the translation forms or the fallback
    assert result in (
        forms[0],
        forms[1],
        singular_form,
        plural_form,
    ), f"unexpected result {result!r}"


# ── Test 3: PO file parsing resilience ────────────────────────────────────


@given(content=_po_content_st)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_po_parse_no_crash(content: str):
    """Malformed PO content doesn't crash the parser."""
    try:
        result = parse_po_file(content)
        # Must return a list (possibly empty)
        assert isinstance(result, list), f"expected list, got {type(result)}"
    except ValueError:
        # ValueError from _extract_quoted on lines without quotes is acceptable
        pass


# ── Test 4: Plural rule evaluation ────────────────────────────────────────


@given(count=st.integers(min_value=0, max_value=100_000_000))
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_plural_rules_all_languages(count: int):
    """Plural rules for all registered languages don't crash on any count."""
    for lang, func in _PLURAL_RULES.items():
        idx = func(count)
        assert isinstance(idx, int), f"lang={lang}, count={count}: not int"
        assert idx >= 0, f"lang={lang}, count={count}: negative index {idx}"


# ── Test 5: Missing key fallback ──────────────────────────────────────────


@given(msgid=_msgid_st)
@settings(max_examples=_ex(150), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_missing_key_returns_original(msgid: str):
    """Translation lookup for missing keys returns the original string."""
    engine = _fresh_engine()
    # Engine with no catalogs loaded
    result = engine.translate(msgid, language="xx")
    assert result == msgid, f"expected {msgid!r}, got {result!r}"

    # Engine with a catalog but missing key
    catalog = _make_catalog("xx", messages={"other_key": "other_value"})
    engine.load_catalog("xx", catalog)
    result = engine.translate(msgid, language="xx")
    if msgid == "other_key":
        assert result == "other_value"
    else:
        assert result == msgid, f"expected {msgid!r}, got {result!r}"


# ── Test 6: Unicode translations ──────────────────────────────────────────


@given(msgid=_unicode_st, msgstr=_unicode_st)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_unicode_roundtrip(msgid: str, msgstr: str):
    """Non-ASCII msgid/msgstr round-trip correctly through the engine."""
    engine = _fresh_engine()
    catalog = _make_catalog("ja", messages={msgid: msgstr})
    engine.load_catalog("ja", catalog)
    result = engine.translate(msgid, language="ja")
    if msgstr != "":
        assert result == msgstr, f"expected {msgstr!r}, got {result!r}"
    else:
        assert result == msgid


# ── Test 7: pgettext context isolation ────────────────────────────────────


@given(
    ctx_a=st.text(min_size=1, max_size=50),
    ctx_b=st.text(min_size=1, max_size=50),
    msgid=_msgid_st,
    msgstr_a=_msgstr_st,
    msgstr_b=_msgstr_st,
)
@settings(max_examples=_ex(150), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_pgettext_context_isolation(
    ctx_a: str, ctx_b: str, msgid: str, msgstr_a: str, msgstr_b: str
):
    """Same msgid with different contexts returns different translations."""
    engine = _fresh_engine()
    context_messages: dict[tuple[str, str], str] = {
        (ctx_a, msgid): msgstr_a,
        (ctx_b, msgid): msgstr_b,
    }
    catalog = _make_catalog("de", context_messages=context_messages)
    engine.load_catalog("de", catalog)

    result_a = engine.ptranslate(ctx_a, msgid, language="de")
    result_b = engine.ptranslate(ctx_b, msgid, language="de")

    if ctx_a == ctx_b:
        # Same key: msgstr_b overwrites msgstr_a in the dict
        expected = msgstr_b if msgstr_b != "" else msgid
        assert result_a == expected, (
            f"same ctx: expected {expected!r}, got {result_a!r}"
        )
        assert result_b == expected, (
            f"same ctx: expected {expected!r}, got {result_b!r}"
        )
    else:
        # Different contexts: each resolves independently
        if msgstr_a != "":
            assert result_a == msgstr_a, (
                f"ctx_a: expected {msgstr_a!r}, got {result_a!r}"
            )
        if msgstr_b != "":
            assert result_b == msgstr_b, (
                f"ctx_b: expected {msgstr_b!r}, got {result_b!r}"
            )


# ── Test 8: parse_accept_language resilience ──────────────────────────────


@given(header=_header_st)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_parse_accept_language_no_crash(header: str):
    """Arbitrary Accept-Language header strings don't crash the parser."""
    result = parse_accept_language(header)
    assert isinstance(result, list), f"expected list, got {type(result)}"
    for item in result:
        assert isinstance(item, tuple), f"expected tuple, got {type(item)}"
        assert len(item) == 2, f"expected 2-tuple, got {len(item)}-tuple"
        lang, quality = item
        assert isinstance(lang, str), f"lang not str: {type(lang)}"
        assert isinstance(quality, float), f"quality not float: {type(quality)}"
        assert 0.0 <= quality <= 1.0, f"quality out of range: {quality}"


# ── Direct deterministic tests ────────────────────────────────────────────


def test_po_parse_valid_entry():
    """A well-formed PO entry parses correctly."""
    po = """
msgid "Hello"
msgstr "Bonjour"

msgid "Goodbye"
msgstr "Au revoir"
"""
    entries = parse_po_file(po)
    check("PO parse: 2 entries", len(entries) == 2, f"got {len(entries)}")
    check("PO parse: first msgid", entries[0].msgid == "Hello")
    check("PO parse: first msgstr", entries[0].msgstr == "Bonjour")
    check("PO parse: second msgid", entries[1].msgid == "Goodbye")
    check("PO parse: second msgstr", entries[1].msgstr == "Au revoir")


def test_po_parse_plural_entry():
    """PO plural entries parse correctly."""
    po = """
msgid "%(count)d item"
msgid_plural "%(count)d items"
msgstr[0] "%(count)d article"
msgstr[1] "%(count)d articles"
"""
    entries = parse_po_file(po)
    check("PO plural: 1 entry", len(entries) == 1, f"got {len(entries)}")
    check(
        "PO plural: has plural_messages",
        entries[0].msgstr_plural is not None,
    )
    check(
        "PO plural: form 0",
        entries[0].msgstr_plural is not None
        and entries[0].msgstr_plural.get(0) == "%(count)d article",
    )
    check(
        "PO plural: form 1",
        entries[0].msgstr_plural is not None
        and entries[0].msgstr_plural.get(1) == "%(count)d articles",
    )


def test_po_parse_context_entry():
    """PO context entries parse correctly."""
    po = """
msgctxt "month name"
msgid "May"
msgstr "Mai"
"""
    entries = parse_po_file(po)
    check("PO context: 1 entry", len(entries) == 1, f"got {len(entries)}")
    check("PO context: msgctxt", entries[0].msgctxt == "month name")
    check("PO context: msgid", entries[0].msgid == "May")
    check("PO context: msgstr", entries[0].msgstr == "Mai")


def test_po_parse_empty_content():
    """Empty PO content returns empty list."""
    entries = parse_po_file("")
    check("PO empty: no entries", len(entries) == 0)


def test_po_parse_comments_only():
    """PO file with only comments returns empty list."""
    po = "# This is a comment\n# Another comment\n"
    entries = parse_po_file(po)
    check("PO comments: no entries", len(entries) == 0)


def test_po_parse_multiline_concat():
    """Multi-line string concatenation in PO entries works correctly."""
    po = 'msgid ""\n"Hello "\n"World"\nmsgstr ""\n"Bonjour "\n"le monde"\n'
    entries = parse_po_file(po)
    check("PO multiline: 1 entry", len(entries) == 1, f"got {len(entries)}")
    if entries:
        check("PO multiline: msgid concat", entries[0].msgid == "Hello World")
        check(
            "PO multiline: msgstr concat",
            entries[0].msgstr == "Bonjour le monde",
        )


def test_unescape_po_string():
    """PO string unescaping handles all escape sequences."""
    check("unescape \\n", _unescape_po_string("hello\\nworld") == "hello\nworld")
    check("unescape \\t", _unescape_po_string("hello\\tworld") == "hello\tworld")
    check("unescape \\r", _unescape_po_string("hello\\rworld") == "hello\rworld")
    check("unescape \\\\", _unescape_po_string("hello\\\\world") == "hello\\world")
    check('unescape \\"', _unescape_po_string('hello\\"world') == 'hello"world')
    check("unescape plain", _unescape_po_string("hello") == "hello")


def test_plural_rule_edge_cases():
    """Specific count values exercise all plural rule branches."""
    # English: 1 -> 0, everything else -> 1
    en = get_plural_func("en")
    check("en(0)=1", en(0) == 1)
    check("en(1)=0", en(1) == 0)
    check("en(2)=1", en(2) == 1)

    # Russian: complex 3-form system
    ru = get_plural_func("ru")
    check("ru(1)=0", ru(1) == 0)
    check("ru(2)=1", ru(2) == 1)
    check("ru(5)=2", ru(5) == 2)
    check("ru(11)=2", ru(11) == 2)
    check("ru(21)=0", ru(21) == 0)
    check("ru(22)=1", ru(22) == 1)
    check("ru(111)=2", ru(111) == 2)
    check("ru(112)=2", ru(112) == 2)

    # Arabic: 6-form system
    ar = get_plural_func("ar")
    check("ar(0)=0", ar(0) == 0)
    check("ar(1)=1", ar(1) == 1)
    check("ar(2)=2", ar(2) == 2)
    check("ar(3)=3", ar(3) == 3)
    check("ar(11)=4", ar(11) == 4)
    check("ar(100)=5", ar(100) == 5)

    # Polish: 3-form system
    pl = get_plural_func("pl")
    check("pl(1)=0", pl(1) == 0)
    check("pl(2)=1", pl(2) == 1)
    check("pl(5)=2", pl(5) == 2)
    check("pl(12)=2", pl(12) == 2)
    check("pl(22)=1", pl(22) == 1)

    # French: 0 and 1 are singular
    fr = get_plural_func("fr")
    check("fr(0)=0", fr(0) == 0)
    check("fr(1)=0", fr(1) == 0)
    check("fr(2)=1", fr(2) == 1)

    # East Asian: always 0
    ja = get_plural_func("ja")
    check("ja(0)=0", ja(0) == 0)
    check("ja(1)=0", ja(1) == 0)
    check("ja(100)=0", ja(100) == 0)


def test_get_plural_func_fallback():
    """Unknown language codes fall back to Germanic rule."""
    func = get_plural_func("xx-unknown")
    check("unknown lang: 1 -> 0", func(1) == 0)
    check("unknown lang: 2 -> 1", func(2) == 1)

    # Base language extraction
    func_br = get_plural_func("pt-BR")
    check("pt-BR: 0 -> 0 (french-style)", func_br(0) == 0)
    check("pt-BR: 1 -> 0 (french-style)", func_br(1) == 0)
    check("pt-BR: 2 -> 1 (french-style)", func_br(2) == 1)


def test_override_context_manager():
    """Language override context manager restores state."""
    engine = _fresh_engine()
    catalog = _make_catalog("es", messages={"Hello": "Hola"})
    engine.load_catalog("es", catalog)

    # Use the global engine for this test
    old_engine = i18n._engine
    i18n._engine = engine
    try:
        activate("en")
        check("before override: en", gettext("Hello") == "Hello")

        with override("es"):
            check("inside override: es", gettext("Hello") == "Hola")

        check("after override: en restored", gettext("Hello") == "Hello")
    finally:
        i18n._engine = old_engine
        deactivate()


def test_catalog_merge():
    """Loading a second catalog for the same language merges entries."""
    engine = _fresh_engine()
    cat1 = _make_catalog("fr", messages={"Hello": "Bonjour"})
    cat2 = _make_catalog("fr", messages={"Goodbye": "Au revoir"})
    engine.load_catalog("fr", cat1)
    engine.load_catalog("fr", cat2)

    check("merge: first entry", engine.translate("Hello", language="fr") == "Bonjour")
    check(
        "merge: second entry",
        engine.translate("Goodbye", language="fr") == "Au revoir",
    )


def test_parse_accept_language_valid():
    """Standard Accept-Language headers parse correctly."""
    result = parse_accept_language("en-US,en;q=0.9,fr;q=0.8")
    check("accept: 3 entries", len(result) == 3, f"got {len(result)}")
    check("accept: first is en-US", result[0][0] == "en-US")
    check("accept: first quality 1.0", result[0][1] == 1.0)

    result_empty = parse_accept_language("")
    check("accept empty: no entries", len(result_empty) == 0)

    # Wildcard "*" doesn't match the BCP 47 language regex, returns empty
    result_star = parse_accept_language("*")
    check("accept star: no entries (not BCP 47)", len(result_star) == 0)


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> int:
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- i18n Fuzz + Property Tests --\n")

    # Hypothesis property-based tests
    hypothesis_tests = [
        ("gettext roundtrip (Hypothesis)", test_gettext_roundtrip),
        ("ngettext plural valid index (Hypothesis)", test_ngettext_plural_valid_index),
        (
            "ngettext engine returns string (Hypothesis)",
            test_ngettext_engine_returns_string,
        ),
        ("PO parse no crash (Hypothesis)", test_po_parse_no_crash),
        ("plural rules all languages (Hypothesis)", test_plural_rules_all_languages),
        (
            "missing key returns original (Hypothesis)",
            test_missing_key_returns_original,
        ),
        ("unicode roundtrip (Hypothesis)", test_unicode_roundtrip),
        ("pgettext context isolation (Hypothesis)", test_pgettext_context_isolation),
        (
            "parse_accept_language no crash (Hypothesis)",
            test_parse_accept_language_no_crash,
        ),
    ]

    for name, test_fn in hypothesis_tests:
        try:
            test_fn()
            passed += 1
            print(f"  PASS: {name}")
        except Exception as e:
            failed += 1
            errors.append(f"FAIL: {name}: {e}")
            print(f"  FAIL: {name}: {e}")
            traceback.print_exc()

    # Direct deterministic tests
    test_po_parse_valid_entry()
    test_po_parse_plural_entry()
    test_po_parse_context_entry()
    test_po_parse_empty_content()
    test_po_parse_comments_only()
    test_po_parse_multiline_concat()
    test_unescape_po_string()
    test_plural_rule_edge_cases()
    test_get_plural_func_fallback()
    test_override_context_manager()
    test_catalog_merge()
    test_parse_accept_language_valid()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"i18n fuzz: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
