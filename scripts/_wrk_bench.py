"""
Shared wrk benchmark machinery — used by `bench_hypernews_wrk.py`,
`bench_bookstore_wrk.py`, and any future per-app wrk harness.

Each per-app script provides:
- a list of `WrkTarget` rows (slug, path, label)
- the app import spec + port for `AppRunner`
- the `hyper setup --drop --seed ...` arguments
- optional `pre_hook` (async) that can mutate targets after DB setup
  but before `AppRunner` starts — used by e.g. HyperNews to resolve
  an HMAC-signed post external ID and patch it into the URL set.

It then calls `run_wrk_benchmark(...)` which handles:
- DB drop+seed via `hyper setup`
- DB connect + pre_hook
- `HYPER_POOL_SIZE` env forwarding to the server subprocess
- `AppRunner` lifecycle
- Per-target warmup + N-run median
- Stability rule: ≥ 5 s per run, jitter budget 5 %, per-run rps visible
- Structured JSON + human-readable TXT output in `logs/`

Stability rule matches `feedback_profile_before_optimize.md`:
- Every wrk run is ≥ 5 seconds (default WrkConfig.duration is "8s")
- N=3 runs per endpoint, median rps reported, jitter tracked
- Per-run rps printed so variance is never hidden
- Warmup run primes prepared statements + caches before measurement
- Any run with jitter > 5 % gets a ⚠ warning in stdout
"""

import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from e2e_helper import AppRunner  # noqa: E402

LOGS = Path(__file__).resolve().parent.parent / "logs"


@dataclass(slots=True)
class WrkConfig:
    """wrk invocation parameters + run budget.

    Defaults are tuned for the ≥ 5 s stability rule: duration "8s" with
    multi_run 3 gives a 24 s budget per endpoint (plus one warmup run).
    """

    duration: str = "8s"
    threads: int = 4
    connections: int = 20
    multi_run: int = 3


@dataclass(slots=True)
class WrkTarget:
    """One endpoint the benchmark will hit."""

    slug: str
    path: str
    label: str


@dataclass(slots=True)
class WrkRunSample:
    """Parsed output of a single wrk invocation."""

    rps: float = 0.0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p99_ms: float = 0.0
    errors: int = 0
    total_requests: int = 0


