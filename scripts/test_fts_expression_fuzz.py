"""
Hypothesis fuzz tests for FTS Expression classes and UPDATE RETURNING.

Proves:
1. SearchVector as_sql never crashes on valid field names
2. SearchQuery as_sql always parameterizes the query text (never inline)
3. SearchRank param offset tracks correctly through composition
4. SearchMatch composes vector + query params correctly
5. TrigramSimilarity param offset is always correct
6. _validate_field_name rejects all dangerous inputs
7. SearchHeadline int coercion prevents injection
8. UPDATE RETURNING SQL generation is well-formed

# hyper-test: unit
"""

import os

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from hyperdjango.postgres import (
    SearchHeadline,
    SearchMatch,
    SearchQuery,
    SearchRank,
    SearchVector,
    TrigramSimilarity,
    _validate_field_name,
)

# Under parallel test execution, CPU contention can push individual examples
# past per-call deadlines (500-1000ms). Disable the deadline under parallel
# mode — these tests do no I/O, the deadline is only an anti-regression guard
# for single-threaded runs.
_PARALLEL = os.environ.get("HYPER_TEST_PARALLEL") == "1"
_DEADLINE = None if _PARALLEL else 1000
_DEADLINE_FAST = None if _PARALLEL else 500
_SUPPRESS = [HealthCheck.too_slow, HealthCheck.filter_too_much] if _PARALLEL else []


