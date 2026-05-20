#!/usr/bin/env python3
"""CI watcher — small reusable wrapper around `gh` for monitoring this
repo's GitHub Actions runs.

Subcommands:
    latest              — print the latest run's id, status, conclusion, title
    watch [RUN_ID]      — stream "<job>: <conclusion>" lines as jobs complete;
                          exit when the run completes (RUN_ID defaults to latest)
    logs  [RUN_ID]      — save each failed job's log to logs/ci/<RUN_ID>/<job>.log
                          (also caches the FULL log for fast re-grepping)
    errors [RUN_ID]     — print the deduplicated error/traceback lines from
                          every failed job (the bits worth reading first)
    report [RUN_ID]     — one-shot: fetch + cache logs + summarise everything
                          worth knowing about a failed run (test totals,
                          connection-refused counts, pass/fail file split,
                          first error of every failed file). Use this after
                          every CI failure instead of inline grep pipelines.

`watch` emits one stdout line per terminal event, so it composes cleanly with
the Monitor tool. Default polling interval is 20s — pass `--interval N` to
override.

Examples:
    uv run python scripts/ci_watch.py watch
    uv run python scripts/ci_watch.py logs 24959711657
    uv run python scripts/ci_watch.py errors        # latest run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs" / "ci"

# Lines worth surfacing from failed-job logs. Add to this list rather than
# narrowing it — silence is more dangerous than noise.
ERROR_PATTERNS = re.compile(
    r"\b(error:|FAIL|Error|RuntimeError|Traceback|Failed|FAILED|Exception|"
    r"Connection|Refused|missing|not found|fatal|panic|HYPER\])",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class Job:
    name: str
    database_id: int
    status: str  # queued | in_progress | completed
    conclusion: str | None  # success | failure | cancelled | skipped | None


@dataclass(slots=True, frozen=True)
class Run:
    database_id: int
    status: str
    conclusion: str | None
    title: str
    jobs: list[Job]


def gh(*args: str, check: bool = True, retries: int = 3) -> str:
    """Run gh with simple retry. Transient TLS / API failures shouldn't kill
    a long-running watch — back off and try again."""
    last_err = ""
    for attempt in range(retries):
        r = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
        if r.returncode == 0:
            return r.stdout
        last_err = r.stderr
        time.sleep(2 * (attempt + 1))
    if check:
        sys.stderr.write(last_err)
        sys.exit(1)
    return ""


def latest_run_id() -> int:
    out = gh(
        "run",
        "list",
        "--limit",
        "1",
        "--json",
        "databaseId",
        "-q",
        ".[0].databaseId",
    )
    return int(out.strip())


def fetch_run(run_id: int) -> Run:
    raw = gh(
        "run",
        "view",
        str(run_id),
        "--json",
        "databaseId,status,conclusion,displayTitle,jobs",
    )
    data = json.loads(raw)
    return Run(
        database_id=data["databaseId"],
        status=data["status"],
        conclusion=data.get("conclusion"),
        title=data.get("displayTitle", ""),
        jobs=[
            Job(
                name=j["name"],
                database_id=j["databaseId"],
                status=j["status"],
                conclusion=j.get("conclusion"),
            )
            for j in data.get("jobs", [])
        ],
    )


@dataclass(slots=True, frozen=True)
class Annotation:
    job_name: str
    level: str  # "notice" | "warning" | "failure" | None (None = informational deprecation)
    title: str
    message: str
    path: str


def _repo_slug() -> str:
    """Return owner/repo for the current working tree."""
    return gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()


def fetch_annotations(run_id: int) -> list[Annotation]:
    """Fetch per-check-run annotations for every job in `run_id`.

    Annotations are run-level *banners* (deprecations, warnings, build
    metadata) — they don't appear in `gh run view --log` output but are
    visible in the GitHub UI and via the REST API. The deprecation
    notices for Node.js 20 actions (and similar runtime warnings) live
    here, not in the job logs. Returns one Annotation per (job, message).
    """
    slug = _repo_slug()
    raw = gh("api", f"repos/{slug}/actions/runs/{run_id}/jobs", "--paginate")
    jobs_data = json.loads(raw).get("jobs", [])
    out: list[Annotation] = []
    for j in jobs_data:
        check_run_id = j.get("check_run_url", "").rsplit("/", 1)[-1]
        if not check_run_id:
            continue
        try:
            ann_raw = gh(
                "api",
                f"repos/{slug}/check-runs/{check_run_id}/annotations",
                "--paginate",
                check=False,
            )
        except SystemExit:
            continue
        if not ann_raw.strip():
            continue
        for a in json.loads(ann_raw):
            out.append(
                Annotation(
                    job_name=j.get("name", "?"),
                    level=a.get("annotation_level") or "info",
                    title=a.get("title") or "",
                    message=a.get("message") or "",
                    path=a.get("path") or "",
                )
            )
    return out


def cmd_latest(_: argparse.Namespace) -> int:
    rid = latest_run_id()
    run = fetch_run(rid)
    print(f"{run.database_id}\t{run.status}\t{run.conclusion or '-'}\t{run.title}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Stream job completions to stdout until the run finishes.

    Always exits 0 on a successfully-completed watch — the CI conclusion
    (success / failure) is reported via the final RUN_COMPLETE line, not
    via this process's exit code. That way Monitor treats every completed
    watch as a successful watch, and the human / agent reads the
    conclusion from the event stream.
    """
    rid = args.run_id or latest_run_id()
    print(f"watching run {rid} (interval={args.interval}s)", file=sys.stderr)
    seen: set[str] = set()
    while True:
        run = fetch_run(rid)
        for job in run.jobs:
            if job.status == "completed" and job.name not in seen:
                print(f"{job.name}: {job.conclusion}", flush=True)
                seen.add(job.name)
        if run.status == "completed":
            print(f"RUN_COMPLETE: {run.conclusion}", flush=True)
            return 0
        time.sleep(args.interval)


