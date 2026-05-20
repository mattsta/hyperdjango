"""
Hypothesis property tests for the Zig SIMD JSON parser.

Proves correctness properties:
1. json_loads(json_dumps(x)) == x for ANY Python object
2. Zig json_loads matches stdlib json.loads for ANY valid JSON
3. Unicode roundtrip: CJK, emoji, escaped sequences
4. Nested structures roundtrip correctly
5. Zig json_dumps matches stdlib json.dumps output
6. EXACT differential vs stdlib on the blind spots the strategies never reach —
   big ints > 2^53, correctly-rounded floats, raw \\u escapes.

# hyper-test: unit
"""

import json

from hyperdjango._hyperdjango_native import json_dumps_native, json_loads_native
from hypothesis import example, given, settings
from hypothesis import strategies as st

from hyperdjango.testkit import check, finish, run_main, run_property

# Native-boundary property counts. The Zig calls are microsecond-cheap, so these
# keep the whole file well under a minute while still exercising thousands of
# generated documents; the nesting sweeps walk a bounded depth strategy so they
# stay small on purpose.
_DOC_EXAMPLES = 300
_NESTING_EXAMPLES = 30


# ---------------------------------------------------------------------------
# Strategies for JSON-compatible Python objects
# ---------------------------------------------------------------------------

json_atoms = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(min_value=-1e15, max_value=1e15, allow_nan=False, allow_infinity=False),
    st.text(max_size=100),
)

# Recursive JSON structures (dicts/lists of atoms)
json_values = st.recursive(
    json_atoms,
    lambda children: st.one_of(
        st.lists(children, max_size=10),
        st.dictionaries(st.text(min_size=1, max_size=20), children, max_size=10),
    ),
    max_leaves=50,
)

# Comparing decoded floats needs a tolerance; the byte-exact float behaviour is
# pinned separately by the differential corpus below.
_FLOAT_TOL = 1e-10


# ---------------------------------------------------------------------------
# Property 1: Zig roundtrip json_loads(json_dumps(x)) == x
# ---------------------------------------------------------------------------


@given(obj=json_values)
@settings(max_examples=_DOC_EXAMPLES)
@example(obj={})
@example(obj=[])
@example(obj="")
def prop_zig_roundtrip(obj):
    """json_loads(json_dumps(x)) == x for ANY JSON-compatible object."""
    dumped = json_dumps_native(obj)
    loaded = json_loads_native(dumped)
    if isinstance(obj, float):
        assert abs(loaded - obj) < _FLOAT_TOL, f"Float mismatch: {obj} -> {loaded}"
    else:
        assert loaded == obj, f"Roundtrip failed: {obj!r} -> {dumped!r} -> {loaded!r}"


# ---------------------------------------------------------------------------
# Property 2: Zig json_loads matches stdlib for valid JSON strings
# ---------------------------------------------------------------------------


@given(obj=json_values)
@settings(max_examples=_DOC_EXAMPLES)
def prop_zig_matches_stdlib_loads(obj):
    """Zig json_loads produces same result as stdlib json.loads."""
    json_str = json.dumps(obj, ensure_ascii=False)
    zig_result = json_loads_native(json_str)
    stdlib_result = json.loads(json_str)
    if isinstance(stdlib_result, float):
        assert abs(zig_result - stdlib_result) < _FLOAT_TOL
    else:
        assert zig_result == stdlib_result, (
            f"Mismatch for JSON: {json_str!r}\n"
            f"  zig:    {zig_result!r}\n"
            f"  stdlib: {stdlib_result!r}"
        )


# ---------------------------------------------------------------------------
# Property 3: Unicode roundtrip
# ---------------------------------------------------------------------------


@given(
    text=st.text(
        alphabet=st.characters(
            categories=("L", "M", "N", "P", "S", "Z"),
            include_characters='\n\t\r"\\',
        ),
        max_size=200,
    )
)
@settings(max_examples=_DOC_EXAMPLES)
def prop_unicode_roundtrip(text):
    """ANY unicode string survives json_dumps -> json_loads roundtrip."""
    loaded = json_loads_native(json_dumps_native(text))
    assert loaded == text, f"Unicode roundtrip failed: {text!r} -> {loaded!r}"


