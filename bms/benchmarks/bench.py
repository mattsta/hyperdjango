"""
HyperDjango Benchmark Suite

Compares hyperdjango's native/optimized implementations against
stdlib and manual equivalents.

Usage:
    uv run python benchmarks/bench.py
    uv run python benchmarks/bench.py --iterations 500000
"""

import argparse
import json
import re
import sys
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def bench(fn, iterations):
    """Run fn() for `iterations` times, return total nanoseconds."""
    start = time.perf_counter_ns()
    for _ in range(iterations):
        fn()
    return time.perf_counter_ns() - start


def fmt_ops(total_ns, iterations):
    """Format ops/sec as a human-readable string."""
    if total_ns == 0:
        return "inf"
    ops = iterations / (total_ns / 1_000_000_000)
    if ops >= 1_000_000:
        return f"{ops / 1_000_000:.2f}M"
    if ops >= 1_000:
        return f"{ops / 1_000:.1f}k"
    return f"{ops:.0f}"


def fmt_ns(total_ns, iterations):
    """Format average time per operation."""
    avg = total_ns / iterations
    if avg >= 1_000_000:
        return f"{avg / 1_000_000:.2f} ms"
    if avg >= 1_000:
        return f"{avg / 1_000:.1f} us"
    return f"{avg:.0f} ns"


def print_table(rows):
    """Print a formatted results table."""
    headers = ["Benchmark", "Avg time", "Ops/sec", "vs stdlib"]
    col_widths = [
        max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)
    ]

    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    header_line = (
        "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_widths)) + " |"
    )

    print(sep)
    print(header_line)
    print(sep)
    for row in rows:
        print("| " + " | ".join(val.ljust(w) for val, w in zip(row, col_widths)) + " |")
    print(sep)


# ---------------------------------------------------------------------------
# Benchmark: Validation
# ---------------------------------------------------------------------------


def bench_validation(iterations):
    """Compare hyperdjango's BaseModel validation vs manual validation."""
    from hyperdjango.validation.core.fields import Field
    from hyperdjango.validation.core.model import BaseModel

    class UserModel(BaseModel):
        name: str = Field(min_length=1, max_length=100)
        age: int = Field(ge=0, le=150)
        email: str = Field(min_length=5, max_length=255)

    test_data = {"name": "Alice Johnson", "age": 30, "email": "alice@example.com"}

    # hyperdjango BaseModel validation
    def hd_validate():
        UserModel(**test_data)

    # Manual / stdlib validation
    def manual_validate():
        name = test_data["name"]
        age = test_data["age"]
        email = test_data["email"]
        if not isinstance(name, str) or not (1 <= len(name) <= 100):
            raise ValueError("bad name")
        if not isinstance(age, int) or not (0 <= age <= 150):
            raise ValueError("bad age")
        if not isinstance(email, str) or not (5 <= len(email) <= 255):
            raise ValueError("bad email")

    t_hd = bench(hd_validate, iterations)
    t_manual = bench(manual_validate, iterations)

    speedup = t_hd / t_manual if t_manual else float("inf")

    return [
        (
            "validation (hyperdjango)",
            fmt_ns(t_hd, iterations),
            fmt_ops(t_hd, iterations),
            f"{speedup:.2f}x",
        ),
        (
            "validation (manual)",
            fmt_ns(t_manual, iterations),
            fmt_ops(t_manual, iterations),
            "1.00x (base)",
        ),
    ]


# ---------------------------------------------------------------------------
# Benchmark: JSON serialization
# ---------------------------------------------------------------------------


def bench_json(iterations):
    """Compare hyperdjango's fast_json_dumps vs stdlib json.dumps."""
    from hyperdjango.native import fast_json_dumps

    test_data = {
        "id": 42,
        "name": "Alice Johnson",
        "email": "alice@example.com",
        "active": True,
        "scores": [98.5, 87.3, 92.1, 88.0, 95.6],
        "metadata": {"role": "admin", "level": 5, "verified": True},
    }

    def hd_json():
        fast_json_dumps(test_data)

    def stdlib_json():
        json.dumps(test_data).encode("utf-8")

    t_hd = bench(hd_json, iterations)
    t_stdlib = bench(stdlib_json, iterations)

    speedup = t_stdlib / t_hd if t_hd else float("inf")

    return [
        (
            "json (hyperdjango)",
            fmt_ns(t_hd, iterations),
            fmt_ops(t_hd, iterations),
            f"{speedup:.2f}x",
        ),
        (
            "json (stdlib)",
            fmt_ns(t_stdlib, iterations),
            fmt_ops(t_stdlib, iterations),
            "1.00x (base)",
        ),
    ]


# ---------------------------------------------------------------------------
# Benchmark: Routing
# ---------------------------------------------------------------------------