def _failed_jobs(run: Run) -> Iterable[Job]:
    return (j for j in run.jobs if j.conclusion == "failure")


def _fetch_job_log(run_id: int, job: Job, *, full: bool = False) -> Path:
    """Fetch (or read from cache) one job's log file.

    Cached at logs/ci/<run_id>/<job>.log (failed-only) and
    logs/ci/<run_id>/<job>.full.log (every step).
    """
    out_dir = LOGS_DIR / str(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".full.log" if full else ".log"
    path = out_dir / f"{job.name}{suffix}"
    if not path.exists():
        flag = "--log" if full else "--log-failed"
        path.write_text(
            gh("run", "view", "--job", str(job.database_id), flag, check=False)
        )
    return path


def cmd_logs(args: argparse.Namespace) -> int:
    rid = args.run_id or latest_run_id()
    run = fetch_run(rid)
    failed = list(_failed_jobs(run))
    if not failed:
        print(
            f"no failed jobs in run {rid} (status={run.status} conclusion={run.conclusion})"
        )
        return 0
    paths: list[Path] = []
    for job in failed:
        paths.append(_fetch_job_log(rid, job, full=False))
        _fetch_job_log(rid, job, full=True)  # also cache the full log
    print(f"saved {len(paths)} failed-job log(s) under {LOGS_DIR / str(rid)}/")
    for p in paths:
        try:
            print(f"  {p.relative_to(Path.cwd())}")
        except ValueError:
            print(f"  {p}")
    return 0


def _strip_gh_prefix(line: str) -> str:
    """`gh run view --log` prepends "<job>\t<step>\t<timestamp> " to every
    line. Strip that so the underlying content is greppable."""
    parts = line.split("\t", 2)
    payload = parts[-1] if len(parts) == 3 else line
    # Drop the leading ISO timestamp + space too
    if len(payload) > 30 and payload[:4].isdigit() and payload[4] == "-":
        payload = payload.split(" ", 1)[-1] if " " in payload[:30] else payload
    return payload.strip()


def cmd_errors(args: argparse.Namespace) -> int:
    rid = args.run_id or latest_run_id()
    run = fetch_run(rid)
    failed = list(_failed_jobs(run))
    if not failed:
        print(
            f"no failed jobs in run {rid} (status={run.status} conclusion={run.conclusion})"
        )
        return 0
    seen: set[str] = set()
    for job in failed:
        print(f"\n=== {job.name} ===")
        log_path = _fetch_job_log(rid, job, full=False)
        for raw in log_path.read_text().splitlines():
            payload = _strip_gh_prefix(raw)
            if not ERROR_PATTERNS.search(payload) or payload in seen:
                continue
            seen.add(payload)
            print(f"  {payload}")
    return 0


# Patterns the report subcommand looks for in test_runner output. Each entry
# is (label, regex). Labels are shown in the summary in this order.
_REPORT_METRICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Tests passed/total",
        re.compile(r"Total:\s+(\d+/\d+\s+tests passed,\s+\d+\s+failed)"),
    ),
    ("Files passed/total", re.compile(r"Files:\s+(\d+/\d+\s+passed,\s+\d+\s+failed)")),
    ("Run wall time", re.compile(r"Time:\s+([\d.]+s)")),
    ("DBs created", re.compile(r"Created\s+(\d+/\d+\s+test databases\s+in\s+\d+ms)")),
)