# ---------------------------------------------------------------------------
# Property 4: Nested structures
# ---------------------------------------------------------------------------


@given(depth=st.integers(min_value=1, max_value=30))
@settings(max_examples=_NESTING_EXAMPLES)
def prop_deep_dict_nesting(depth):
    """Deeply nested dicts roundtrip correctly."""
    obj = "leaf"
    for _ in range(depth):
        obj = {"a": obj}
    loaded = json_loads_native(json_dumps_native(obj))
    assert loaded == obj, f"Deep dict nesting (depth={depth}) failed"


@given(depth=st.integers(min_value=1, max_value=30))
@settings(max_examples=_NESTING_EXAMPLES)
def prop_deep_list_nesting(depth):
    """Deeply nested lists roundtrip correctly."""
    obj = 42
    for _ in range(depth):
        obj = [obj]
    loaded = json_loads_native(json_dumps_native(obj))
    assert loaded == obj, f"Deep list nesting (depth={depth}) failed"


# ---------------------------------------------------------------------------
# Property 5: Zig json_dumps output is stdlib-parseable
# ---------------------------------------------------------------------------


@given(obj=json_values)
@settings(max_examples=_DOC_EXAMPLES)
def prop_zig_dumps_matches_stdlib(obj):
    """Zig json_dumps output can be parsed by stdlib json.loads."""
    zig_bytes = json_dumps_native(obj)
    stdlib_parsed = json.loads(zig_bytes)
    if isinstance(obj, float):
        assert abs(stdlib_parsed - obj) < _FLOAT_TOL
    else:
        assert stdlib_parsed == obj, (
            f"Zig dumps output not stdlib-compatible:\n"
            f"  original: {obj!r}\n"
            f"  zig bytes: {zig_bytes!r}\n"
            f"  stdlib parsed: {stdlib_parsed!r}"
        )


# ---------------------------------------------------------------------------
# Deterministic corpora — the blind spots the strategies never reach. The props
# above compare floats with a 1e-10 tolerance and cap ints at ±2^53, so a
# not-correctly-rounded float parser or a truncating big-int path would slip
# through. Here comparison is BYTE-EXACT (repr level). These pinned inputs run
# unconditionally as their own regression corpus.
# ---------------------------------------------------------------------------

_BIGINT_CASES = (
    "9223372036854775807",  # i64 max (SIMD path)
    "9223372036854775808",  # i64 max + 1 -> fallback
    "-9223372036854775808",  # i64 min (sign at boundary)
    "-9223372036854775809",  # i64 min - 1 -> fallback
    "18446744073709551615",  # u64 max
    "18446744073709551616",  # 2^64
    "123456789012345678901234567890",  # 30 digits
    "-" + "9" * 40,  # 40-nine negative
    str(2**53 + 1),  # 2^53+1 (would lose precision as float)
)

_FLOAT_CASES = (
    "0.1",
    "0.2",
    "0.3",
    "0.30000000000000004",
    "1.7976931348623157e308",  # max finite double
    "5e-324",  # min positive subnormal
    "2.2250738585072014e-308",  # min normal
    "2.2250738585072011e-308",  # historical round-to-nearest DoS value
    "1.0000000000000002",  # smallest double > 1
    "9007199254740993.0",  # 2^53+1 as a float literal (must round to 2^53)
    "3.141592653589793238462643383279",  # over-long mantissa
    "1e-1",
    "1E10",
    "-0.0",
    "123456789.123456789",
)

