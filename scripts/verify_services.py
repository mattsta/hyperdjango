#!/usr/bin/env python3
"""Verify all service apps start and serve correctly."""

import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: {detail}")


def test_service(name, app_path, port, test_routes):
    """Start a service app and test its routes."""
    print(f"\n=== Service: {name} ===")

    if not Path(app_path).exists():
        check(f"{name} exists", False, f"file not found: {app_path}")
        return

    check(f"{name} file exists", True)

    # Start server
    proc = subprocess.Popen(
        [sys.executable, app_path, str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(app_path).parent) or ".",
    )

    # Wait for startup
    started = False
    first_route = test_routes[0][0] if test_routes else "/health"
    for _ in range(30):
        time.sleep(0.1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{first_route}", timeout=1)
            started = True
            break
        except urllib.error.URLError, ConnectionRefusedError, OSError:
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode()[:300] if proc.stderr else ""
                check(f"{name} starts", False, f"died: {stderr}")
                return
        except urllib.error.HTTPError:
            started = True
            break

    if not started:
        proc.terminate()
        check(f"{name} starts", False, "timeout")
        return

    check(f"{name} starts", True)

    # Test routes
    for path, expected_status, body_check in test_routes:
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3)
            body = resp.read()
            check(f"GET {path} → {resp.status}", resp.status == expected_status)
            if body_check:
                if isinstance(body_check, bytes):
                    check(
                        f"  body contains {body_check!r}",
                        body_check in body,
                        body[:100],
                    )
                elif isinstance(body_check, str):
                    check(
                        f"  body contains '{body_check}'",
                        body_check.encode() in body,
                        body[:100],
                    )
        except urllib.error.HTTPError as e:
            check(f"GET {path} → {e.code}", e.code == expected_status)
        except Exception as e:
            check(f"GET {path}", False, str(e))

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


# Test each service
base = Path(__file__).resolve().parent.parent

test_service(
    "hello",
    str(base / "services" / "hello" / "app.py"),
    19890,
    [
        ("/", 200, "Hello"),
        ("/greet/world", 200, "world"),
    ],
)

test_service(
    "benchmark_app",
    str(base / "services" / "benchmark_app" / "app.py"),
    19891,
    [
        ("/json", 200, "Hello"),
        ("/plaintext", 200, "Hello"),
        ("/health", 200, "ok"),
    ],
)

print(f"\n{'=' * 50}")
print(f"  {PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("  All services verified!")
else:
    print(f"  {FAIL} checks need attention")
    sys.exit(1)
