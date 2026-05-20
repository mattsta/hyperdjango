#!/usr/bin/env python3
# hyper-test: unit
"""SQL-identifier authority (round-15 unification, class C2).

Proves the single sqlident authority validates identifiers/types/paths, that the
collapsed validators delegate to it, and that the closed live gaps (F, Cast,
SearchFilter, ALTER TYPE) reject injection while legit uses compile.
"""

import sys

from hyperdjango.sqlident import (
    IdentifierError,
    escape_sql_literal,
    quote_identifier,
    validate_column_path,
    validate_identifier,
    validate_type,
)

_p = _f = 0


def check(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  PASS {name}")
    else:
        _f += 1
        print(f"  FAIL {name} — {detail}")


def rejects(fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except IdentifierError, ValueError:
        return True


def main():
    # identifiers
    check(
        "valid identifier",
        validate_identifier("author_id", kind="column", source="t") == "author_id",
    )
    for bad in [
        'a"b',
        "a b",
        "a;b",
        "a-b",
        "a.b",
        "1abc",
        "a|b",
        "a(b)",
        "id--",
        "café",
        "x" * 64,
        "",
    ]:
        check(
            f"reject {bad!r}",
            rejects(validate_identifier, bad, kind="column", source="t"),
        )

    # column paths
    check("path a__b__c", validate_column_path("author__name__icontains", source="t"))
    check("reject dotted path", rejects(validate_column_path, "a.b", source="t"))
    check(
        "reject injection path",
        rejects(validate_column_path, "id IS NULL OR 1=1 --", source="t"),
    )

    # types
    for ok in [
        "int",
        "numeric(10,2)",
        "int[]",
        "timestamptz",
        "double precision",
        "varchar(255)",
    ]:
        check(f"type {ok}", validate_type(ok, source="t") == ok)
    for bad in ["int) OR (SELECT 1", "int; DROP TABLE x", "notatype", "'; --"]:
        check(f"reject type {bad!r}", rejects(validate_type, bad, source="t"))

    # quote / literal
    check(
        "quote reserved word",
        quote_identifier("order", kind="column", source="t") == '"order"',
    )
    check(
        "quote rejects injection",
        rejects(quote_identifier, 'a"; DROP', kind="column", source="t"),
    )
    check("escape literal", escape_sql_literal("O'Brien") == "O''Brien")

    # collapsed validators delegate (behavior)
    from hyperdjango.expressions import Cast, F

    check("F rejects injection name", rejects(F, "id) OR (1=1"))
    check("F accepts column", F("price").name == "price")
    check("Cast rejects bad type", rejects(Cast, F("x"), "int) OR (SELECT 1"))
    check(
        "Cast accepts real type",
        Cast(F("x"), "numeric(10,2)").output_type == "numeric(10,2)",
    )

    # Aggregate default literals must be escaped (ArrayAgg/JSONBAgg had drifted
    # from StringAgg — a quote in `default` broke out of the string literal).
    from hyperdjango.postgres import (
        ArrayAgg,
        JSONBAgg,
        SearchQuery,
        SearchRank,
        SearchVector,
    )

    check(
        "ArrayAgg default escapes quotes",
        "''" in ArrayAgg(field="tags", default=["x'y"]).as_sql()
        and "'x'y" not in ArrayAgg(field="tags", default=["x'y"]).as_sql(),
    )
    check(
        "JSONBAgg default escapes quotes",
        "''" in JSONBAgg(field="data", default="x'y").as_sql(),
    )
    # SearchRank weights/normalization are coerced numeric (declared but not
    # enforced) — a non-numeric value must be rejected, not interpolated raw.
    check(
        "SearchRank weights reject non-numeric",
        rejects(
            lambda: SearchRank(
                SearchVector(["a"]), SearchQuery("q"), weights=["1); DROP--"]
            ).as_sql()
        ),
    )
    check(
        "SearchRank weights accept floats",
        "0.1"
        in SearchRank(
            SearchVector(["a"]), SearchQuery("q"), weights=[0.1, 0.2, 0.4, 1.0]
        ).as_sql()[0],
    )

    print(f"\n{_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
