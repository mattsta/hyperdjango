#!/usr/bin/env python3
"""Test native file watcher and hot reload infrastructure."""

# hyper-test: unit

import asyncio
import importlib
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from hyperdjango import _hyperdjango_native as native
from hyperdjango.hot_reload import HotReloader
from hyperdjango.testkit import check, finish, run_main


# ── Test 1: Native file watcher API ───────────────────────────────────────────
def test_native_file_watcher() -> None:
    # Create a temp directory to watch
    with tempfile.TemporaryDirectory() as tmpdir:
        change_detected = threading.Event()
        change_count = [0]

        def on_change():
            change_count[0] += 1
            change_detected.set()

        # Start native watcher
        handle = native._file_watcher_start(
            [tmpdir],
            [".py", ".html"],
            on_change,
        )
        print(f"  native watcher started (handle={handle})")

        # Give the watcher thread time to set up kqueue
        time.sleep(0.2)

        # Create a file — should trigger change
        test_file = Path(tmpdir) / "test.py"
        with test_file.open("w") as f:
            f.write("# test\n")

        # Wait for notification (kqueue monitors directory-level changes)
        if change_detected.wait(timeout=3.0):
            print(f"  file creation detected (count={change_count[0]})")
        else:
            print(
                "⚠ Native watcher: file creation not detected within timeout (kqueue may need ATTRIB)"
            )

        # Modify the file — kqueue watches per-file fds, so content changes trigger NOTE.WRITE
        change_detected.clear()
        time.sleep(0.2)
        with test_file.open("w") as f:
            f.write("# modified\n")

        detected = change_detected.wait(timeout=3.0)
        assert detected, "Native watcher MUST detect file modification"
        print(f"  file modification detected (count={change_count[0]})")

        # Stop watcher
        native._file_watcher_stop(handle)


# ── Test 2: HotReloader class ─────────────────────────────────────────────────
def test_sse_generator() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        reloader = HotReloader(watch_dirs=[tmpdir])
        assert reloader._running == False

        # Test the async SSE generator: emits raw tokens (Response.sse frames them)
        # and wakes on a change WITHOUT parking a serving thread.
        async def _check_sse():
            reloader._running = True
            gen = reloader.sse_generator()
            first = await gen.__anext__()
            assert first == "connected", first
            # Simulate a change: the client is woken on this loop via
            # call_soon_threadsafe, so the next token is "reload".
            reloader._on_change()
            nxt = await gen.__anext__()
            assert nxt == "reload", nxt
            reloader._running = False
            await gen.aclose()

        asyncio.run(_check_sse())


# ── Test 3: Script injection ──────────────────────────────────────────────────
def test_script_injection() -> None:
    reloader = HotReloader()
    html = "<html><body><h1>Hello</h1></body></html>"
    injected = reloader.inject_script(html)
    assert "/__hyper_reload" in injected
    assert "EventSource" in injected
    assert "</body>" in injected

    # No body tag — should return unchanged
    no_body = "<html><h1>Hello</h1></html>"
    assert reloader.inject_script(no_body) == no_body


# ── Test 4: Module reload detection ───────────────────────────────────────────
def test_module_selective_reload() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        reloader = HotReloader(watch_dirs=[tmpdir])

        # Create a test module
        mod_path = Path(tmpdir) / "test_mod.py"
        with mod_path.open("w") as f:
            f.write("VALUE = 1\n")

        sys.path.insert(0, tmpdir)
        try:
            # dynamic import: `test_mod` is written to a temp dir at runtime, so
            # it cannot exist as a top-of-file import statement.
            test_mod = importlib.import_module("test_mod")

            assert test_mod.VALUE == 1

            # Snapshot modules
            reloader._snapshot_modules()

            # Modify the module — ensure mtime changes (sleep for filesystem resolution)
            time.sleep(1.1)
            with mod_path.open("w") as f:
                f.write("VALUE = 2\n")

            # Reload changed modules
            reloader._reload_changed_modules()
            assert test_mod.VALUE == 2, f"Expected VALUE=2, got VALUE={test_mod.VALUE}"
        finally:
            sys.path.remove(tmpdir)
            if "test_mod" in sys.modules:
                del sys.modules["test_mod"]


# ── Test 5: Full integration (start/stop) ─────────────────────────────────────
def test_start_stop() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        probe = Path(tmpdir) / "probe.py"
        probe.write_text("PROBE = 0\n")

        reloader = HotReloader(watch_dirs=[tmpdir])
        reloader.start()
        try:
            assert reloader._running == True

            # A started reloader is one that is actually WATCHING, and only a
            # delivered change proves that. Sleeping "long enough for the
            # watcher to initialize" proves nothing and is exactly the guess
            # that changes answer on a loaded machine. The watcher is edge
            # triggered and offers no ready signal, so re-arm the edge until it
            # is observed — a slow machine takes more turns, not a wrong result.
            deadline = time.monotonic() + 30.0
            n = 0
            while reloader.changes_seen == 0:
                assert time.monotonic() < deadline, (
                    "watcher delivered no change for a file written in its "
                    f"watched dir within 30s (changes_seen={reloader.changes_seen})"
                )
                n += 1
                probe.write_text(f"PROBE = {n}\n")
                reloader.wait_for_change(0.25)
        finally:
            reloader.stop()
        assert reloader._running == False


def main() -> bool:
    tests: tuple[Callable[[], None], ...] = (
        test_native_file_watcher,
        test_sse_generator,
        test_script_injection,
        test_module_selective_reload,
        test_start_stop,
    )
    # Bare asserts abort the file on the first break — that is this suite's
    # contract; the counts are emitted before bailing out.
    for fn in tests:
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
            finish()
            return False
        check(fn.__name__, True)
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
