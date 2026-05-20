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
prior run measured is lost and any two runs can be compared over time. RESTRICTED
runs (a ``--quick`` matrix, a half-measured suite) archive under
``<outdir>/diagnostics/`` instead: same format, kept for the investigation
record, invisible to every comparison surface.

A run entry also DECLARES its intended coverage (`expected_suites`), so a record
missing a suite it was supposed to carry reads as incomplete instead of being
reverse-engineered as "whatever it happens to contain is what it meant".

ONE run entry can be fed by SEVERAL suite runs. The HTTP suite and the WebSocket
suite are separate processes, so `save_run` merges a feed into the matching
existing entry (see `save_run`'s merge semantics) rather than minting a second
half-record — a canonical record carries EVERY suite, and the dashboard's Suite
selector switches views of that one entry. Each suite keeps a `provenance` stamp
(when it was measured, under what label/commit/host), so a merged record never
hides that its suites were measured at different times. Already-archived entries
can be combined after the fact with ``python -m benchmarks.core.merge``.
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


def group(key: str, label: str | None = None, size_bytes: int | None = None) -> dict:
    """A within-sweep facet (e.g. a payload size) shown as small-multiples.

    `size_bytes` is the facet's payload size in bytes when it has one; it travels
    on the wire under the key ``"bytes"`` (the archived schema's name) — the
    PARAMETER is spelled out so it does not shadow the ``bytes`` builtin."""
    g = {"key": key, "label": label or key}
    if size_bytes is not None:
        g["bytes"] = size_bytes
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
    metrics: list[str] | None = None,
    groups_label: str = "",
) -> dict:
    """One generic sweep block. `data` is ``{"<variant>|<group>": {metric: [...]}}``;
    for a single-group sweep use group key ``""``. `desc` is a one-line purpose
    description shown above the chart.

    `metrics` is the subset of the SUITE's metric keys this sweep actually
    records. The dashboard's Metric selector intersects with it, so a sweep that
    measures throughput but not served-connection fraction does not offer an
    empty chart. Leave it empty to mean "every metric the suite declares".

    `groups_label` names the facet dimension (``"payload"``, ``"frame type"``) and
    travels on the wire as ``groupsLabel``.

    Both are part of the SIGNATURE precisely because the dashboard reads them:
    a sweep declares its full shape in one call, instead of being constructed
    half-formed and patched afterwards."""
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
        "metrics": list(metrics or []),
        "groupsLabel": groups_label,
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


# A run entry is ONE canonical record and may be fed by SEVERAL suite runs (the
# HTTP suite and the WebSocket suite are separate processes, minutes to an hour
# apart). `save_run` therefore MERGES INTO an existing entry rather than minting
# a second one whenever the new suites plainly belong to the same record — same
# label, same commit, same host, no suite the entry already carries, inside a
# bounded time window. Every suite records where it came from (`provenance`), so
# a merged record never hides that its suites were measured at different times.
MERGE_WINDOW_HOURS = 24.0

_SUMMARY_KEYS = ("id", "ts", "sha", "branch", "subject", "host", "cores", "label")

# COMPLETE runs archive under `history/` — the ONLY archive `load_history` (and
# therefore the dashboard), `read_index`, and `find_entry` (the merge-target
# lookup) ever read. RESTRICTED runs — a --quick smoke matrix, a suite whose
# measurement half-failed — archive under `diagnostics/` in the same format:
# preserved for the investigation record, structurally invisible to every
# comparison surface. Same doctrine the HTTP suite's own archive applies; a
# partial run is a note to yourself, never a record anyone can compare against.
HISTORY_DIR = "history"
DIAGNOSTICS_DIR = "diagnostics"