def bench_routing(iterations):
    """Compare hyperdjango Router vs dict lookup and regex matching."""
    from hyperdjango.router import Router

    def noop(request):
        pass

    # Set up hyperdjango Router
    router = Router()
    for i in range(50):
        router.add("GET", f"/api/v1/resource{i}", noop)
    router.add("GET", "/api/v1/users/{id:int}", noop)
    router.add("GET", "/api/v1/target", noop)

    # Set up dict-based lookup (static only)
    dict_routes = {}
    for i in range(50):
        dict_routes[f"/api/v1/resource{i}"] = noop
    dict_routes["/api/v1/target"] = noop

    # Set up regex-based routing
    regex_routes = []
    for i in range(50):
        regex_routes.append((re.compile(f"^/api/v1/resource{i}$"), noop))
    regex_routes.append((re.compile(r"^/api/v1/users/(\d+)$"), noop))
    regex_routes.append((re.compile(r"^/api/v1/target$"), noop))

    # Benchmark: hyperdjango router -- static path (hit near end of table)
    def hd_route_static():
        router.resolve("GET", "/api/v1/target")

    # Benchmark: hyperdjango router -- dynamic path
    def hd_route_dynamic():
        router.resolve("GET", "/api/v1/users/42")

    # Benchmark: dict lookup
    def dict_route():
        dict_routes.get("/api/v1/target")

    # Benchmark: regex scan
    def regex_route():
        path = "/api/v1/target"
        for pattern, handler in regex_routes:
            m = pattern.match(path)
            if m:
                break

    t_hd_static = bench(hd_route_static, iterations)
    t_hd_dynamic = bench(hd_route_dynamic, iterations)
    t_dict = bench(dict_route, iterations)
    t_regex = bench(regex_route, iterations)

    speedup_static = t_dict / t_hd_static if t_hd_static else float("inf")
    speedup_regex = t_regex / t_hd_dynamic if t_hd_dynamic else float("inf")

    return [
        (
            "route/static (hyperdjango)",
            fmt_ns(t_hd_static, iterations),
            fmt_ops(t_hd_static, iterations),
            f"{speedup_static:.2f}x vs dict",
        ),
        (
            "route/dynamic (hyperdjango)",
            fmt_ns(t_hd_dynamic, iterations),
            fmt_ops(t_hd_dynamic, iterations),
            f"{speedup_regex:.2f}x vs regex",
        ),
        (
            "route/static (dict)",
            fmt_ns(t_dict, iterations),
            fmt_ops(t_dict, iterations),
            "1.00x (base)",
        ),
        (
            "route/static (regex scan)",
            fmt_ns(t_regex, iterations),
            fmt_ops(t_regex, iterations),
            "1.00x (base)",
        ),
    ]


# ---------------------------------------------------------------------------
# Benchmark: Response creation
# ---------------------------------------------------------------------------


def bench_response(iterations):
    """Measure Response.json() throughput."""
    from hyperdjango.response import Response

    test_data = {
        "id": 1,
        "name": "Alice",
        "items": [{"sku": "A1", "qty": 3}, {"sku": "B2", "qty": 7}],
        "total": 129.99,
    }

    def hd_response():
        Response.json(test_data)

    # Manual equivalent: json.dumps + encode + build headers
    def manual_response():
        body = json.dumps(test_data, separators=(",", ":")).encode("utf-8")
        return {
            "status": 200,
            "headers": {"content-type": "application/json; charset=utf-8"},
            "body": body,
        }

    t_hd = bench(hd_response, iterations)
    t_manual = bench(manual_response, iterations)

    speedup = t_manual / t_hd if t_hd else float("inf")

    return [
        (
            "Response.json (hyperdjango)",
            fmt_ns(t_hd, iterations),
            fmt_ops(t_hd, iterations),
            f"{speedup:.2f}x",
        ),
        (
            "response dict (manual)",
            fmt_ns(t_manual, iterations),
            fmt_ops(t_manual, iterations),
            "1.00x (base)",
        ),
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="HyperDjango benchmarks")
    parser.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=100_000,
        help="Number of iterations per benchmark (default: 100000)",
    )
    args = parser.parse_args()
    iterations = args.iterations

    print("HyperDjango Benchmark Suite")
    print("  Native extension: YES")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  Iterations: {iterations:,}")
    print()

    all_rows = []

    benchmarks = [
        ("Validation", bench_validation),
        ("JSON Serialization", bench_json),
        ("Routing", bench_routing),
        ("Response Creation", bench_response),
    ]

    for name, fn in benchmarks:
        print(f"Running: {name} ...")
        try:
            rows = fn(iterations)
            all_rows.extend(rows)
            all_rows.append(("", "", "", ""))  # separator
        except Exception as e:
            print(f"  SKIPPED: {e}")
            all_rows.append((name, "ERROR", str(e)[:30], ""))
            all_rows.append(("", "", "", ""))

    # Remove trailing empty separator
    if all_rows and all_rows[-1] == ("", "", "", ""):
        all_rows.pop()

    print()
    print_table(all_rows)
    print()


if __name__ == "__main__":
    main()
