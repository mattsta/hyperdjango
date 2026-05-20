#!/usr/bin/env python3
"""Unified benchmark record: merge semantics.

A canonical record must carry EVERY suite — the dashboard's Suite selector
switches views of ONE entry, not between two half-records. This pins how suites
from separate feeds land in a single entry:

- `save_run` merge-into on label collision (HTTP feeds, WebSocket merges in),
  with its safety rails: no silent overwrite of a measured suite, no merging
  across commits/hosts, no merging outside the time window.
- `benchmarks.core.merge` combining ALREADY-archived entries: per-suite
  provenance kept, one index row, sources retained on disk with a merged-into
  pointer, and a repeat merge that is a no-op.

Everything here is synthetic and local — no servers, no network, no box.

Usage:
    uv run hyper-test bench_core_merge
"""

# hyper-test: unit

import datetime
import json
import tempfile
from pathlib import Path

from benchmarks.core.merge import merge_runs
from benchmarks.core.results import (
    load_history,
    read_entry,
    read_index,
    save_run,
    write_entry,
)
from hyperdjango.testkit import check, finish, run_main


def _suite(key: str, marker: float) -> dict:
    """A minimal but schema-shaped suite block."""
    return {
        "key": key,
        "label": key.upper(),
        "variants": ["a"],
        "metrics": [{"key": "t", "label": "Throughput", "unit": "req/s"}],
        "sweeps": {
            "s": {
                "key": "s",
                "label": "S",
                "xtitle": "x",
                "xs": [1],
                "xlog": False,
                "variants": ["a"],
                "groups": [{"key": "", "label": ""}],
                "refs": [],
                "note": "",
                "desc": "d",
                "data": {"a|": {"t": [marker]}},
            }
        },
        "colors": {},
        "configs": {},
        "interpreter": "",
        "note": "",
    }


def _entry_files(out: str) -> list[Path]:
    return sorted(
        p for p in (Path(out) / "history").glob("*.json") if p.name != "index.json"
    )


def _save_run_merge_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        http_id = save_run(
            out, {"http": _suite("http", 1.0)}, label="canonical", cores=8
        )
        ws_id = save_run(
            out, {"websocket": _suite("websocket", 2.0)}, label="canonical"
        )

        check(
            "a second feed under the same label merges into the SAME entry",
            ws_id == http_id,
            f"{ws_id} vs {http_id}",
        )
        entry = read_entry(out, http_id)
        check(
            "the merged entry carries both suites",
            set(entry["suites"]) == {"http", "websocket"},
            str(sorted(entry["suites"])),
        )
        check("merged entry is flagged as merged", entry.get("merged") is True)
        check(
            "each suite records its own provenance",
            set(entry["provenance"]) == {"http", "websocket"}
            and all(
                p["ts"] and p["run_id"] == http_id for p in entry["provenance"].values()
            ),
            str(entry["provenance"]),
        )
        check("merging keeps the entry's core count", entry["cores"] == 8)

        index = read_index(out)
        check("the index shows ONE record, not two", len(index) == 1, str(index))
        check(
            "the index row states both suites",
            sorted(index[0]["suites"]) == ["http", "websocket"],
            str(index[0]),
        )
        check("only one entry file exists", len(_entry_files(out)) == 1)

        runs = load_history(out)
        check(
            "history exposes one record covering both suites",
            len(runs) == 1 and set(runs[0]["suites"]) == {"http", "websocket"},
        )

        # Non-destructive rail: re-feeding a suite the record already holds must
        # NOT overwrite the measured one — it starts a fresh record.
        again = save_run(out, {"http": _suite("http", 9.0)}, label="canonical")
        check(
            "re-feeding an already-present suite mints a NEW record",
            again != http_id,
            f"{again} vs {http_id}",
        )
        check(
            "the original measurement is untouched",
            read_entry(out, http_id)["suites"]["http"]["sweeps"]["s"]["data"]["a|"]["t"]
            == [1.0],
        )
        check("both records are indexed", len(read_index(out)) == 2)


def _save_run_guard_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        base = save_run(out, {"http": _suite("http", 1.0)}, label="canonical")

        other = save_run(
            out, {"websocket": _suite("websocket", 2.0)}, label="different"
        )
        check(
            "a different label never merges",
            other != base and len(read_index(out)) == 2,
        )

        # Commit mismatch: a record must not silently blend suites from two shas.
        entry = read_entry(out, base)
        entry["sha"] = "deadbee"
        write_entry(out, entry)
        idx = read_index(out)
        for row in idx:
            if row["id"] == base:
                row["sha"] = "deadbee"
        (Path(out) / "history" / "index.json").write_text(json.dumps(idx, indent=2))
        cross = save_run(
            out, {"websocket": _suite("websocket", 3.0)}, label="canonical"
        )
        check("a different commit never merges", cross != base)

    with tempfile.TemporaryDirectory() as out:
        stale = save_run(out, {"http": _suite("http", 1.0)}, label="canonical")
        entry = read_entry(out, stale)
        old = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat(
            timespec="seconds"
        )
        entry["ts"] = old
        write_entry(out, entry)
        idx = read_index(out)
        idx[0]["ts"] = old
        (Path(out) / "history" / "index.json").write_text(json.dumps(idx, indent=2))
        fresh = save_run(
            out, {"websocket": _suite("websocket", 2.0)}, label="canonical"
        )
        check("a record older than the merge window never merges", fresh != stale)

    with tempfile.TemporaryDirectory() as out:
        first = save_run(out, {"http": _suite("http", 1.0)}, label="canonical")
        save_run(out, {"http": _suite("http", 5.0)}, label="canonical")
        # Explicit merge_into is the deliberate "correct this record" path and
        # DOES replace a suite of the same key.
        forced = save_run(
            out, {"http": _suite("http", 7.0)}, label="fix", merge_into=first
        )
        check("explicit merge_into targets the named entry", forced == first)
        check(
            "explicit merge_into replaces that suite",
            read_entry(out, first)["suites"]["http"]["sweeps"]["s"]["data"]["a|"]["t"]
            == [7.0],
        )
        check(
            "the replacement records its own provenance label",
            read_entry(out, first)["provenance"]["http"]["label"] == "fix",
        )
        try:
            save_run(out, {"http": _suite("http", 1.0)}, merge_into="nope")
            raised = False
        except ValueError:
            raised = True
        check("an unresolvable merge_into fails loudly", raised)

        no_merge = save_run(
            out, {"websocket": _suite("websocket", 1.0)}, label="canonical", merge=False
        )
        check(
            "merge=False always mints a new record",
            read_entry(out, no_merge)["suites"].keys() == {"websocket"},
        )