def _artifact_dir(run_id: int) -> Path:
    return LOGS_DIR / str(run_id) / "artifact"


def _ensure_artifact(run_id: int, name: str) -> Path:
    """Download (idempotently) the named artifact into a per-run directory."""
    out_dir = _artifact_dir(run_id)
    if out_dir.exists() and any(out_dir.iterdir()):
        return out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    # gh run download <run_id> --name <name> --dir <dir>
    gh("run", "download", str(run_id), "--name", name, "--dir", str(out_dir))
    return out_dir


def cmd_artifact(args: argparse.Namespace) -> int:
    rid = args.run_id or latest_run_id()
    out = _ensure_artifact(rid, args.name)
    print(f"artifact '{args.name}' available at {out}/")
    return 0


def cmd_subproc(args: argparse.Namespace) -> int:
    """Print one per-subprocess log from the test_runner artifact.

    The hyper-test runner writes full stdout+stderr per script under
    logs/test_runs/subprocess/<name>.log inside the artifact. Useful
    when the run summary truncates the trace lines we'd actually like
    to read (debug.print output, repeated stack frames, etc.).
    """
    rid = args.run_id or latest_run_id()
    art_dir = _ensure_artifact(rid, args.name)
    candidates = list(art_dir.rglob(f"subprocess/{args.test_name}.log"))
    if not candidates:
        print(f"no subprocess log for {args.test_name!r} in artifact {args.name!r}")
        print(f"artifact tree: {art_dir}")
        for p in art_dir.rglob("*.log"):
            print(f"  {p.relative_to(art_dir)}")
        return 1
    for path in candidates:
        print(f"# === {path.relative_to(art_dir)} ===")
        print(path.read_text())
    return 0


_DIFF_BUCKETS: tuple[tuple[str, str], ...] = (
    ("Workflow", ".github/workflows/"),
    ("Build/Make", "Makefile"),
    ("CI tooling", "scripts/ci_watch.py"),
    ("Test runner", "hyperdjango/test_runner.py"),
    ("Doctor", "hyperdjango/doctor/"),
    ("CLI", "hyperdjango/cli.py"),
    ("DB extensions", "hyperdjango/db_extensions.py"),
    ("Database (Py)", "hyperdjango/database.py"),
    ("Zig core: connect", "zig/src/pg/stream.zig"),
    ("Zig core: db", "zig/src/db.zig"),
    ("Zig core: server", "zig/src/server.zig"),
    ("Zig core: file_watcher", "zig/src/file_watcher.zig"),
    ("Zig core: py FFI", "zig/src/py.zig"),
    ("Tests (pytest)", "tests/"),
    ("Tests (scripts)", "scripts/test_"),
)


