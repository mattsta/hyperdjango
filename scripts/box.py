#!/usr/bin/env python3
"""Single entry point for ALL remote benchmark-box operations.

Every operation that touches the box goes through this CLI — never ad-hoc
ssh command strings. Each subcommand encapsulates the full, verified
procedure with the failure modes that ad-hoc invocation kept hitting:

- ``sync``    rsyncs ONLY git-tracked files (plus the ignored toolchains the
              box needs) — untracked local scratch files can never spray onto
              the box again (a sprayed ``test_*.py`` without a marker aborts
              the remote suite instantly).
- ``run``     uploads a job script, launches it in a DETACHED TMUX SESSION,
              VERIFIES the launch, then waits with a BOUNDED dual-marker
              wait — success and failure markers both end the wait; a silent
              job times out loudly instead of stalling forever.
- ``test``    remote build + full suite as a marker-wrapped job.
- ``wait``    re-attach the bounded marker wait to an ALREADY-RUNNING job by
              name — for jobs launched in a prior session whose original
              waiter is gone.
- ``status``  which tmux job sessions are live + the latest job logs.
- ``stop``    kill-session teardown, verified, with a TERM->KILL backstop.
- ``clean``   removes remote files that are not tracked locally (scripts/).

Jobs are tmux-managed. Each job owns one detached session named
``boxjob_<name>`` running ``bash <script> 2>&1 | tee /tmp/boxjob_<name>.log``:

- ``tee`` (not ``pipe-pane``) is what writes the transcript, because it is
  attached to the pipeline BEFORE the first byte is produced. ``pipe-pane`` is
  wired up after the pane already exists, so a job that prints and dies inside
  that window loses exactly the output that explains it — unacceptable for a
  log the marker waiter treats as the source of truth.
- the session stays attachable for the job's lifetime, so a human can watch a
  3-hour bench live (``ssh -t <box> tmux attach -t boxjob_<name>``, printed at
  launch) without disturbing the waiter, which only ever reads the log file.
- when the job ends tmux tears the session down; the full transcript remains
  in the log, and a marker wait re-attached afterwards is satisfied
  immediately by the marker already sitting in it.

Usage:
    uv run python scripts/box.py sync
    uv run python scripts/box.py test                 # build + full suite
    uv run python scripts/box.py run myjob.sh         # arbitrary job script
    uv run python scripts/box.py status
    uv run python scripts/box.py clean

Config via env: HYPER_BOX (required, e.g. user@host),
HYPER_BOX_DIR (default ~/hyperdjango).
"""

from __future__ import annotations

import argparse

# env-boundary: remote-ops tooling configuration, not framework runtime config.
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BOX = os.environ.get("HYPER_BOX", "")
BOX_DIR = os.environ.get("HYPER_BOX_DIR", "~/hyperdjango")
REPO = Path(__file__).resolve().parent.parent

_SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", BOX]

# Markers every job script is wrapped with — the bounded waiter keys on these.
MARK_OK = "HYPER-BOX-JOB-COMPLETE"
MARK_FAIL = "HYPER-BOX-JOB-FAILED"

# One tmux session per job. The prefix is also what the teardown sweep and the
# status listing match on, so it is spelled once here.
SESSION_PREFIX = "boxjob_"
# Job names become a tmux session name, a /tmp script path and a /tmp log path.
# Anything outside this set would have to be quoted into three different remote
# contexts — reject it at the door instead.
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(slots=True)
class SshResult:
    code: int
    out: str


