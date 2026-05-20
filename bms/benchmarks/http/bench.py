"""Register the HTTP framework comparison with the unified benchmark registry.
Reuses the existing concurrency + worker sweeps (automated per-framework server
setup/teardown via ServerFixture, wrk/python client) and returns a core suite."""

from __future__ import annotations

from types import SimpleNamespace

import benchmarks.http.run as hr
from benchmarks.core.registry import register
from benchmarks.http.suite import build_http_suite


@register(
    "http",
    "HTTP frameworks",
    "hyperdjango (threaded/reactor) vs FastAPI vs Flask across concurrency & worker count",
)
def run_http(quick: bool = False, frameworks=None, duration=None, **opts) -> dict:
    hr.CLIENT = "wrk" if hr.wrk_available() else "python"
    import os

    args = SimpleNamespace(
        quick=quick,
        workers=8,
        duration=(1.0 if quick else 2.0) if duration is None else duration,
        warmup=0.4 if quick else 0.6,
        client_procs=None,
        sweep_concurrency=128,
        worker_counts="8,18" if quick else "1,2,4,8,12,16,18,24,32,40,50,64",
        cs_workers=os.cpu_count() or 8,
        cs_think_ms=25.0,
        outdir="benchmarks/http/out",
    )
    fws = frameworks or hr.ALL_FRAMEWORKS
    print(f"[http] client={hr.CLIENT} frameworks={fws}")
    conc, conc_meta = hr._concurrency_sweep(fws, args)
    work, work_meta = hr._worker_sweep(fws, args)
    cs, cs_meta = hr._conn_scaling_sweep(fws, args)
    entry = {
        "sweeps": {
            "concurrency": {"meta": conc_meta, "results": conc},
            "workers": {"meta": work_meta, "results": work},
        },
        "conn_scaling": {"meta": cs_meta, "results": cs},
    }
    return build_http_suite(entry)
