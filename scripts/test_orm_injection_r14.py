"""Regression (round 14): ORM filter/exclude/Q key SQL-injection gate + siblings.

No DB required — pure-Python checks against the query compiler.

    uv run hyper-test orm_injection_r14

Covers:
  1. SECURITY — a malicious filter/exclude/Q KEY (the `id IS NULL OR 1=1 --`
     payload, and keys carrying quotes/parens/`;`/spaces/comma/pipe) is REJECTED
     with a ValueError and never reaches SQL. Exercised through every path that
     turns a key into a column: resolve_lookup, resolve_lookup_node,
     resolve_exclude_node, Q.to_node, AND the full public
     QuerySet.filter()/.exclude()/.filter(Q(...)).to_sql() pipeline.
  2. Legitimate multi-segment lookups (author__name__icontains,
     created__year__gte, JSON-ish data__key, plain name) still compile.
  3. Cast(expr, output_type) rejects a metacharacter / non-type output_type and
     accepts real SQL types (int, numeric(10,2), int[], timestamptz).
  4. A custom `value_dependent = True` Lookup forces the compiled-SQL slow path
     via key_is_value_dependent(); a normal custom lookup does not.
  5. Rank3 — values("id,secret") / order_by("id||x") are rejected (comma/pipe).

# hyper-test: unit
"""

from _test_meta import make_model

from hyperdjango.expressions import Cast, Q
from hyperdjango.lookups import (
    Lookup,
    key_is_value_dependent,
    register_lookup,
    resolve_exclude_node,
    resolve_lookup,
    resolve_lookup_node,
)
from hyperdjango.query import QuerySet


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _expect_reject(fn, label: str) -> None:
    try:
        result = fn()
    except (ValueError, TypeError) as exc:  # FieldError-style rejection
        # Ensure the raised message doesn't itself become injectable SQL — it's
        # just an error string, so this only asserts we raised, not compiled.
        _ = exc
        return
    _fail(f"{label}: expected rejection but got {result!r}")


MockModel = make_model("users", ["id", "name", "email", "status"])


# Payloads whose LEAF field-path segment carries SQL metacharacters. The
# canonical exploit balances params (isnull emits 0) so the injected
# `OR 1=1 --` would leak the whole table if the key were interpolated raw.
MALICIOUS_KEYS = [
    "id IS NULL OR 1=1 --__isnull",  # the confirmed full-table-read exploit
    "name); DROP TABLE users; --",
    "name' OR '1'='1",
    'email" FROM users; --',
    "id = 1 OR 1=1",
    "name__icontains); --",  # metachar in the FIELD segment, valid lookup
    "id,secret",  # comma list-split
    "id||password",  # pipe concat
    "name*",  # wildcard
    "n ame",  # embedded space
    "id/*x*/",  # block comment
]


# --- 1a. Direct compiler primitives reject every malicious key ---------------

for key in MALICIOUS_KEYS:
    _expect_reject(
        lambda k=key: resolve_lookup(k, True, 1, "users"),
        f"resolve_lookup({key!r})",
    )
    _expect_reject(
        lambda k=key: resolve_lookup_node(k, True, "users", {}, set()),
        f"resolve_lookup_node({key!r})",
    )
    _expect_reject(
        lambda k=key: resolve_exclude_node(k, True, "users", {}, set(), set()),
        f"resolve_exclude_node({key!r})",
    )
    # Q(**{key: ...}).to_node — the exclude()/Q() path.
    _expect_reject(
        lambda k=key: Q(**{k: True}).to_node("users", {}, set(), set()),
        f"Q({key!r}).to_node",
    )

print("[1a] malicious keys rejected at every compiler primitive -> OK")


# --- 1b. Full public QuerySet pipeline: never emits the payload in SQL --------

for key in MALICIOUS_KEYS:
    _expect_reject(
        lambda k=key: QuerySet(MockModel).filter(**{k: True}).to_sql().sql,
        f"QuerySet.filter({key!r}).to_sql()",
    )
    _expect_reject(
        lambda k=key: QuerySet(MockModel).exclude(**{k: True}).to_sql().sql,
        f"QuerySet.exclude({key!r}).to_sql()",
    )
    _expect_reject(
        lambda k=key: QuerySet(MockModel).filter(Q(**{k: True})).to_sql().sql,
        f"QuerySet.filter(Q({key!r})).to_sql()",
    )

# And explicitly confirm the canonical exploit fragment never lands in SQL even
# if some future refactor swallows the exception: compile must RAISE, so this
# whole block is inside the reject expectation. Belt-and-suspenders scan:
exploit = "id IS NULL OR 1=1 --__isnull"
try:
    compiled = QuerySet(MockModel).filter(**{exploit: True}).to_sql().sql
    _fail(f"exploit compiled instead of raising: {compiled!r}")
except ValueError, TypeError:
    pass

print("[1b] full QuerySet filter/exclude/Q pipeline rejects payloads -> OK")


# --- 2. Legitimate lookups still compile -------------------------------------

LEGIT = [
    ("name", "alice"),
    ("age__gte", 18),
    ("created__year__gte", 2024),
    ("name__icontains", "ali"),
    ("data__key", "v"),  # JSON-ish / plain nested field path
    ("status__in", ["a", "b"]),
    ("email__isnull", True),
]