def cmd_diff(args: argparse.Namespace) -> int:
    """Print a categorized git-diff summary or a single file's diff.

    Without `path`: prints a per-bucket stat overview so you can see
    which areas changed and audit them one bucket at a time.
    With `path`: prints the full diff for that path (one file or directory).
    """
    if args.path:
        out = subprocess.run(
            ["git", "diff", f"{args.from_ref}..{args.to_ref}", "--", args.path],
            capture_output=True,
            text=True,
            check=False,
        )
        sys.stdout.write(out.stdout)
        return 0

    print(f"# Diff summary  {args.from_ref}..{args.to_ref}\n")
    out = subprocess.run(
        ["git", "diff", f"{args.from_ref}..{args.to_ref}", "--name-status"],
        capture_output=True,
        text=True,
        check=False,
    )
    files = [line.split("\t", 1) for line in out.stdout.splitlines() if line.strip()]

    bucketed: dict[str, list[tuple[str, str]]] = {
        label: [] for label, _ in _DIFF_BUCKETS
    }
    bucketed["Other"] = []
    for status, path in files:
        for label, prefix in _DIFF_BUCKETS:
            if path.startswith(prefix):
                bucketed[label].append((status, path))
                break
        else:
            bucketed["Other"].append((status, path))

    for label, _ in (*_DIFF_BUCKETS, ("Other", "")):
        entries = bucketed.get(label, [])
        if not entries:
            continue
        print(f"\n## {label}  ({len(entries)} files)")
        for status, path in entries:
            print(f"  {status}  {path}")
        print(f"  → uv run python scripts/ci_watch.py diff {entries[0][1]}")
    return 0


def _print_annotations(rid: int) -> None:
    anns = fetch_annotations(rid)
    if not anns:
        return
    # Group by (level, message) and list which jobs hit it. Most run-level
    # banners (deprecation notices, runner image warnings) repeat the same
    # message across every job — collapse them so the report stays compact.
    grouped: dict[tuple[str, str], list[str]] = {}
    for a in anns:
        grouped.setdefault((a.level, a.message), []).append(a.job_name)
    print(f"\n## Annotations  ({len(anns)} total, {len(grouped)} unique)")
    for (level, msg), jobs in grouped.items():
        # Truncate noisy URL trailers but keep the actionable head.
        head = msg.split("https://", 1)[0].rstrip(" .")
        if len(head) > 240:
            head = head[:237] + "…"
        joblist = ", ".join(sorted(set(jobs)))
        print(f"  [{level}] ({joblist})")
        print(f"    {head}")


