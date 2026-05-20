"""
Micro-benchmark: WhereNode.compile() Zig vs Python.

NOT in CI test suite (manual benchmark).

Run: uv run python scripts/bench_where_compile.py
"""

import time

from hyperdjango._hyperdjango_native import _where_compile

from hyperdjango.where import WhereNode


def _python_compile(node: WhereNode, start_idx: int = 1) -> tuple[str, list, int]:
    """Original pure-Python implementation for comparison."""
    if node.is_empty:
        return "", [], start_idx

    if node.template:
        params = list(node.bind_values)
        segments = node.template.split("{}")
        if len(segments) == 1:
            sql = node.template
            idx = start_idx
        else:
            result_parts = [segments[0]]
            idx = start_idx
            for seg in segments[1:]:
                result_parts.append(f"${idx}")
                result_parts.append(seg)
                idx += 1
            sql = "".join(result_parts)

        if node.negated:
            sql = f"NOT ({sql})"
        return sql, params, idx

    parts = []
    all_params = []
    idx = start_idx
    for child in node.children:
        child_sql, child_params, idx = _python_compile(child, idx)
        if child_sql:
            parts.append(child_sql)
            all_params.extend(child_params)

    if not parts:
        return "", all_params, idx

    if len(parts) == 1:
        joined = parts[0]
    elif node.connector == "OR":
        joined = f"({' OR '.join(parts)})"
    else:
        joined = " AND ".join(parts)

    if node.negated:
        joined = f"NOT ({joined})"

    return joined, all_params, idx


# ─── Benchmark scenarios ─────────────────────────────────────────────────────


def make_simple_leaf():
    return WhereNode(template="name = {}", bind_values=["alice"])


def make_3_filter_and():
    return WhereNode(
        connector="AND",
        children=[
            WhereNode(template='"name" = {}', bind_values=["alice"]),
            WhereNode(template='"age" >= {}', bind_values=[18]),
            WhereNode(template='"status" = {}', bind_values=["active"]),
        ],
    )


def make_complex_nested():
    return WhereNode(
        connector="AND",
        children=[
            WhereNode(template='"is_deleted" = FALSE'),
            WhereNode(template='"tenant_id" = {}', bind_values=[42]),
            WhereNode(
                connector="OR",
                children=[
                    WhereNode(template='"role" = {}', bind_values=["admin"]),
                    WhereNode(template='"role" = {}', bind_values=["staff"]),
                    WhereNode(template='"role" = {}', bind_values=["mod"]),
                ],
            ),
            WhereNode(template='"created_at" >= {}', bind_values=["2024-01-01"]),
        ],
    )


def time_compile(
    compile_fn, node, iterations: int, runs: int
) -> tuple[float, list[float]]:
    """Measure ns/op. Returns (median_ns_per_op, all_per_op_measurements).

    Runs `runs` independent loops of `iterations` each. Reports the median
    per-op time across runs so single-run CPU jitter is visible and filtered.
    A 100K-iter single-run measurement at ~1-2μs/op completes in 100-200ms,
    which is too short for stable numbers — variance of ±30% is common.
    """
    # Warmup — prime the CPU caches + interp inline caches
    for _ in range(5000):
        compile_fn(node, 1)

    per_op_times: list[float] = []
    for _ in range(runs):
        start = time.perf_counter_ns()
        for _ in range(iterations):
            compile_fn(node, 1)
        elapsed = time.perf_counter_ns() - start
        per_op_times.append(elapsed / iterations)

    per_op_sorted = sorted(per_op_times)
    median = per_op_sorted[len(per_op_sorted) // 2]
    return median, per_op_times


def main():
    print("=" * 70)
    print("  WhereNode.compile() Benchmark — Zig vs Python")
    print("=" * 70)

    iterations = 500_000  # ~0.5-1 second per run at ~1-2μs/op
    runs = 5  # median across 5 runs

    scenarios = [
        ("Simple leaf (1 placeholder)", make_simple_leaf()),
        ("3-filter AND", make_3_filter_and()),
        ("Complex nested (4 children + OR sub-branch)", make_complex_nested()),
    ]

    total_ops = iterations * runs
    print(f"\n  Iterations per run: {iterations:,}")
    print(f"  Runs per measurement: {runs} (median reported)")
    print(f"  Total operations per scenario: {total_ops:,}")
    print(f"\n  {'Scenario':<45} {'Python (ns)':>12} {'Zig (ns)':>12} {'Speedup':>10}")
    print("  " + "-" * 81)

    for name, node in scenarios:
        py_ns, py_runs = time_compile(_python_compile, node, iterations, runs)
        zig_ns, zig_runs = time_compile(_where_compile, node, iterations, runs)
        speedup = py_ns / zig_ns if zig_ns > 0 else 0
        # Show run-to-run jitter
        py_min, py_max = min(py_runs), max(py_runs)
        zig_min, zig_max = min(zig_runs), max(zig_runs)
        py_jit = ((py_max - py_min) / py_ns * 100 / 2) if py_ns else 0
        zig_jit = ((zig_max - zig_min) / zig_ns * 100 / 2) if zig_ns else 0
        print(f"  {name:<45} {py_ns:>11,.0f}  {zig_ns:>11,.0f}  {speedup:>9.2f}x")
        print(f"  {'  jitter:':<45} ±{py_jit:>9.1f}%  ±{zig_jit:>9.1f}%")

    print("\n" + "=" * 70)
    print("  Benchmark complete")
    print("=" * 70)


if __name__ == "__main__":
    main()
