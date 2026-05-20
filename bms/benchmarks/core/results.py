"""Unified benchmark result schema + non-destructive run history.

Every subsystem emits the *same* shape so one dashboard can unify all of it:

    Run   — one invocation, git + time stamped, on one host.
      Suite   — one subsystem: "http", "websocket", "startup", …
        Sweep   — one chart family: varies ONE x-dimension (concurrency, workers,
                  payload size, connection count) across `variants` (the servers /
                  frameworks under test), over one or more `groups` (e.g. payload
                  sizes shown as small-multiples), recording `metrics`.

A Sweep's `data` is keyed ``"<variant>|<group>"`` → ``{metric_key: [values aligned to xs]}``.
`refs` are vertical reference lines (configured limit / CPU cores) that make a
plateau legible as a config ceiling vs a hardware ceiling vs real degradation.

Runs are archived under ``<outdir>/history/`` and never overwritten, so nothing a
prior run measured is lost and any two runs can be compared over time.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import pathlib
import socket
import subprocess

# ── Schema builders (plain dicts — the dashboard consumes JSON directly) ──────


def metric(key: str, label: str, unit: str, lower_is_better: bool = False) -> dict:
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "lower_is_better": lower_is_better,
    }


def group(key: str, label: str | None = None, bytes: int | None = None) -> dict:
    """A within-sweep facet (e.g. a payload size) shown as small-multiples."""
    g = {"key": key, "label": label or key}
    if bytes is not None:
        g["bytes"] = bytes
    return g


def ref(value: float, label: str, kind: str = "sys") -> dict:
    """A vertical reference line. kind: 'cfg' (configured limit) | 'sys' (hardware)."""
    return {"v": value, "label": label, "kind": kind}


def sweep(
    *,
    key: str,
    label: str,
    xtitle: str,
    xs: list,
    variants: list[str],
    data: dict,
    groups: list[dict] | None = None,
    xlog: bool = False,
    refs: list[dict] | None = None,
    note: str = "",
    desc: str = "",
) -> dict:
    """One generic sweep block. `data` is ``{"<variant>|<group>": {metric: [...]}}``;
    for a single-group sweep use group key ``""``. `desc` is a one-line purpose
    description shown above the chart."""
    return {
        "key": key,
        "label": label,
        "xtitle": xtitle,
        "xs": list(xs),
        "xlog": bool(xlog),
        "variants": list(variants),
        "groups": groups or [group("", "")],
        "refs": refs or [],
        "note": note,
        "desc": desc,
        "data": data,
    }


def suite(
    *,
    key: str,
    label: str,
    variants: list[str],
    metrics: list[dict],
    sweeps: dict,
    colors: dict | None = None,
    configs: dict | None = None,
    interpreter: str = "",
    note: str = "",
) -> dict:
    """One subsystem's results: its variants, metrics, per-variant launch configs,
    and its sweeps (chart families)."""
    return {
        "key": key,
        "label": label,
        "variants": list(variants),
        "metrics": metrics,
        "sweeps": sweeps,
        "colors": colors or {},
        "configs": configs or {},
        "interpreter": interpreter,
        "note": note,
    }


# ── Non-destructive run history ──────────────────────────────────────────────


def git_info() -> dict:
    def g(args):
        try:
            return subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    return {
        "sha": g(["rev-parse", "--short", "HEAD"]) or "nogit",
        "branch": g(["rev-parse", "--abbrev-ref", "HEAD"]),
        "subject": g(["log", "-1", "--pretty=%s"]),
    }


def save_run(
    outdir: str, suites: dict, label: str = "", cores: int | None = None
) -> str:
    """Archive one run (a dict of ``{suite_key: suite_block}``) to
    ``<outdir>/history/`` and return its id. Never overwrites a prior run."""
    d = pathlib.Path(outdir)
    hist = d / "history"
    hist.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    g = git_info()
    run_id = (
        now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}_" + g["sha"]
    )
    entry = {
        "id": run_id,
        "ts": now.isoformat(timespec="seconds"),
        "sha": g["sha"],
        "branch": g["branch"],
        "subject": g["subject"],
        "host": socket.gethostname(),
        "cores": cores,
        "label": label or "",
        "suites": suites,
    }
    (hist / f"{run_id}.json").write_text(json.dumps(entry, indent=2))

    idx_path = hist / "index.json"
    index = []
    if idx_path.exists():
        try:
            index = json.loads(idx_path.read_text())
        except Exception:  # noqa: BLE001
            index = []
    index = [e for e in index if e.get("id") != run_id]
    summary = {
        k: entry[k]
        for k in ("id", "ts", "sha", "branch", "subject", "host", "cores", "label")
    }
    summary["suites"] = list(suites.keys())
    index.append(summary)
    index.sort(key=lambda e: e.get("ts", ""))
    idx_path.write_text(json.dumps(index, indent=2))
    return run_id


def load_history(outdir: str) -> list[dict]:
    """Load every archived run (oldest first), each a full entry with suite data."""
    hist = pathlib.Path(outdir) / "history"
    idx_path = hist / "index.json"
    if not idx_path.exists():
        return []
    try:
        index = json.loads(idx_path.read_text())
    except Exception:  # noqa: BLE001
        return []
    runs = []
    for e in index:
        p = hist / f"{e['id']}.json"
        if p.exists():
            with contextlib.suppress(Exception):
                runs.append(json.loads(p.read_text()))
    runs.sort(key=lambda r: r.get("ts", ""))
    return runs