_ESCAPE_CASES = (
    r'"é"',  # é (BMP)
    r'"café"',
    r'"AB"',
    r'"😀"',  # surrogate pair
    r'"𝄞"',  # musical symbol
    r'"tab\there\nnewline"',
    r'"quote\"backslash\\slash\/"',
    '"\\u00e9"',  # é via BMP \u escape
    '"\\u0041\\u0042"',  # AB via \u escapes
    '"\\ud83d\\ude00"',  # emoji via surrogate-pair escape
    '"\\ud834\\udd1e"',  # musical symbol via surrogate-pair escape
    '"\\u0000embedded"',  # NUL via \u escape
    r'"nul"',
    r'"mixed é 😀 text"',
)


def _empty_structures() -> tuple[bool, str]:
    for obj in ({}, [], ""):
        loaded = json_loads_native(json_dumps_native(obj))
        if loaded != obj:
            return False, f"empty {type(obj).__name__} roundtrip -> {loaded!r}"
    return True, ""


def _special_int_values() -> tuple[bool, str]:
    for val in (0, -0, 1, -1, 2**31 - 1, -(2**31)):
        loaded = json_loads_native(json_dumps_native(val))
        if loaded != val:
            return False, f"int {val} roundtrip -> {loaded!r}"
    return True, ""


def _bigint_exact_vs_stdlib() -> tuple[bool, str]:
    """Integers across and beyond the i64 boundary must decode EXACTLY as
    arbitrary-precision ints — not truncate, not become floats."""
    for s in _BIGINT_CASES:
        native = json_loads_native(s)
        std = json.loads(s)
        if not (native == std and type(native) is type(std)):
            return False, (
                f"bigint {s}: native {native!r} ({type(native).__name__}) != "
                f"stdlib {std!r} ({type(std).__name__})"
            )
    return True, ""


def _float_exact_roundtrip_vs_stdlib() -> tuple[bool, str]:
    """Tricky floats must decode to the SAME double as stdlib (correctly
    rounded). Compared via repr — exact, no tolerance."""
    for s in _FLOAT_CASES:
        native = json_loads_native(s)
        std = json.loads(s)
        if not isinstance(native, float):
            return False, f"{s}: native not float ({native!r})"
        if repr(native) != repr(std):
            return False, f"float {s}: native repr {native!r} != stdlib repr {std!r}"
    return True, ""


def _unicode_escape_vs_stdlib() -> tuple[bool, str]:
    """Raw \\u escapes (incl. valid surrogate pairs) must decode identically to
    stdlib on the well-defined cases."""
    for s in _ESCAPE_CASES:
        native = json_loads_native(s)
        std = json.loads(s)
        if native != std:
            return False, f"escape {s!r}: native {native!r} != stdlib {std!r}"
    return True, ""


def _overflow_float_matches_stdlib() -> tuple[bool, str]:
    """A float literal that overflows to ±inf: match stdlib (which yields inf),
    OR reject cleanly — never a wrong finite value or a crash."""
    for s, want in (("1e400", float("inf")), ("-1e400", float("-inf"))):
        try:
            native = json_loads_native(s)
        except ValueError:
            continue  # clean rejection is acceptable
        if native != want:
            return False, f"overflow {s}: native {native!r} != {want!r}"
    return True, ""


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

_PROPERTIES = (
    prop_zig_roundtrip,
    prop_zig_matches_stdlib_loads,
    prop_unicode_roundtrip,
    prop_deep_dict_nesting,
    prop_deep_list_nesting,
    prop_zig_dumps_matches_stdlib,
)

_CORPORA = (
    ("empty structures", _empty_structures),
    ("special int values", _special_int_values),
    ("bigint exact vs stdlib", _bigint_exact_vs_stdlib),
    ("float exact roundtrip vs stdlib", _float_exact_roundtrip_vs_stdlib),
    ("unicode escape vs stdlib", _unicode_escape_vs_stdlib),
    ("overflow float matches stdlib", _overflow_float_matches_stdlib),
)


def run_tests() -> bool:
    print("\n-- JSON Parser Hypothesis Property Tests --\n")
    for prop in _PROPERTIES:
        run_property(prop)
    for name, corpus in _CORPORA:
        ok, detail = corpus()
        check(name, ok, detail)
    return finish()


if __name__ == "__main__":
    run_main(run_tests)
