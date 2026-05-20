"""
Microbench: from_record setattr-loop vs __dict__.update bulk assignment.

Current from_record does N setattrs for bookkeeping + fields.
Proposal: use instance.__dict__.update(record_subset) — a single C-level
dict merge — for the plain field loop. Enum fields still use setattr for
the coercion.

Proves: Post instances have __dict__ (not slots), and __dict__.update
produces correct attribute access.

Run: uv run python scripts/bench_from_record_bulk.py
"""

import os
import statistics
import sys
import time

os.environ.setdefault("DATABASE_URL", "postgres://localhost/hyperdjango_test")
os.environ.setdefault("HYPER_LOAD_TEST", "1")

# Import hypernews models so we have a realistic ORM class
from services.hypernews.app import Post


def make_record() -> dict:
    from datetime import UTC, datetime

    return {
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "id": 1,
        "title": "Test Post",
        "slug": "test-post",
        "url": "https://example.com/",
        "text": "Body text here",
        "author_id": 1,
        "forum_id": 1,
        "score": 10,
        "weighted_score": 1.5,
        "upvotes": 5,
        "downvotes": 0,
        "hot_score": 0.5,
        "controversy": 0.1,
        "velocity": 2.0,
        "comment_count": 3,
        "status": "published",
        "crosspost_source_id": 0,
        "is_pinned": False,
        "pinned_by": 0,
        "agree_count": 0,
        "disagree_count": 0,
        "is_ask": False,
        "is_show": False,
        "is_deleted": False,
    }


def from_record_current(cls, record):
    """Current implementation — setattr loop."""
    instance = object.__new__(cls)
    _setattr = object.__setattr__
    _setattr(instance, "__pydantic_fields_set__", set(cls.__dhi_field_names__))
    _setattr(instance, "__pydantic_private__", None)
    _setattr(instance, "__pydantic_extra__", None)
    _setattr(instance, "_loaded_from_db", True)
    for field_name in cls.__dhi_plain_field_names__:
        _setattr(instance, field_name, record[field_name])
    for field_name, enum_cls in cls.__dhi_enum_coercer_items__:
        value = record[field_name]
        if value is not None and not isinstance(value, enum_cls):
            value = enum_cls(value)
        _setattr(instance, field_name, value)
    return instance


def from_record_bulk(cls, record):
    """Proposed: __dict__.update for bulk field assignment."""
    instance = object.__new__(cls)
    d = instance.__dict__
    d["__pydantic_fields_set__"] = set(cls.__dhi_field_names__)
    d["__pydantic_private__"] = None
    d["__pydantic_extra__"] = None
    d["_loaded_from_db"] = True
    # Bulk assign plain fields — single C-level dict merge
    # Only copy keys that are actual plain fields (filter out annotations/extras)
    plain = cls.__dhi_plain_field_names__
    for k in plain:
        d[k] = record[k]
    # Enum coercion
    for field_name, enum_cls in cls.__dhi_enum_coercer_items__:
        value = record[field_name]
        if value is not None and not isinstance(value, enum_cls):
            value = enum_cls(value)
        d[field_name] = value
    return instance


def from_record_dict_update(cls, record):
    """Alternative: filter record to plain fields via dict comprehension +
    dict.update. Advantage: __dict__.update is faster than a Python loop."""
    instance = object.__new__(cls)
    d = instance.__dict__
    # Pre-seed bookkeeping
    d["__pydantic_fields_set__"] = set(cls.__dhi_field_names__)
    d["__pydantic_private__"] = None
    d["__pydantic_extra__"] = None
    d["_loaded_from_db"] = True
    # Build a filtered dict then update — one C-level merge.
    # This has tuple/list cost for the comprehension but the update is
    # fastest when the input dict is small.
    plain_names = cls.__dhi_plain_field_names__
    d.update({k: record[k] for k in plain_names})
    # Enum
    for field_name, enum_cls in cls.__dhi_enum_coercer_items__:
        value = record[field_name]
        if value is not None and not isinstance(value, enum_cls):
            value = enum_cls(value)
        d[field_name] = value
    return instance