def cmd_annotations(args: argparse.Namespace) -> int:
    """Print every annotation attached to a run's jobs.

    Annotations are GitHub-side banners — deprecation notices,
    image-update warnings, build metadata. They are visible in the
    Actions UI but NOT in `gh run view --log` output, so the rest
    of this watcher's commands miss them. Surface them here so a
    full audit covers both log content and run-level notices.
    """
    rid = args.run_id or latest_run_id()
    run = fetch_run(rid)
    print(f"# Run {rid}: {run.title}")
    print(f"  conclusion={run.conclusion}")
    _print_annotations(rid)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """One-shot human-readable summary of a (typically failed) CI run.

    Pulls every failed job's log, runs every check we'd otherwise do
    via inline grep pipelines, and prints a structured report. Use this
    after any CI failure instead of cobbling together one-off greps.
    """
    rid = args.run_id or latest_run_id()
    run = fetch_run(rid)

    print(f"# Run {rid}: {run.title}")
    print(f"  status={run.status} conclusion={run.conclusion}")
    print()
    print("## Jobs")
    for job in run.jobs:
        marker = (
            "✓"
            if job.conclusion == "success"
            else "✗"
            if job.conclusion == "failure"
            else "·"
        )
        print(f"  {marker} {job.name}: {job.conclusion or job.status}")

    # Run-level annotations (deprecations, image warnings) come BEFORE the
    # log-based metrics so a clean run with deprecation banners doesn't look
    # like nothing happened.
    _print_annotations(rid)

    failed = list(_failed_jobs(run))
    if not failed:
        return 0

    for job in failed:
        full = _fetch_job_log(rid, job, full=True).read_text()
        failed_only = _fetch_job_log(rid, job, full=False).read_text()

        print(f"\n## {job.name}: metrics from full log")
        for label, pattern in _REPORT_METRICS:
            m = pattern.search(full)
            print(f"  {label}: {m.group(1) if m else '(not found)'}")

        # Connection / FATAL counts — common test-suite-vs-postgres signals
        signals: list[tuple[str, int]] = [
            ("ConnectionRefused", len(re.findall(r"ConnectionRefused", full))),
            ("FATAL", len(re.findall(r"\bFATAL\b", full))),
            ("too many clients", len(re.findall(r"too many clients", full))),
            ("RuntimeError", len(re.findall(r"RuntimeError", full))),
        ]
        nonzero = [(k, v) for k, v in signals if v > 0]
        if nonzero:
            print(f"\n## {job.name}: signal counts")
            for k, v in nonzero:
                print(f"  {k}: {v}")

        # File-level pass/fail (from the test_runner's per-file lines)
        passes = re.findall(r"  pass  ([a-z_0-9]+)", full)
        fails = re.findall(r"  FAIL  ([a-z_0-9]+)", full)
        if passes or fails:
            print(
                f"\n## {job.name}: per-file split  passes={len(passes)}  fails={len(fails)}"
            )
            if fails:
                print("  failed files:")
                for name in sorted(set(fails))[:30]:
                    print(f"    - {name}")
                if len(set(fails)) > 30:
                    print(f"    ... and {len(set(fails)) - 30} more")

        # Top deduplicated error lines from --log-failed
        seen: set[str] = set()
        errors: list[str] = []
        for raw in failed_only.splitlines():
            payload = _strip_gh_prefix(raw)
            if not ERROR_PATTERNS.search(payload) or payload in seen:
                continue
            seen.add(payload)
            errors.append(payload)
            if len(errors) >= 20:
                break
        if errors:
            print(f"\n## {job.name}: top {len(errors)} unique error lines")
            for e in errors:
                print(f"  {e}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("latest").set_defaults(fn=cmd_latest)

    pw = sub.add_parser("watch")
    pw.add_argument("run_id", nargs="?", type=int)
    pw.add_argument("--interval", type=float, default=20.0)
    pw.set_defaults(fn=cmd_watch)

    pl = sub.add_parser("logs")
    pl.add_argument("run_id", nargs="?", type=int)
    pl.set_defaults(fn=cmd_logs)

    pe = sub.add_parser("errors")
    pe.add_argument("run_id", nargs="?", type=int)
    pe.set_defaults(fn=cmd_errors)

    pr = sub.add_parser("report")
    pr.add_argument("run_id", nargs="?", type=int)
    pr.set_defaults(fn=cmd_report)

    pann = sub.add_parser(
        "annotations",
        help="Print run-level GitHub annotations (deprecations, runner warnings, etc).",
    )
    pann.add_argument("run_id", nargs="?", type=int)
    pann.set_defaults(fn=cmd_annotations)

    pa = sub.add_parser(
        "artifact",
        help="Download an artifact (default: test-logs-linux) into logs/ci/<run>/artifact/",
    )
    pa.add_argument("run_id", nargs="?", type=int)
    pa.add_argument("--name", default="test-logs-linux")
    pa.set_defaults(fn=cmd_artifact)

    psub = sub.add_parser(
        "subproc",
        help="Print the per-subprocess log for one failing test (downloads artifact if needed)",
    )
    psub.add_argument("test_name", help="Test file stem, e.g. connection_timeout")
    psub.add_argument("run_id", nargs="?", type=int)
    psub.add_argument("--name", default="test-logs-linux", help="Artifact name")
    psub.set_defaults(fn=cmd_subproc)

    pd = sub.add_parser(
        "diff",
        help="Show a per-file slice of git diff between two refs. "
        "Categorizes by directory so you can audit big changesets one bucket "
        "at a time without manually piping git diff.",
    )
    pd.add_argument(
        "--from-ref", default="3f3b7f4", help="Base ref (default = root commit)"
    )
    pd.add_argument("--to-ref", default="HEAD", help="Tip ref (default = HEAD)")
    pd.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Optional file/dir to limit the diff to. Without it, prints a "
        "categorized summary by directory bucket.",
    )
    pd.set_defaults(fn=cmd_diff)

    args = p.parse_args()
    if not hasattr(args, "fn"):
        # No subcommand → default to "watch" of the latest run
        args.run_id = None
        args.interval = 20.0
        args.fn = cmd_watch
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
