"""Regression (round 10): ORM query-compiler vector-lookup + aggregate bugs.

No DB required — pure-Python checks against the installed extension.

Covers:
  1. The 4 pgvector lookups (l2_distance / cosine_distance / inner_product /
     nearest) produce a matching SQL-placeholder-count and correctly-FORMATTED
     params on BOTH the cache-MISS (as_sql) and cache-HIT (bind_params) paths.
  2. `key_is_value_dependent` marks vector-lookup keys so filter()/exclude()
     force the compiled-SQL slow path — two `nearest` calls with DIFFERENT
     metrics must NOT reuse each other's SQL (l2 vs cosine operator).
  3. Aggregate over a forbidden column string (or forbidden filter_expr key) is
     rejected instead of interpolated raw.
"""

# hyper-test: unit

import re
import traceback
from collections.abc import Callable

from hyperdjango.expressions import Count, Sum
from hyperdjango.lookups import (
    CosineDistanceLookup,
    InnerProductLookup,
    L2DistanceLookup,
    NearestLookup,
    key_is_value_dependent,
    resolve_bind_params,
)
from hyperdjango.testkit import check, finish, run_main


def _placeholder_count(sql: str) -> int:
    return len(set(re.findall(r"\$(\d+)", sql)))


def _fail(msg: str) -> None:
    raise AssertionError(msg)


# --- 1. as_sql (miss) and bind_params (hit) agree in count + format ----------

VECTOR_CASES = [
    ("l2_distance", L2DistanceLookup(), ([0.1, 0.2, 0.3], 1.5)),
    ("cosine_distance", CosineDistanceLookup(), ([0.1, 0.2], 0.2)),
    ("inner_product", InnerProductLookup(), ([0.1, 0.2], -0.8)),
    ("nearest", NearestLookup(), ([0.1, 0.2], "cosine")),
]


def test_vector_as_sql_bind_params_consistency() -> None:
    for name, lk, value in VECTOR_CASES:
        sql, miss_params = lk.as_sql("emb", 1, value)
        n_ph = _placeholder_count(sql)
        hit_params = lk.bind_params(value)
        resolved = resolve_bind_params(f"emb__{name}", value)

        if len(miss_params) != n_ph:
            _fail(
                f"{name}: as_sql produced {len(miss_params)} params for {n_ph} placeholders"
            )
        if hit_params != miss_params:
            _fail(
                f"{name}: bind_params {hit_params!r} != as_sql params {miss_params!r}"
            )
        if resolved != miss_params:
            _fail(
                f"{name}: resolve_bind_params {resolved!r} != as_sql params {miss_params!r}"
            )
        # Vector must be a formatted pgvector literal string, not the raw list/tuple.
        if not isinstance(hit_params[0], str) or not hit_params[0].startswith("["):
            _fail(
                f"{name}: vector not formatted as pgvector literal: {hit_params[0]!r}"
            )


# --- 2. vector keys force the slow path; nearest metrics don't collide -------


def test_vector_keys_are_value_dependent() -> None:
    for suffix in ("l2_distance", "cosine_distance", "inner_product", "nearest"):
        if not key_is_value_dependent(f"emb__{suffix}"):
            _fail(f"key_is_value_dependent missed vector suffix {suffix!r}")

    # Non-vector lookups must remain cacheable (fast path).
    for suffix in ("exact", "gt", "in", "icontains", "isnull"):
        if key_is_value_dependent(f"col__{suffix}"):
            _fail(f"key_is_value_dependent falsely flagged {suffix!r}")
    # A bare field (no lookup suffix) is exact-cacheable.
    if key_is_value_dependent("plain_field"):
        _fail("key_is_value_dependent falsely flagged a bare field")


def test_nearest_metric_distinct_sql() -> None:
    # nearest metric determines the emitted operator — different metrics => different SQL.
    nl = NearestLookup()
    sql_l2, _ = nl.as_sql("emb", 1, ([0.1, 0.2], "l2"))
    sql_cos, _ = nl.as_sql("emb", 1, ([0.1, 0.2], "cosine"))
    if sql_l2 == sql_cos:
        _fail("nearest l2 and cosine produced identical SQL (operator collision)")
    if "<->" not in sql_l2 or "<=>" not in sql_cos:
        _fail(f"nearest operators wrong: l2={sql_l2!r} cosine={sql_cos!r}")


# --- 3. Aggregate rejects forbidden column / filter_expr key -----------------

FORBIDDEN_COLS = [
    "price); DROP TABLE users;--",
    "price FROM users",  # whitespace
    "(SELECT secret)",  # parens
    "price'",  # quote
]


def test_aggregate_rejects_forbidden_column() -> None:
    # A legitimate plain column and dotted FK path must still compile.
    Sum("price").as_sql()
    Sum("author.price").as_sql()
    Count("*").as_sql()

    for bad in FORBIDDEN_COLS:
        try:
            Sum(bad).as_sql()
        except ValueError:
            pass
        else:
            _fail(f"Aggregate accepted forbidden column {bad!r}")


def test_aggregate_rejects_forbidden_filter_expr_key() -> None:
    # filter_expr key injection is rejected; a clean key is accepted.
    Count("id", filter_expr={"status": "active"}).as_sql()
    try:
        Count("id", filter_expr={"status = 'x' OR 1=1 --": True}).as_sql()
    except ValueError:
        pass
    else:
        _fail("Aggregate accepted forbidden filter_expr key")


def main() -> bool:
    tests: tuple[Callable[[], None], ...] = (
        test_vector_as_sql_bind_params_consistency,
        test_vector_keys_are_value_dependent,
        test_nearest_metric_distinct_sql,
        test_aggregate_rejects_forbidden_column,
        test_aggregate_rejects_forbidden_filter_expr_key,
    )
    # `_fail` raises — the file aborts on the first break, as it always has;
    # the counts are emitted before bailing out.
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            finish()
            return False
        check(fn.__name__, True)
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