def from_record_assume_record_is_clean(cls, record):
    """Fastest possible: if record is known to have EXACTLY the plain fields
    (no extras, no missing, no enums), just copy it wholesale."""
    instance = object.__new__(cls)
    d = instance.__dict__
    d.update(record)  # single C-level copy of the whole record dict
    d["__pydantic_fields_set__"] = set(cls.__dhi_field_names__)
    d["__pydantic_private__"] = None
    d["__pydantic_extra__"] = None
    d["_loaded_from_db"] = True
    # Enum coercion (reassigns over the copy)
    for field_name, enum_cls in cls.__dhi_enum_coercer_items__:
        value = d[field_name]
        if value is not None and not isinstance(value, enum_cls):
            d[field_name] = enum_cls(value)
    return instance


def bench(fn, cls, record, iterations: int) -> float:
    start = time.perf_counter_ns()
    for _ in range(iterations):
        fn(cls, record)
    end = time.perf_counter_ns()
    return (end - start) / iterations


def run(label, fn, cls, record, iterations, runs):
    results: list[float] = []
    for _ in range(runs):
        results.append(bench(fn, cls, record, iterations))
    results.sort()
    median = statistics.median(results)
    jitter = (results[-1] - results[0]) / median * 100 / 2
    print(
        f"  {label:<35} median={median:>7.1f} ns/op  "
        f"per-run={[f'{r:.0f}' for r in results]}  "
        f"jitter=±{jitter:.1f}%"
    )
    return median


def verify_correctness(cls, record):
    """All 3 variants must produce instances with identical observable state."""
    a = from_record_current(cls, record)
    b = from_record_bulk(cls, record)
    c = from_record_dict_update(cls, record)
    d = from_record_assume_record_is_clean(cls, record)

    for name in cls.__dhi_field_names__:
        va = getattr(a, name)
        vb = getattr(b, name)
        vc = getattr(c, name)
        vd = getattr(d, name)
        if not (va == vb == vc == vd):
            print(
                f"  FAIL: {name}: current={va!r} bulk={vb!r} update={vc!r} assume={vd!r}"
            )
            sys.exit(1)
    # _loaded_from_db
    for x, lbl in [(a, "current"), (b, "bulk"), (c, "update"), (d, "assume")]:
        if x._loaded_from_db is not True:
            print(f"  FAIL: {lbl}._loaded_from_db is {x._loaded_from_db!r}")
            sys.exit(1)
    print("  correctness: all 4 variants produce identical instances ✓")


def main():
    record = make_record()
    print(f"\nPost model fields: {len(Post.__dhi_field_names__)}")
    print(f"Plain fields: {len(Post.__dhi_plain_field_names__)}")
    print(f"Enum coercers: {len(Post.__dhi_enum_coercer_items__)}")

    # Proof: instances have __dict__
    inst = from_record_current(Post, record)
    assert hasattr(inst, "__dict__"), "BaseModel instance must have __dict__"
    print(f"  instance.__dict__ type: {type(inst.__dict__).__name__}")
    print(f"  instance.__dict__ size: {len(inst.__dict__)} keys")

    verify_correctness(Post, record)

    ITERATIONS = 500_000
    RUNS = 5
    print(f"\nBench: {ITERATIONS:,} iters × {RUNS} runs per variant\n")

    run("current (setattr loop)", from_record_current, Post, record, ITERATIONS, RUNS)
    run("bulk (dict[k]=)", from_record_bulk, Post, record, ITERATIONS, RUNS)
    run(
        "update (compr + update)",
        from_record_dict_update,
        Post,
        record,
        ITERATIONS,
        RUNS,
    )
    run(
        "assume (wholesale copy)",
        from_record_assume_record_is_clean,
        Post,
        record,
        ITERATIONS,
        RUNS,
    )


if __name__ == "__main__":
    main()
