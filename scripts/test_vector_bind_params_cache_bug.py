"""Confirm: vector distance lookups' as_sql and bind_params agree.

as_sql (cache-MISS / first call) emits N placeholders and N params with the
vector FORMATTED as a pgvector literal string. bind_params (cache-HIT / second
call, via QuerySet._collect_where_params -> resolve_bind_params) must return the
same params, or the cached SQL template's placeholders and the hit-path binds
disagree. Reproduces at unit level, no DB needed.
"""

# hyper-test: unit

import re

from hyperdjango.lookups import (
    CosineDistanceLookup,
    InnerProductLookup,
    L2DistanceLookup,
    NearestLookup,
    resolve_bind_params,
)
from hyperdjango.testkit import check, finish, run_main


def count_placeholders(sql: str) -> int:
    return len(set(re.findall(r"\$(\d+)", sql)))


def check_lookup(name, lk, value) -> bool:
    sql, as_sql_params = lk.as_sql("emb", 1, value)
    n_ph = count_placeholders(sql)
    # This is what the cache-HIT path collects (via resolve_bind_params):
    hit_params = resolve_bind_params(f"emb__{name}", value)
    print(f"--- {name} ---")
    print(f"  as_sql SQL          : {sql}")
    print(f"  as_sql params (miss): {as_sql_params!r}  (count={len(as_sql_params)})")
    print(f"  hit-path params     : {hit_params!r}  (count={len(hit_params)})")
    print(f"  placeholders in SQL : {n_ph}")
    ok = len(hit_params) == n_ph and hit_params == as_sql_params
    return check(
        f"{name} as_sql/bind_params consistent",
        ok,
        f"placeholders={n_ph} as_sql={as_sql_params!r} hit={hit_params!r}",
    )


def main() -> bool:
    check_lookup("l2_distance", L2DistanceLookup(), ([0.1, 0.2, 0.3], 1.5))
    check_lookup("cosine_distance", CosineDistanceLookup(), ([0.1, 0.2], 0.2))
    check_lookup("inner_product", InnerProductLookup(), ([0.1, 0.2], -0.8))
    check_lookup("nearest", NearestLookup(), ([0.1, 0.2], "cosine"))
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
