#!/usr/bin/env python3
"""Release stamps: the canonical UTC-ms trunk-release version format.

Covers format/parse round-trip, display rendering, the forward-only mint
guard (clock-skew clamp), rejection of non-stamps, and the `hyper release`
CLI against a scratch pyproject (dry-run + --apply + guard through the CLI).

Usage:
    uv run hyper-test release_stamp
"""

# hyper-test: unit

import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hyperdjango.testkit import check, finish, run_main
from hyperdjango.versioning import (
    RELEASE_STAMP_LENGTH,
    format_release_stamp,
    mint_release_stamp,
    parse_release_stamp,
    release_stamp_display,
)

_T = datetime(2026, 7, 25, 14, 39, 40, 411_000, tzinfo=UTC)


def check_format_parse() -> None:
    stamp = format_release_stamp(_T)
    check("format is the canonical digit run", stamp == "20260725143940411")
    check("fixed width", len(stamp) == RELEASE_STAMP_LENGTH)
    check("round-trips exactly", parse_release_stamp(stamp) == _T)
    check(
        "sub-millisecond precision is truncated, not rounded up",
        format_release_stamp(_T.replace(microsecond=411_999)) == stamp,
    )
    check(
        "non-UTC input converts (stamps are always UTC)",
        format_release_stamp(_T.astimezone()) == stamp,
    )
    check(
        "pre-10:00 hours keep their leading zero",
        format_release_stamp(_T.replace(hour=9)) == "20260725093940411",
    )
    check(
        "lexical order == chronological order",
        format_release_stamp(_T) < format_release_stamp(_T + timedelta(milliseconds=1)),
    )
    check(
        "display rendering",
        release_stamp_display("20260725143940411") == "2026-07-25 14:39:40.411Z",
    )

    for bad in ("", "0.18.0", "20260725143940", "2026072514394041a", "9" * 18):
        try:
            parse_release_stamp(bad)
        except ValueError:
            check(f"rejects {bad!r}", True)
        else:
            check(f"rejects {bad!r}", False)
    try:
        parse_release_stamp("20261325143940411")  # month 13
    except ValueError:
        check("rejects impossible instants", True)
    else:
        check("rejects impossible instants", False)


def check_mint_guard() -> None:
    check(
        "mint with no floor uses the clock",
        mint_release_stamp(last="", now=_T) == "20260725143940411",
    )
    check(
        "legacy static version imposes no floor",
        mint_release_stamp(last="0.18.0", now=_T) == "20260725143940411",
    )
    check(
        "normal forward mint passes through",
        mint_release_stamp(last="20260725143940410", now=_T) == "20260725143940411",
    )
    check(
        "clock at the floor clamps to floor+1ms",
        mint_release_stamp(last="20260725143940411", now=_T) == "20260725143940412",
    )
    check(
        "clock BEHIND the floor clamps forward (never sorts backwards)",
        mint_release_stamp(last="20270101000000000", now=_T) == "20270101000000001",
    )


def check_cli() -> None:
    with tempfile.TemporaryDirectory() as td:
        pp = Path(td) / "pyproject.toml"
        pp.write_text('[project]\nname = "demo"\nversion = "0.18.0"\n')

        r = subprocess.run(
            ["uv", "run", "hyper", "release", "--pyproject", str(pp)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        check("dry run exits 0", r.returncode == 0, r.stderr[-200:])
        check("dry run prints a stamp", "release stamp: 2" in r.stdout, r.stdout)
        check(
            "dry run leaves pyproject untouched", 'version = "0.18.0"' in pp.read_text()
        )

        r = subprocess.run(
            ["uv", "run", "hyper", "release", "--apply", "--pyproject", str(pp)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        check("apply exits 0", r.returncode == 0, r.stderr[-200:])
        content = pp.read_text()
        applied = next(
            line for line in content.splitlines() if line.startswith("version")
        )
        stamp = applied.split('"')[1]
        check(
            "apply writes a valid canonical stamp",
            len(stamp) == RELEASE_STAMP_LENGTH and stamp.isdigit(),
            applied,
        )
        parse_release_stamp(stamp)  # raises on corruption
        check("rest of pyproject intact", 'name = "demo"' in content)

        r = subprocess.run(
            ["uv", "run", "hyper", "release", "--apply", "--pyproject", str(pp)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        stamp2 = pp.read_text().split('version = "')[1].split('"')[0]
        check(
            "second release through the CLI moves strictly forward",
            r.returncode == 0 and stamp2 > stamp,
            f"{stamp} -> {stamp2}",
        )


def main() -> bool:
    check_format_parse()
    check_mint_guard()
    check_cli()
    return finish()


if __name__ == "__main__":
    run_main(main)