def _merge_cli_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        http_id = save_run(
            out, {"http": _suite("http", 1.0)}, label="canonical-baseline"
        )
        ws_id = save_run(
            out, {"websocket": _suite("websocket", 2.0)}, label="ws-full-refeed"
        )
        check("two separate feeds start as two records", len(read_index(out)) == 2)

        combined = merge_runs(out, ["canonical-baseline", "ws-full-refeed"], quiet=True)
        check("merge resolves both refs and combines them", bool(combined))
        entry = read_entry(out, combined)
        check(
            "the combined record carries BOTH suites",
            set(entry["suites"]) == {"http", "websocket"},
            str(sorted(entry["suites"])),
        )
        check(
            "the combined record keeps the base's label",
            entry["label"] == "canonical-baseline",
        )
        check(
            "the combined record names its sources",
            sorted(entry["merged_from"]) == sorted([http_id, ws_id]),
        )
        check(
            "per-suite provenance points at the source runs",
            entry["provenance"]["http"]["run_id"] == http_id
            and entry["provenance"]["websocket"]["run_id"] == ws_id,
            str(entry["provenance"]),
        )
        check(
            "provenance keeps each suite's original label + timestamp",
            entry["provenance"]["websocket"]["label"] == "ws-full-refeed"
            and entry["provenance"]["websocket"]["ts"],
        )
        check(
            "suite payloads survive the merge intact",
            entry["suites"]["http"]["sweeps"]["s"]["data"]["a|"]["t"] == [1.0]
            and entry["suites"]["websocket"]["sweeps"]["s"]["data"]["a|"]["t"] == [2.0],
        )

        index = read_index(out)
        check("the index shows ONE record after the merge", len(index) == 1, str(index))
        check("the indexed record is the combined one", index[0]["id"] == combined)
        check(
            "the index row states both suites",
            sorted(index[0]["suites"]) == ["http", "websocket"],
        )

        check(
            "source JSON files are retained on disk",
            read_entry(out, http_id) is not None and read_entry(out, ws_id) is not None,
        )
        check(
            "each source carries a merged-into pointer",
            read_entry(out, http_id)["merged_into"] == combined
            and read_entry(out, ws_id)["merged_into"] == combined,
        )

        runs = load_history(out)
        check(
            "the dashboard sees exactly one record, with both suites",
            len(runs) == 1 and set(runs[0]["suites"]) == {"http", "websocket"},
            str([sorted(r["suites"]) for r in runs]),
        )

        files_before = len(_entry_files(out))
        repeat = merge_runs(out, ["canonical-baseline", "ws-full-refeed"], quiet=True)
        check("a repeat merge is a no-op", repeat is None)
        check(
            "a repeat merge writes no new entry", len(_entry_files(out)) == files_before
        )
        check("a repeat merge leaves one indexed record", len(read_index(out)) == 1)

        single = merge_runs(out, ["canonical-baseline"], quiet=True)
        check("merging a single record is a no-op", single is None)


def _merge_conflict_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        old_http = save_run(out, {"http": _suite("http", 1.0)}, label="old")
        new_http = save_run(out, {"http": _suite("http", 4.0)}, label="new")
        combined = merge_runs(out, ["old", "new"], label="combined", quiet=True)
        entry = read_entry(out, combined)
        check(
            "a suite measured twice keeps the NEWER measurement",
            entry["suites"]["http"]["sweeps"]["s"]["data"]["a|"]["t"] == [4.0],
        )
        check(
            "provenance points at the run the kept suite came from",
            entry["provenance"]["http"]["run_id"] == new_http,
            f"{entry['provenance']['http']} (old={old_http})",
        )
        check(
            "an explicit --label renames the combined record",
            entry["label"] == "combined",
        )

        missing = merge_runs(out, ["combined", "does-not-exist"], quiet=True)
        check("an unresolvable ref cannot force a bogus merge", missing is None)


def main() -> bool:
    _save_run_merge_checks()
    _save_run_guard_checks()
    _merge_cli_checks()
    _merge_conflict_checks()
    return finish()


if __name__ == "__main__":
    run_main(main)