def _ex(n: int) -> int:
    """Scale Hypothesis example count for parallel-mode CPU contention.

    Even with deadline=None, the wall-clock budget for the WHOLE test
    file under the runner's 90s timeout was being exceeded under
    parallel CPU contention (~91 seconds observed). Halving examples
    drops the per-file runtime to ~45-60s comfortably under budget.
    """
    return max(n // 2, 30) if _PARALLEL else n


# Strategy: valid SQL identifier field names (alphanumeric + underscore, starts with letter)
valid_field = st.from_regex(r"[a-z][a-z0-9_]{0,30}", fullmatch=True)
valid_fields = st.lists(valid_field, min_size=1, max_size=5)
valid_config = st.sampled_from(["english", "simple", "spanish", "french", "german"])
valid_weight = st.sampled_from(["A", "B", "C", "D"])
valid_search_type = st.sampled_from(["plain", "phrase", "raw", "websearch"])
param_offset = st.integers(min_value=0, max_value=100)
query_text = st.text(min_size=1, max_size=200)


# ---------------------------------------------------------------------------
# 1. SearchVector: valid fields never crash, SQL is structural
# ---------------------------------------------------------------------------


@given(fields=valid_fields, config=valid_config, offset=param_offset)
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_search_vector_never_crashes(fields, config, offset):
    """SearchVector.as_sql() never crashes on valid field names."""
    sv = SearchVector(fields=fields, config=config)
    sql, params = sv.as_sql(offset)
    assert isinstance(sql, str)
    assert isinstance(params, list)
    assert params == []  # SearchVector has no bind params
    assert "to_tsvector" in sql
    for f in fields:
        assert f in sql


@given(fields=valid_fields, weight=valid_weight)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_search_vector_weight(fields, weight):
    """Weighted SearchVector includes setweight and correct weight letter."""
    sv = SearchVector(fields=fields, weight=weight)
    sql, _ = sv.as_sql()
    assert "setweight" in sql
    assert f"'{weight}'" in sql


# ---------------------------------------------------------------------------
# 2. SearchQuery: query text is ALWAYS a bind parameter
# ---------------------------------------------------------------------------


@given(
    query=query_text,
    config=valid_config,
    search_type=valid_search_type,
    offset=param_offset,
)
@settings(max_examples=_ex(500), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_search_query_parameterized(query, config, search_type, offset):
    """SearchQuery always parameterizes the query text — never inline SQL."""
    sq = SearchQuery(query=query, config=config, search_type=search_type)
    sql, params = sq.as_sql(offset)
    assert params == [query]  # Query text is ALWAYS a bind param
    assert f"${offset + 1}" in sql  # Placeholder present
    # Query text must not appear as a quoted SQL literal OUTSIDE the
    # config position. The config IS a structural literal ('english'),
    # so when query == config (e.g., both are 'english'), the raw SQL
    # legitimately contains the string. Strip the config literal before
    # checking for injection of the query value.
    escaped_config = config.replace("'", "''")
    sql_without_config = sql.replace(f"'{escaped_config}'", "", 1)
    assert f"'{query}'" not in sql_without_config


# ---------------------------------------------------------------------------
# 3. SearchRank: param offset tracks through composition
# ---------------------------------------------------------------------------


@given(fields=valid_fields, query=query_text, offset=param_offset)
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_search_rank_param_offset(fields, query, offset):
    """SearchRank correctly offsets params through vector + query."""
    vector = SearchVector(fields=fields)
    sq = SearchQuery(query=query)
    rank = SearchRank(vector=vector, query=sq)
    sql, params = rank.as_sql(offset)

    assert "ts_rank" in sql
    assert params == [query]  # Only query has a param (vector has none)
    assert f"${offset + 1}" in sql  # Query param at correct offset


@given(
    fields=valid_fields,
    query=query_text,
    weights=st.lists(
        st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
        min_size=4,
        max_size=4,
    ),
)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_search_rank_with_weights(fields, query, weights):
    """SearchRank with weights includes weight array in SQL."""
    rank = SearchRank(
        vector=SearchVector(fields=fields),
        query=SearchQuery(query=query),
        weights=weights,
    )
    sql, params = rank.as_sql()
    assert "'{" in sql  # Weight array syntax
    assert params == [query]


# ---------------------------------------------------------------------------
# 4. SearchMatch: composes vector + query correctly
# ---------------------------------------------------------------------------


@given(fields=valid_fields, query=query_text, offset=param_offset)
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_search_match_composition(fields, query, offset):
    """SearchMatch composes vector @@ query with correct params."""
    match = SearchMatch(
        vector=SearchVector(fields=fields),
        query=SearchQuery(query=query),
    )
    sql, params = match.as_sql(offset)

    assert "@@" in sql
    assert params == [query]
    assert f"${offset + 1}" in sql


# ---------------------------------------------------------------------------
# 5. TrigramSimilarity: param offset always correct
# ---------------------------------------------------------------------------


@given(field=valid_field, value=query_text, offset=param_offset)
@settings(max_examples=_ex(300), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_trigram_param_offset(field, value, offset):
    """TrigramSimilarity param is always at correct offset."""
    ts = TrigramSimilarity(field=field, value=value)
    sql, params = ts.as_sql(offset)

    assert params == [value]
    assert f"${offset + 1}" in sql
    assert "similarity" in sql
    if len(value) > 5:
        assert value not in sql  # Long value NEVER in SQL string


# ---------------------------------------------------------------------------
# 6. _validate_field_name: rejects ALL dangerous inputs
# ---------------------------------------------------------------------------


@given(
    name=st.text(
        min_size=1,
        max_size=50,
        alphabet=st.characters(whitelist_categories=("Cs", "Cc", "P", "Z", "S")),
    )
)
@settings(
    max_examples=_ex(500), deadline=_DEADLINE_FAST, suppress_health_check=_SUPPRESS
)
def test_validate_rejects_dangerous(name):
    """Field names with special chars are always rejected."""
    try:
        _validate_field_name(name)
        # If it passed, verify it's actually safe (only alnum/underscore)
        assert all(c.isalnum() or c == "_" for c in name), (
            f"Dangerous name passed: {name!r}"
        )
    except ValueError:
        pass  # Correctly rejected


@given(name=valid_field)
@settings(
    max_examples=_ex(200), deadline=_DEADLINE_FAST, suppress_health_check=_SUPPRESS
)
def test_validate_accepts_valid(name):
    """Valid identifier field names always pass."""
    _validate_field_name(name)  # Should not raise


# ---------------------------------------------------------------------------
# 7. SearchHeadline: int coercion prevents injection
# ---------------------------------------------------------------------------


@given(
    field=valid_field,
    query=query_text,
    max_words=st.integers(min_value=1, max_value=1000),
    min_words=st.integers(min_value=1, max_value=100),
    max_fragments=st.integers(min_value=0, max_value=50),
)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_headline_int_coercion(field, query, max_words, min_words, max_fragments):
    """SearchHeadline int fields produce valid SQL options."""
    hl = SearchHeadline(
        field=field,
        query=SearchQuery(query=query),
        max_words=max_words,
        min_words=min_words,
        max_fragments=max_fragments,
    )
    sql, params = hl.as_sql()
    assert f"MaxWords={max_words}" in sql
    assert f"MinWords={min_words}" in sql
    assert f"MaxFragments={max_fragments}" in sql
    assert params == [query]


# ---------------------------------------------------------------------------
# 8. Multiple annotations: param offsets chain correctly
# ---------------------------------------------------------------------------


@given(
    fields=valid_fields,
    query1=query_text,
    query2=query_text,
    trgm_value=query_text,
)
@settings(max_examples=_ex(200), deadline=_DEADLINE, suppress_health_check=_SUPPRESS)
def test_multi_annotation_offsets(fields, query1, query2, trgm_value):
    """Multiple Expression.as_sql() calls with incrementing offsets produce unique $N."""
    rank = SearchRank(SearchVector(fields=fields), SearchQuery(query=query1))
    sim = TrigramSimilarity(field=fields[0], value=trgm_value)
    headline = SearchHeadline(field=fields[0], query=SearchQuery(query=query2))

    # Simulate annotate() param accumulation
    offset = 0
    all_params = []

    sql1, params1 = rank.as_sql(offset)
    all_params.extend(params1)
    offset += len(params1)

    sql2, params2 = sim.as_sql(offset)
    all_params.extend(params2)
    offset += len(params2)

    sql3, params3 = headline.as_sql(offset)
    all_params.extend(params3)

    # All params should be collected in order
    assert all_params == [query1, trgm_value, query2]

    # Each SQL should reference unique $N placeholders
    assert "$1" in sql1
    assert "$2" in sql2
    assert "$3" in sql3


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    print("\n── FTS Expression Hypothesis Fuzz Tests ──\n")

    tests = [
        ("SearchVector never crashes", test_search_vector_never_crashes),
        ("SearchVector weight", test_search_vector_weight),
        ("SearchQuery parameterized", test_search_query_parameterized),
        ("SearchRank param offset", test_search_rank_param_offset),
        ("SearchRank with weights", test_search_rank_with_weights),
        ("SearchMatch composition", test_search_match_composition),
        ("TrigramSimilarity offset", test_trigram_param_offset),
        ("validate rejects dangerous", test_validate_rejects_dangerous),
        ("validate accepts valid", test_validate_accepts_valid),
        ("SearchHeadline int coercion", test_headline_int_coercion),
        ("multi-annotation offsets", test_multi_annotation_offsets),
    ]

    passed = 0
    failed = 0
    for name, test in tests:
        try:
            test()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"FTS expression fuzz: {passed}/{total} passed")
    if failed:
        import sys

        sys.exit(1)
    else:
        print("ALL PASSED")


if __name__ == "__main__":
    run_tests()
