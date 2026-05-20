"""
Phase B of task #156: py-spy flame graph during sustained wrk load on List endpoint.

Starts bookstore_api server, attaches py-spy, drives /api/v1/books/ with wrk
for the specified duration, writes speedscope JSON + SVG.

Outputs:
  logs/profile_list_flame.svg         — SVG flame graph (open in browser)
  logs/profile_list_speedscope.json   — speedscope.app compatible profile
  logs/profile_list_wrk.txt           — wrk output for correlation

Run: uv run python scripts/profile_list_endpoint.py
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from e2e_helper import TEST_PORTS, AppRunner, http_get

LOGS = Path(__file__).resolve().parent.parent / "logs"
PORT = TEST_PORTS["load_orm"]
HOST = "127.0.0.1"
DURATION = 20  # seconds


def find_server_pid() -> int | None:
    """Find the server process PID listening on PORT."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{PORT}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split("\n") if p]
        return pids[0] if pids else None
    except subprocess.SubprocessError, ValueError:
        return None


def main():
    LOGS.mkdir(parents=True, exist_ok=True)

    print(f"=== Phase B: py-spy flame graph on List endpoint ({DURATION}s) ===")

    # Preconditions
    if not shutil.which("wrk"):
        print("ERROR: wrk not installed")
        sys.exit(1)
    if not shutil.which("py-spy"):
        print("ERROR: py-spy not installed (uv add --dev py-spy)")
        sys.exit(1)

    # Setup DB
    print("Setting up database...")
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.bookstore_api.app:app",
            "--drop",
            "--seed",
            "services.bookstore_api.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    # Start server
    print(f"Starting server on port {PORT}...")
    with AppRunner(
        "services.bookstore_api.app:app",
        host=HOST,
        port=PORT,
        env={"HYPER_LOAD_TEST": "1"},
    ) as runner:
        base = runner.url()

        # Warmup
        print("Warming up (30 requests)...")
        for _ in range(30):
            http_get(f"{base}/api/v1/books/")

        pid = find_server_pid()
        if pid is None:
            print("ERROR: could not find server PID")
            sys.exit(1)
        print(f"Server PID: {pid}")

        svg_path = LOGS / "profile_list_flame.svg"
        speedscope_path = LOGS / "profile_list_speedscope.json"

        # Start py-spy record (speedscope format) in background
        # NOTE: py-spy on macOS usually requires sudo to attach to other
        # processes. We run without sudo first — if it fails, the user must
        # re-run with sudo.
        use_sudo = os.environ.get("PYSPY_SUDO", "0") == "1"
        # Note: --native not supported on macOS; Python-level stacks only
        pyspy_cmd = (["sudo"] if use_sudo else []) + [
            "py-spy",
            "record",
            "--pid",
            str(pid),
            "--output",
            str(speedscope_path),
            "--format",
            "speedscope",
            "--duration",
            str(DURATION),
            "--rate",
            "250",
            "--subprocesses",
        ]
        print(f"Starting py-spy record (duration={DURATION}s, format=speedscope)...")
        print(f"  cmd: {' '.join(pyspy_cmd)}")
        pyspy = subprocess.Popen(
            pyspy_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Give py-spy a moment to attach
        time.sleep(1.0)

        # Run wrk against the List endpoint for DURATION seconds
        print(f"Running wrk for {DURATION}s on /api/v1/books/ ...")
        wrk_result = subprocess.run(
            [
                "wrk",
                "-t16",
                "-c200",
                f"-d{DURATION}s",
                f"{base}/api/v1/books/",
            ],
            capture_output=True,
            text=True,
            timeout=DURATION + 30,
        )
        wrk_out = wrk_result.stdout + wrk_result.stderr
        (LOGS / "profile_list_wrk.txt").write_text(wrk_out)

        print("\n--- wrk output ---")
        for line in wrk_out.strip().split("\n"):
            if line.strip():
                print(f"  {line.strip()}")

        # Wait for py-spy to finish
        try:
            pyspy_stdout, pyspy_stderr = pyspy.communicate(timeout=DURATION + 30)
            if pyspy.returncode == 0:
                print(f"\npy-spy output: {speedscope_path}")
                # Also generate SVG from speedscope data
                svg_cmd = (["sudo"] if use_sudo else []) + [
                    "py-spy",
                    "record",
                    "--pid",
                    str(pid),
                    "--output",
                    str(svg_path),
                    "--format",
                    "flamegraph",
                    "--duration",
                    "5",
                    "--rate",
                    "250",
                ]
                svg_proc = subprocess.run(
                    svg_cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if svg_proc.returncode == 0:
                    print(f"SVG flame graph: {svg_path}")
            else:
                print(
                    f"py-spy failed (exit {pyspy.returncode}): {pyspy_stderr.decode()[:500]}"
                )
        except subprocess.TimeoutExpired:
            pyspy.kill()
            print("py-spy timed out")

    print("\n=== Phase B complete ===")


if __name__ == "__main__":
    main()