@dataclass(slots=True)
class EndpointResult:
    """Aggregated multi-run result for one endpoint."""

    slug: str
    url: str
    label: str
    median_rps: float
    per_run_rps: list[float]
    jitter_pct: float
    avg_ms: float
    p50_ms: float
    p99_ms: float
    errors: int
    total_requests: int

    def to_dict(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "url": self.url,
            "label": self.label,
            "median_rps": round(self.median_rps, 1),
            "per_run_rps": [round(v, 1) for v in self.per_run_rps],
            "jitter_pct": round(self.jitter_pct, 2),
            "avg_ms": round(self.avg_ms, 3),
            "p50_ms": round(self.p50_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "errors": self.errors,
            "total_requests": self.total_requests,
        }


# ── wrk invocation + output parsing ──────────────────────────────────────

_RE_RPS = re.compile(r"Requests/sec:\s+([\d.]+)")
_RE_LATENCY = re.compile(r"Latency\s+([\d.]+)(us|ms|s)")
_RE_TOTAL = re.compile(r"(\d+) requests in")
_RE_NON_2XX = re.compile(r"Non-2xx or 3xx responses:\s+(\d+)")


def _unit_to_ms(value: float, unit: str) -> float:
    if unit == "us":
        return value / 1000.0
    if unit == "s":
        return value * 1000.0
    return value  # "ms"


def parse_wrk(output: str) -> WrkRunSample:
    """Parse one wrk stdout block into a typed sample."""
    sample = WrkRunSample()

    m = _RE_RPS.search(output)
    if m:
        sample.rps = float(m.group(1))

    m = _RE_LATENCY.search(output)
    if m:
        sample.avg_ms = _unit_to_ms(float(m.group(1)), m.group(2))

    for label, setter in (("50%", "p50_ms"), ("99%", "p99_ms")):
        m = re.search(rf"\s+{re.escape(label)}\s+([\d.]+)(us|ms|s)", output)
        if m:
            setattr(sample, setter, _unit_to_ms(float(m.group(1)), m.group(2)))

    m = _RE_NON_2XX.search(output)
    if m:
        sample.errors = int(m.group(1))

    m = _RE_TOTAL.search(output)
    if m:
        sample.total_requests = int(m.group(1))

    return sample


def run_wrk(url: str, config: WrkConfig) -> WrkRunSample:
    """Invoke wrk once against `url` and parse its output."""
    proc = subprocess.run(
        [
            "wrk",
            f"-t{config.threads}",
            f"-c{config.connections}",
            f"-d{config.duration}",
            "--latency",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return parse_wrk(proc.stdout)


def bench_endpoint(
    target: WrkTarget, base_url: str, config: WrkConfig
) -> EndpointResult:
    """Warmup + N-run median for one endpoint."""
    url = f"{base_url}{target.path}"

    # Warmup run — not scored. Primes prepared statements, LRU caches, pool.
    _ = run_wrk(url, config)

    samples: list[WrkRunSample] = [
        run_wrk(url, config) for _ in range(config.multi_run)
    ]

    rps_values = [s.rps for s in samples]
    rps_sorted = sorted(rps_values)
    median_rps = statistics.median(rps_sorted)
    min_rps = min(rps_values) if rps_values else 0.0
    max_rps = max(rps_values) if rps_values else 0.0
    jitter_pct = ((max_rps - min_rps) / median_rps * 100 / 2) if median_rps else 0.0

    result = EndpointResult(
        slug=target.slug,
        url=url,
        label=target.label,
        median_rps=median_rps,
        per_run_rps=rps_values,
        jitter_pct=jitter_pct,
        avg_ms=statistics.median([s.avg_ms for s in samples]),
        p50_ms=statistics.median([s.p50_ms for s in samples]),
        p99_ms=statistics.median([s.p99_ms for s in samples]),
        errors=sum(s.errors for s in samples),
        total_requests=sum(s.total_requests for s in samples),
    )

    warn = " ⚠ JITTER" if jitter_pct > 5.0 else ""
    err_str = f" ({result.errors} errors)" if result.errors else ""
    print(
        f"  {target.label}: median {median_rps:,.0f} rps | "
        f"per-run {[f'{v:.0f}' for v in rps_values]} | "
        f"jitter ±{jitter_pct:.1f}%{warn}{err_str}"
    )
    print(
        f"    avg {result.avg_ms:.2f}ms | "
        f"p50 {result.p50_ms:.2f}ms | "
        f"p99 {result.p99_ms:.2f}ms"
    )
    return result


# ── Orchestrator ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class WrkBenchmark:
    """Declarative config for a per-app wrk benchmark run.

    Pass one of these to `run_wrk_benchmark(benchmark)` to execute the
    full drop+seed → warmup → benchmark → output pipeline.
    """

    name: str  # header text + output filename base
    app_spec: str  # "services.hypernews.app:app"
    port: int
    targets: list[WrkTarget]
    setup_args: list[str]  # args to `hyper setup` beyond --app
    output_slug: str  # "hypernews" → logs/bench_hypernews_wrk.{json,txt}
    config: WrkConfig = field(default_factory=WrkConfig)
    # Optional async hook that runs AFTER `hyper setup` and the DB
    # pool is connected, but BEFORE AppRunner starts. Can return a new
    # targets list (e.g. HyperNews resolves a dynamic HMAC-signed post
    # external ID and rewrites the path). Return the original list
    # unchanged when no rewrite is needed.
    pre_hook: Callable[[list[WrkTarget]], Awaitable[list[WrkTarget]]] | None = None


def _hyper_setup(app_spec: str, setup_args: list[str]) -> None:
    """Run `hyper setup --drop --seed ...` for the app. Aborts on failure."""
    print("\nSetting up database (drop + seed)...")
    proc = subprocess.run(
        ["uv", "run", "hyper", "setup", "--app", app_spec, *setup_args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        print(f"  setup failed: {proc.stderr[-500:]}")
        sys.exit(1)


def _dump_outputs(benchmark: WrkBenchmark, results: list[EndpointResult]) -> None:
    """Write logs/bench_<slug>_wrk.{json,txt}."""
    LOGS.mkdir(parents=True, exist_ok=True)

    json_path = LOGS / f"bench_{benchmark.output_slug}_wrk.json"
    json_path.write_text(
        json.dumps(
            {
                "name": benchmark.name,
                "wrk_threads": benchmark.config.threads,
                "wrk_connections": benchmark.config.connections,
                "wrk_duration": benchmark.config.duration,
                "multi_run": benchmark.config.multi_run,
                "results": [r.to_dict() for r in results],
            },
            indent=2,
        )
    )
    print(f"\n  JSON: {json_path}")

    txt_path = LOGS / f"bench_{benchmark.output_slug}_wrk.txt"
    lines = [
        "=" * 70,
        f"  {benchmark.name} wrk Benchmark Summary",
        f"  wrk: -t{benchmark.config.threads} -c{benchmark.config.connections} "
        f"-d{benchmark.config.duration} × {benchmark.config.multi_run} runs",
        "=" * 70,
        "",
        f"{'Endpoint':<55} {'rps':>10}  {'jitter':>8}  {'p50':>8}  {'p99':>8}",
        "-" * 95,
    ]
    for r in results:
        lines.append(
            f"{r.label:<55} "
            f"{r.median_rps:>10,.0f}  "
            f"±{r.jitter_pct:>6.1f}%  "
            f"{r.p50_ms:>6.2f}ms  "
            f"{r.p99_ms:>6.2f}ms"
        )
    lines.append("")
    lines.append(f"Generated: {json_path}")
    txt_path.write_text("\n".join(lines))
    print(f"  TXT:  {txt_path}")


def run_wrk_benchmark(benchmark: WrkBenchmark) -> list[EndpointResult]:
    """Execute a full wrk benchmark run end-to-end.

    Returns the aggregated per-endpoint results so callers can assert
    on them in tests or follow-up analysis.
    """
    LOGS.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(f"  {benchmark.name} wrk benchmark (wire-speed, multi-run median)")
    print(
        f"  Endpoints: {len(benchmark.targets)} | "
        f"runs: {benchmark.config.multi_run} | "
        f"duration: {benchmark.config.duration}"
    )
    print(f"  wrk: -t{benchmark.config.threads} -c{benchmark.config.connections}")
    print("=" * 70)

    _hyper_setup(benchmark.app_spec, benchmark.setup_args)

    os.environ["HYPER_LOAD_TEST"] = "1"
    os.environ["RATE_LIMIT"] = "0"

    # Run the pre-hook (if any) before AppRunner starts. The pre-hook
    # can connect the DB, seed additional data, and mutate targets.
    targets = benchmark.targets
    if benchmark.pre_hook is not None:
        targets = asyncio.run(benchmark.pre_hook(list(targets)))

    # Forward HYPER_POOL_SIZE override to the AppRunner subprocess.
    runner_env: dict[str, str] = {}
    pool_size = int(os.environ.get("HYPER_POOL_SIZE", "0"))
    if pool_size > 0:
        runner_env["HYPER_POOL_SIZE"] = str(pool_size)
        print(f"  HYPER_POOL_SIZE override: {pool_size}")

    results: list[EndpointResult] = []
    with AppRunner(
        benchmark.app_spec,
        host="127.0.0.1",
        port=benchmark.port,
        env=runner_env,
    ) as runner:
        base = runner.url()
        for target in targets:
            print(f"\n── {target.label} — {base}{target.path} ──")
            results.append(bench_endpoint(target, base, benchmark.config))

    _dump_outputs(benchmark, results)

    print("\n" + "=" * 70)
    print(f"  {benchmark.name} wrk benchmark complete")
    print("=" * 70)

    return results