def _archive(outdir: str, diagnostic: bool = False) -> pathlib.Path:
    d = pathlib.Path(outdir) / (DIAGNOSTICS_DIR if diagnostic else HISTORY_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hist(outdir: str) -> pathlib.Path:
    return _archive(outdir, False)


def _read_index_at(archive: pathlib.Path) -> list[dict]:
    idx = archive / "index.json"
    if not idx.exists():
        return []
    try:
        index = json.loads(idx.read_text())
    except Exception:  # noqa: BLE001
        return []
    return index if isinstance(index, list) else []


def _write_index_at(archive: pathlib.Path, index: list[dict]) -> None:
    index.sort(key=lambda e: e.get("ts", ""))
    (archive / "index.json").write_text(json.dumps(index, indent=2))


def read_index(outdir: str) -> list[dict]:
    """The run index (oldest first), or [] when there is none / it is unreadable.
    HISTORY ONLY — diagnostics keep their own index and never appear here."""
    return _read_index_at(_hist(outdir))


def summarize(entry: dict) -> dict:
    """The index row for an entry: its identity, the suites it carries, and (when
    it declared one) the suites it was SUPPOSED to carry."""
    s = {k: entry.get(k) for k in _SUMMARY_KEYS}
    s["suites"] = list(entry.get("suites", {}).keys())
    if entry.get("expected_suites"):
        s["expected_suites"] = list(entry["expected_suites"])
    if entry.get("merged_from"):
        s["merged_from"] = list(entry["merged_from"])
    return s


def write_index(outdir: str, index: list[dict]) -> None:
    _write_index_at(_hist(outdir), index)


def read_entry(outdir: str, run_id: str) -> dict | None:
    p = _hist(outdir) / f"{run_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def write_entry(outdir: str, entry: dict) -> None:
    (_hist(outdir) / f"{entry['id']}.json").write_text(json.dumps(entry, indent=2))


def find_entry(outdir: str, ref: str) -> dict | None:
    """Resolve a run reference — an exact run id, else the NEWEST entry with that
    label. Indexed entries win; an entry that was merged away (dropped from the
    index, kept on disk) is still resolvable by id."""
    hit = read_entry(outdir, ref)
    if hit is not None:
        return hit
    matches = [e for e in read_index(outdir) if e.get("label") == ref]
    if matches:
        matches.sort(key=lambda e: e.get("ts", ""))
        return read_entry(outdir, matches[-1]["id"])
    # Fall back to on-disk entries so a merged-away source is still addressable.
    disk = []
    for p in _hist(outdir).glob("*.json"):
        if p.name == "index.json":
            continue
        try:
            e = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            continue
        if e.get("label") == ref:
            disk.append(e)
    disk.sort(key=lambda e: e.get("ts", ""))
    return disk[-1] if disk else None


def _provenance(entry: dict, suite_keys: list[str]) -> dict:
    """Per-suite origin stamp: when it was measured and under what label/commit."""
    return {
        k: {
            "ts": entry["ts"],
            "label": entry.get("label", ""),
            "sha": entry.get("sha", ""),
            "host": entry.get("host", ""),
            "run_id": entry["id"],
        }
        for k in suite_keys
    }


def _mergeable(
    target: dict,
    *,
    sha: str,
    host: str,
    now: datetime.datetime,
    suites: dict,
    window_hours: float,
) -> bool:
    """Auto-merge is only safe when the candidate is plainly the SAME record:
    same commit, same host, recent, and carrying none of the incoming suites
    (re-feeding a suite must start a new record, never overwrite a measured one)."""
    if target.get("sha") != sha or target.get("host") != host:
        return False
    if set(target.get("suites", {})) & set(suites):
        return False
    try:
        age = now - datetime.datetime.fromisoformat(target["ts"])
    except Exception:  # noqa: BLE001
        return False
    return datetime.timedelta(0) <= age <= datetime.timedelta(hours=window_hours)


def declared_suites(entry: dict) -> list[str]:
    """What an entry says it was SUPPOSED to carry. Entries archived before
    `expected_suites` existed declare nothing, so they fall back to what they
    actually carry — migrate-on-read, never a rewrite of the archive."""
    return list(entry.get("expected_suites") or entry.get("suites", {}).keys())


def save_run(
    outdir: str,
    suites: dict,
    label: str = "",
    cores: int | None = None,
    *,
    expected_suites: list[str] | None = None,
    diagnostic: bool = False,
    merge_into: str = "",
    merge: bool = True,
    merge_window_hours: float = MERGE_WINDOW_HOURS,
) -> str:
    """Archive one feed of ``{suite_key: suite_block}`` and return the id of the
    entry it landed in. Never overwrites a suite another run measured.

    COMPLETE feeds land in ``<outdir>/history/`` — the only archive the
    dashboard, `read_index`, `find_entry` and `load_history` ever read.
    ``diagnostic=True`` (a restricted / partially-measured run) lands in
    ``<outdir>/diagnostics/`` in the same format, keeping the investigation
    record while staying invisible to every comparison surface. A diagnostic
    never merges: quarantine that leaks into a canonical record is not
    quarantine, so `merge_into` alongside it is an error.

    `expected_suites` DECLARES what the record was supposed to contain, so
    coverage is stated rather than reverse-engineered from whatever happens to be
    present: a ``bench-all`` run declares both suites the moment the first one is
    fed, and a record still missing the second reads as visibly incomplete
    instead of silently "complete". It defaults to the suites in this feed, and
    merging UNIONS the declarations of both sides.

    `merge_into` targets an explicit entry (run id or label) and REPLACES any
    suite of the same key there — the deliberate "correct this record" path.
    Otherwise, with `merge` on (the default), a feed carrying a label whose
    newest entry is the same commit+host, is inside `merge_window_hours`, and
    lacks every incoming suite, MERGES INTO that entry: that is how two runners
    (HTTP then WebSocket) invoked with the same ``--label`` produce ONE canonical
    two-suite record instead of two half-records. Anything else mints a new run."""
    now = datetime.datetime.now()
    g = git_info()
    host = socket.gethostname()
    declared = sorted(set(expected_suites if expected_suites is not None else suites))

    if diagnostic:
        if merge_into:
            raise ValueError(
                "merge_into is not available for a diagnostic run — a quarantined "
                "run must never write into a canonical record"
            )
        return _write_new(
            outdir,
            suites,
            label=label,
            cores=cores,
            declared=declared,
            now=now,
            g=g,
            host=host,
            diagnostic=True,
        )

    index = read_index(outdir)

    target: dict | None = None
    if merge_into:
        target = find_entry(outdir, merge_into)
        if target is None:
            raise ValueError(
                f"merge_into={merge_into!r} matched no archived run in {outdir}/history/"
            )
    elif merge and label:
        cand = [e for e in index if e.get("label") == label]
        cand.sort(key=lambda e: e.get("ts", ""))
        for e in reversed(cand):
            full = read_entry(outdir, e["id"])
            if full and _mergeable(
                full,
                sha=g["sha"],
                host=host,
                now=now,
                suites=suites,
                window_hours=merge_window_hours,
            ):
                target = full
                break

    if target is not None:
        stamp = {
            "ts": now.isoformat(timespec="seconds"),
            "label": label or "",
            "sha": g["sha"],
            "host": host,
            "run_id": target["id"],
        }
        target.setdefault("suites", {}).update(suites)
        target.setdefault("provenance", {}).update({k: dict(stamp) for k in suites})
        # The record now stands for BOTH declarations — the target's (or, for a
        # pre-declaration entry, what it carries) unioned with this feed's.
        target["expected_suites"] = sorted(set(declared_suites(target)) | set(declared))
        target["merged"] = True
        if cores is not None and target.get("cores") is None:
            target["cores"] = cores
        write_entry(outdir, target)
        index = [e for e in index if e.get("id") != target["id"]]
        index.append(summarize(target))
        write_index(outdir, index)
        return str(target["id"])

    return _write_new(
        outdir,
        suites,
        label=label,
        cores=cores,
        declared=declared,
        now=now,
        g=g,
        host=host,
        diagnostic=False,
        index=index,
    )


def _write_new(
    outdir: str,
    suites: dict,
    *,
    label: str,
    cores: int | None,
    declared: list[str],
    now: datetime.datetime,
    g: dict,
    host: str,
    diagnostic: bool,
    index: list[dict] | None = None,
) -> str:
    """Mint a fresh entry in the requested archive (history or diagnostics) and
    add its row to THAT archive's index."""
    archive = _archive(outdir, diagnostic)
    run_id = (
        now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}_" + g["sha"]
    )
    entry = {
        "id": run_id,
        "ts": now.isoformat(timespec="seconds"),
        "sha": g["sha"],
        "branch": g["branch"],
        "subject": g["subject"],
        "host": host,
        "cores": cores,
        "label": label or "",
        "expected_suites": declared,
        "suites": suites,
    }
    if diagnostic:
        entry["diagnostic"] = True
    entry["provenance"] = _provenance(entry, list(suites))
    (archive / f"{run_id}.json").write_text(json.dumps(entry, indent=2))

    rows = _read_index_at(archive) if index is None else index
    rows = [e for e in rows if e.get("id") != run_id]
    rows.append(summarize(entry))
    _write_index_at(archive, rows)
    return run_id


def diagnostics_index(outdir: str) -> list[dict]:
    """The quarantined runs' index (oldest first). Read this to INSPECT the
    investigation record; nothing that compares runs may consult it."""
    return _read_index_at(pathlib.Path(outdir) / DIAGNOSTICS_DIR)


def load_history(outdir: str) -> list[dict]:
    """Load every archived COMPLETE run (oldest first), each a full entry with
    suite data. Diagnostics live in their own archive and are never returned."""
    hist = pathlib.Path(outdir) / HISTORY_DIR
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