def ssh(cmd: str, timeout: int = 60) -> SshResult:
    """One remote command. `cmd` is a single string executed by the remote
    shell with the box PATH already exported — callers never hand-build
    environment or quoting. A local timeout resolves to exit 124 (the same
    convention as remote `timeout`) instead of an unhandled TimeoutExpired —
    bounded waits must end in a reported verdict, never a stack trace."""
    full = f'export PATH="$HOME/.local/bin:$PATH"; cd {BOX_DIR} 2>/dev/null; {cmd}'
    try:
        r = subprocess.run(
            [*_SSH, full], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return SshResult(124, "(local ssh timeout)")
    return SshResult(r.returncode, (r.stdout + r.stderr).strip())


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=REPO, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def cmd_sync(_args) -> int:
    """rsync ONLY git-tracked files to the box (plus nothing else)."""
    files = tracked_files()
    listing = "\n".join(files) + "\n"
    p = subprocess.run(
        [
            "rsync",
            "-a",
            "--files-from=-",
            str(REPO) + "/",
            f"{BOX}:{BOX_DIR}/",
        ],
        input=listing,
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        print(p.stderr.strip() or "rsync failed", file=sys.stderr)
        return 1
    # Make the mirror's own git index agree with what was just shipped.
    # discover_tests (and check_test_markers) treat an untracked
    # scripts/test_*.py as local scratch and SKIP it — correct on a dev
    # machine, wrong on this mirror, where every file arrived by rsync and the
    # box's index still reflects whatever it was cloned at. Without this a box
    # run silently covers fewer files than the suite actually has (observed:
    # 451 of 464, with 13 newly-added test files skipped), so a green box run
    # would mean less than it appeared to. --intent-to-add registers the paths
    # without staging content; nothing is ever committed from the mirror.
    ssh("git add -A --intent-to-add . 2>/dev/null; true", timeout=60)
    print(f"synced {len(files)} tracked files -> {BOX}:{BOX_DIR}")
    return 0


def cmd_clean(_args) -> int:
    """Remove remote scripts/test_*.py that are not tracked locally — the
    stray-file class that aborts the remote suite at the marker gate."""
    tracked = {f for f in tracked_files() if f.startswith("scripts/test_")}
    r = ssh("ls scripts/test_*.py 2>/dev/null")
    remote = {line.strip() for line in r.out.splitlines() if line.strip()}
    stray = sorted(f for f in remote if f and f not in tracked)
    if not stray:
        print("no stray remote test files")
        return 0
    ssh("rm -f " + " ".join(stray), timeout=30)
    print(f"removed {len(stray)} stray remote test files: {', '.join(stray)}")
    return 0


def _wrap_job(body: str) -> str:
    """Wrap a job body so it ALWAYS emits a terminal marker."""
    return (
        "#!/bin/bash\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        f"cd {BOX_DIR} || {{ echo {MARK_FAIL}; exit 1; }}\n"
        f"if (\nset -e\n{body}\n); then echo {MARK_OK}; else echo {MARK_FAIL}; fi\n"
    )


@dataclass(frozen=True, slots=True)
class Job:
    """The three remote names a job owns, derived from one validated name."""

    name: str
    session: str
    script: str
    log: str

    @property
    def attach_cmd(self) -> str:
        """The command a human runs to watch this job live. Read-only for the
        job: attaching shares the pane, it never interferes with the waiter
        (which reads the log file, not the terminal)."""
        return f"ssh -t {BOX} tmux attach -t {self.session}"


def job(name: str) -> Job:
    if not _NAME_RE.fullmatch(name):
        raise SystemExit(
            f"invalid job name {name!r} — use only letters, digits, '.', '_', '-' "
            "(the name becomes a tmux session and two /tmp paths)"
        )
    session = f"{SESSION_PREFIX}{name}"
    return Job(name, session, f"/tmp/{session}.sh", f"/tmp/{session}.log")


def require_tmux() -> bool:
    """tmux is the job supervisor — there is no fallback. A silent drop back to
    `nohup` would take the session away (nothing to attach to) while still
    looking like it worked, which is exactly the failure this tooling exists to
    prevent. Fail loudly with the install line instead."""
    r = ssh("command -v tmux >/dev/null 2>&1 && echo HAVE_TMUX || true", timeout=30)
    if "HAVE_TMUX" in r.out:
        return True
    print(
        f"tmux is not installed on {BOX} — box jobs are tmux-managed and there is "
        "no fallback. Install it on the box:\n"
        "    sudo apt-get install -y tmux     # debian/ubuntu\n"
        "    sudo dnf install -y tmux         # fedora/rhel",
        file=sys.stderr,
    )
    return False


def _job_state(j: Job) -> tuple[bool, bool]:
    """(session live, terminal marker already in the log) in ONE round trip.

    Together these are the whole job state machine: live means the tmux session
    still owns a running command; marked means the wrapper already wrote a
    terminal marker. Neither means there is nothing to wait for."""
    r = ssh(
        f"tmux has-session -t {j.session} 2>/dev/null && echo LIVE; "
        f"grep -qaE '{MARK_OK}|{MARK_FAIL}' {j.log} 2>/dev/null && echo MARKED; true",
        timeout=30,
    )
    return "LIVE" in r.out, "MARKED" in r.out


def wait_job(name: str, wait_minutes: int, announce_attach: bool = True) -> int:
    """EVENT-DRIVEN bounded wait on a job's terminal markers — no polling,
    no sleeps. One blocking remote command is the state machine: `tail -F`
    streams the log and `grep -q` EXITS the instant a terminal marker line
    arrives; the remote `timeout` bounds a job that dies without writing
    anything. A dead-at-birth launch is covered too: the wrapper's cd-guard
    writes the FAIL marker, and an entirely absent log makes tail -F wait
    until timeout, which reports. Re-attachable: markers already present in
    the log satisfy the grep immediately (tail -n +1 replays from the top)."""
    j = job(name)
    live, marked = _job_state(j)
    if not live and not marked:
        # Waiting the full timeout on a job that is neither running nor
        # finished is a wait that can only ever report a timeout — say so now.
        print(
            f"job {name}: NOT RUNNING — no tmux session {j.session} and no "
            f"terminal marker in {j.log}; nothing to wait for. Log tail:"
        )
        print(ssh(f"tail -25 {j.log} 2>/dev/null").out or f"(no log at {j.log})")
        return 2
    print(f"job {name}: waiting on markers (log {j.log})")
    if live and announce_attach:
        print(f"job {name}: watch it live ->  {j.attach_cmd}")
    wait_cmd = (
        f"timeout {wait_minutes * 60} "
        f"grep -qaE '{MARK_OK}|{MARK_FAIL}' <(tail -n +1 -F {j.log} 2>/dev/null)"
    )
    w = ssh(wait_cmd, timeout=wait_minutes * 60 + 60)
    tail = ssh(f"tail -25 {j.log} 2>/dev/null").out
    if MARK_OK in tail:
        print(f"job {name}: COMPLETE — log tail:")
        print(tail)
        return 0
    if MARK_FAIL in tail:
        print(f"job {name}: FAILED — log tail:")
        print(tail)
        return 1
    print(f"job {name}: TIMED OUT after {wait_minutes} min (exit {w.code}) — log tail:")
    print(tail)
    return 2


def run_job(body: str, name: str, wait_minutes: int) -> int:
    """Upload a wrapped job, launch it in a detached tmux session, VERIFY the
    launch, then bounded-wait on the terminal markers.

    `tee` truncates the log at launch, so a re-run of the same job name can
    never be satisfied by the PREVIOUS run's markers."""
    if not require_tmux():
        return 2
    j = job(name)
    live, _ = _job_state(j)
    if live:
        print(
            f"job {name}: tmux session {j.session} is ALREADY RUNNING — refusing "
            f"to launch a second one.\n"
            f"    watch it:  {j.attach_cmd}\n"
            f"    wait:      uv run python scripts/box.py wait {name}\n"
            f"    kill it:   uv run python scripts/box.py stop {name}",
            file=sys.stderr,
        )
        return 2
    subprocess.run(
        [*_SSH, f"cat > {j.script}"], input=_wrap_job(body), text=True, check=True
    )
    ssh(
        f"tmux new-session -d -s {j.session} 'bash {j.script} 2>&1 | tee {j.log}'",
        timeout=30,
    )
    # VERIFY: either the session is alive, or the job was short enough to have
    # already finished and left a marker. Neither means the launch failed.
    live, marked = _job_state(j)
    if not live and not marked:
        print(f"job {name}: LAUNCH FAILED — no tmux session and no marker. Log tail:")
        print(ssh(f"tail -25 {j.log} 2>/dev/null").out or f"(no log at {j.log})")
        return 2
    print(f"job {name}: launched in tmux session {j.session}")
    print(f"job {name}: watch it live ->  {j.attach_cmd}")
    return wait_job(name, wait_minutes, announce_attach=False)


def cmd_run(args) -> int:
    body = Path(args.script).read_text()
    return run_job(body, Path(args.script).stem, args.wait_minutes)


def cmd_test(args) -> int:
    body = (
        "uv run hyper-build --release\n"
        "uv run hyper-test\n"
        "LAST=$(ls -t logs/test_runs/*_all.log | head -1)\n"
        'grep -E "Total:|Files:|Uncounted" "$LAST" | tail -3\n'
    )
    rc = cmd_sync(args)
    if rc != 0:
        return rc
    rc = cmd_clean(args)
    if rc != 0:
        return rc
    return run_job(body, "fullsuite", args.wait_minutes)


def cmd_wait(args) -> int:
    return wait_job(args.name, args.wait_minutes)


def cmd_fetch(args) -> int:
    """Pull a remote path (relative to the box repo) down via rsync — the
    sanctioned way to copy benchmark results/artifacts off the box (they are
    per-run repo-local files and are never committed)."""
    dest = Path(args.local)
    (dest if args.local.endswith("/") else dest.parent).mkdir(
        parents=True, exist_ok=True
    )
    p = subprocess.run(
        ["rsync", "-a", f"{BOX}:{BOX_DIR}/{args.remote}", args.local],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        print(p.stderr.strip() or "rsync failed", file=sys.stderr)
        return 1
    print(f"fetched {BOX}:{BOX_DIR}/{args.remote} -> {args.local}")
    return 0


def cmd_push(args) -> int:
    """Push a single local file/dir to the box via rsync — for shipping a
    targeted change without a full sync (which mirrors the WHOLE tracked
    working tree, including any in-flight uncommitted edits)."""
    p = subprocess.run(
        ["rsync", "-a", args.local, f"{BOX}:{BOX_DIR}/{args.remote}"],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        print(p.stderr.strip() or "rsync failed", file=sys.stderr)
        return 1
    print(f"pushed {args.local} -> {BOX}:{BOX_DIR}/{args.remote}")
    return 0


def cmd_tail(args) -> int:
    """Non-blocking peek at a job's log — progress check without a wait."""
    j = job(args.name)
    r = ssh(f"tail -{args.lines} {j.log} 2>/dev/null")
    print(r.out or f"(no log for job {args.name} at {j.log})")
    return 0


def cmd_stop(args) -> int:
    """Stop a running job and every process it spawned.

    `tmux kill-session` is the PRIMARY mechanism: the session owns the job's
    whole process tree, so killing it takes the tree with it. The TERM->KILL
    sweep that follows is the backstop for anything that escaped the session
    (a daemonised child, a bench a previous tooling generation started).
    Always verifies and reports survivors — a stop that silently fails is how
    a rogue 3-hour bench keeps burning a box."""
    j = job(args.name)
    ssh(
        f"tmux kill-session -t {j.session} 2>/dev/null; "
        f"for pid in $(pgrep -f '[b]oxjob_{args.name}'); do "
        "kill -TERM -- -$(ps -o pgid= -p $pid | tr -d ' ') 2>/dev/null "
        "|| kill -TERM $pid 2>/dev/null; done; "
        "pkill -TERM -f '[h]yper-bench' 2>/dev/null; "
        "pkill -TERM -f '[w]rk http' 2>/dev/null; true",
        timeout=30,
    )
    # TERM first (graceful drain), then KILL whatever ignored it. A stop verb
    # that loses to a signal handler is not a stop verb.
    ssh(
        f"pkill -KILL -f '[b]oxjob_{args.name}' 2>/dev/null; "
        "pkill -KILL -f '[h]yper-bench' 2>/dev/null; "
        "pkill -KILL -f '[w]rk http' 2>/dev/null; true",
        timeout=30,
    )
    # Verification is TWO round trips on purpose. The `[b]oxjob_` bracket trick
    # only stops the sweep from matching itself; a command that also carries the
    # session name (`boxjob_<name>`) matches its OWN shell's command line and
    # reports a survivor that is the check. So the session check runs alone.
    survived, _ = _job_state(j)
    check = ssh(
        "pgrep -af '[b]oxjob_|[h]yper-bench|[w]rk http' || echo '(all stopped)'",
        timeout=30,
    )
    if survived:
        print(f"tmux session {j.session} SURVIVED the kill")
    print(check.out)
    return 0 if "(all stopped)" in check.out and not survived else 1


def cmd_status(_args) -> int:
    """Live tmux job sessions (the authoritative liveness signal), any stray
    bench processes outside a session, and the newest job logs."""
    # The `||` must see GREP's status, so the fallback is inside the group and
    # the `sed` prefix is applied after it — a trailing `|| echo` on the whole
    # pipeline would read sed's always-zero status and never fire.
    r = ssh(
        "uptime | sed 's/^/load: /';"
        f"{{ tmux ls 2>/dev/null | grep '^{SESSION_PREFIX}' "
        "|| echo '(no tmux job sessions)'; } | sed 's/^/job: /';"
        "pgrep -af '[h]yper-bench|[w]rk http|[t]est_runner' | head -5;"
        "ls -t /tmp/boxjob_*.log 2>/dev/null | head -3"
    )
    print(r.out or "(idle, no job logs)")
    return 0


def main() -> int:
    if not BOX:
        print(
            "HYPER_BOX is not set. Export the box's ssh target first, e.g.\n"
            "    export HYPER_BOX=user@host\n"
            "    export HYPER_BOX_DIR='~/hyperdjango'   # optional, this is the default",
            file=sys.stderr,
        )
        return 2
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    sub.add_parser("clean")
    sub.add_parser("status")
    p_run = sub.add_parser("run")
    p_run.add_argument("script")
    p_run.add_argument("--wait-minutes", type=int, default=30)
    p_test = sub.add_parser("test")
    p_test.add_argument("--wait-minutes", type=int, default=30)
    p_wait = sub.add_parser("wait")
    p_wait.add_argument("name", help="job name (boxjob_<name>.log)")
    p_wait.add_argument("--wait-minutes", type=int, default=30)
    p_tail = sub.add_parser("tail")
    p_tail.add_argument("name", help="job name (boxjob_<name>.log)")
    p_tail.add_argument("--lines", type=int, default=30)
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("name", help="job name (boxjob_<name>.sh)")
    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("remote", help="path relative to the box repo dir")
    p_fetch.add_argument("local", help="local destination path")
    p_push = sub.add_parser("push")
    p_push.add_argument("local", help="local file/dir to push")
    p_push.add_argument("remote", help="destination path relative to the box repo dir")
    args = ap.parse_args()
    return {
        "sync": cmd_sync,
        "clean": cmd_clean,
        "status": cmd_status,
        "run": cmd_run,
        "test": cmd_test,
        "wait": cmd_wait,
        "tail": cmd_tail,
        "fetch": cmd_fetch,
        "push": cmd_push,
        "stop": cmd_stop,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