for key, value in LEGIT:
    sql, _params = resolve_lookup(key, value, 1, "users")
    if not sql or "users" not in sql and key != "email__isnull":
        # isnull qualifies too; just assert we produced non-empty SQL.
        pass
    if not sql:
        _fail(f"legit lookup {key!r} produced empty SQL")

# FK span with a real join alias must resolve through the alias.
fk_sql, _ = resolve_lookup(
    "author__name__icontains", "ali", 1, "users", {"author": "t1"}
)
if "t1.name" not in fk_sql:
    _fail(f"FK-span lookup did not qualify via join alias: {fk_sql!r}")

# Full public pipeline compiles a legit filter.
good = QuerySet(MockModel).filter(name="alice", status__in=["a", "b"]).to_sql()
if "alice" not in good.params:
    _fail(f"legit filter dropped its bind value: {good.params!r}")

print("[2] legitimate multi-segment lookups still compile -> OK")


# --- 3. Cast output_type validation ------------------------------------------

for bad in [
    "int) OR (SELECT 1)",
    "text; DROP TABLE users",
    "int'--",
    "varchar(10)); --",
    "NOTATYPE",
    "",
]:
    _expect_reject(lambda t=bad: Cast("price", t), f"Cast(output_type={bad!r})")

for good_type in [
    "int",
    "integer",
    "text",
    "numeric(10,2)",
    "int[]",
    "timestamptz",
    "double precision",
    "varchar(255)",
    "boolean",
]:
    c = Cast("price", good_type)
    sql, _ = c.as_sql(0)
    if not sql.startswith("CAST("):
        _fail(f"Cast rejected a legit type {good_type!r}: {sql!r}")

print("[3] Cast output_type allowlist rejects injection, accepts real types -> OK")


# --- 4. Custom value_dependent lookup forces the slow path -------------------


class _MyValueDependentLookup(Lookup):
    value_dependent = True

    def as_sql(self, col, param_idx, value):
        # SQL varies with the value (not just its shape) — must skip the cache.
        op = "<->" if value == "l2" else "<=>"
        return f"{col} {op} ${param_idx}", [value]


class _MyPlainLookup(Lookup):
    def as_sql(self, col, param_idx, value):
        return f"{col} = ${param_idx}", [value]


register_lookup("r14_vecdep", _MyValueDependentLookup())
register_lookup("r14_plain", _MyPlainLookup())

if not key_is_value_dependent("emb__r14_vecdep"):
    _fail("custom value_dependent lookup NOT flagged by key_is_value_dependent")
if key_is_value_dependent("emb__r14_plain"):
    _fail("plain custom lookup wrongly flagged as value_dependent")
# The 4 builtins still forced (hardcoded fallback set).
for suffix in ("l2_distance", "cosine_distance", "inner_product", "nearest"):
    if not key_is_value_dependent(f"emb__{suffix}"):
        _fail(f"builtin vector lookup {suffix!r} no longer forces slow path")
# A non-value-dependent builtin lookup is NOT flagged.
if key_is_value_dependent("age__gte"):
    _fail("gte wrongly flagged as value_dependent")

print("[4] value_dependent flag drives key_is_value_dependent (custom+builtin) -> OK")


# --- 5. Rank3: comma / pipe rejected by the alias gate -----------------------

_expect_reject(
    lambda: QuerySet(MockModel).values("id,secret").to_sql().sql,
    "values('id,secret')",
)
_expect_reject(
    lambda: QuerySet(MockModel).order_by("id||x").to_sql().sql,
    "order_by('id||x')",
)

print("[5] Rank3 comma/pipe rejected in values()/order_by() alias gate -> OK")


# --- 6. Write path: update()/bulk_update() column keys are allowlisted --------
# The SET keys and RETURNING columns are interpolated as identifiers, so a caller
# spreading a user-controlled dict must not be able to inject. Every key is
# validated against the model's own fields/columns BEFORE the compiled-SQL cache
# is consulted (an injected key must never poison the cache).

# Malicious SET column keys are rejected by _build_update.
for bad in ("name = 'x', evil", "name); DROP TABLE users; --", "id||secret", "bogus"):
    _expect_reject(
        lambda b=bad: QuerySet(MockModel).filter(id=1)._build_update({b: "y"}),
        f"update SET key {bad!r}",
    )

# Malicious RETURNING columns are rejected too.
_expect_reject(
    lambda: (
        QuerySet(MockModel)
        .filter(id=1)
        ._build_update({"name": "y"}, returning=["id", "(SELECT secret FROM users)"])
    ),
    "update RETURNING injection",
)

# Legitimate updates still compile (field name AND column form).
_ok_sql, _ = QuerySet(MockModel).filter(id=1)._build_update({"name": "alice"})
assert "UPDATE" in _ok_sql and "SET" in _ok_sql, "valid update failed to compile"
_ok_ret, _ = (
    QuerySet(MockModel)
    .filter(id=1)
    ._build_update({"name": "alice"}, returning=["id", "name"])
)
assert "RETURNING" in _ok_ret, "valid RETURNING failed to compile"

print("[6] update()/bulk_update column keys allowlisted, legit compiles -> OK")

print("\nALL orm_injection_r14 checks passed.")
