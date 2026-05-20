#!/usr/bin/env python3
"""
Tests for file storage abstraction.

Usage:
    uv run hyper-test storage
"""

# hyper-test: unit

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from hyperdjango.storage import (
    FileSystemStorage,
    MemoryStorage,
    Storage,
    get_storage,
    set_storage,
)

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 60)
    print("File Storage Tests")
    print("=" * 60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(test_memory_storage())
    loop.run_until_complete(test_filesystem_storage())
    loop.run_until_complete(test_concurrent_saves())
    test_global_storage()
    test_url_generation()
    test_storage_interface()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("Failed:")
        for e in RESULTS["errors"]:
            print(f"  - {e}")
    print(f"{'=' * 60}")
    return 0 if RESULTS["failed"] == 0 else 1


async def test_memory_storage():
    print("\n--- MemoryStorage ---")

    storage = MemoryStorage(base_url="/media/")

    # Save
    name = await storage.save("test.txt", b"hello world")
    check("save returns name", name == "test.txt")

    # Exists
    check("file exists after save", await storage.exists("test.txt"))
    check("nonexistent file doesn't exist", not await storage.exists("nope.txt"))

    # Open
    data = await storage.open("test.txt")
    check("open returns content", data == b"hello world")

    # Size
    sz = await storage.size("test.txt")
    check("size correct", sz == 11, f"got {sz}")

    # URL
    check("url correct", storage.url("test.txt") == "/media/test.txt")

    # Save duplicate — auto-rename
    name2 = await storage.save("test.txt", b"different")
    check("duplicate auto-renamed", name2 == "test_1.txt", f"got {name2!r}")
    check(
        "both files exist",
        await storage.exists("test.txt") and await storage.exists("test_1.txt"),
    )

    # Listdir
    await storage.save("photos/a.jpg", b"jpg1")
    await storage.save("photos/b.jpg", b"jpg2")
    await storage.save("docs/report.pdf", b"pdf1")

    dirs, files = await storage.listdir("")
    check("listdir root has dirs", "photos" in dirs and "docs" in dirs)
    check("listdir root has files", "test.txt" in files)

    dirs2, files2 = await storage.listdir("photos")
    check("listdir subdir has no dirs", len(dirs2) == 0)
    check("listdir subdir has files", "a.jpg" in files2 and "b.jpg" in files2)

    # Delete
    await storage.delete("test.txt")
    check("deleted file doesn't exist", not await storage.exists("test.txt"))

    # Delete non-existent (no error)
    await storage.delete("nonexistent.txt")
    check("delete nonexistent no error", True)

    # Open non-existent
    try:
        await storage.open("nonexistent.txt")
        check("open nonexistent raises", False)
    except FileNotFoundError:
        check("open nonexistent raises", True)

    # Size non-existent
    try:
        await storage.size("nonexistent.txt")
        check("size nonexistent raises", False)
    except FileNotFoundError:
        check("size nonexistent raises", True)

    # Clear
    storage.clear()
    check("clear removes all", not await storage.exists("test_1.txt"))

    # Save with subdirectory
    name3 = await storage.save("deep/nested/file.txt", b"nested")
    check("save with subdirs", name3 == "deep/nested/file.txt")
    check("nested file exists", await storage.exists("deep/nested/file.txt"))


async def test_filesystem_storage():
    print("\n--- FileSystemStorage ---")

    # Use a temporary directory
    tmpdir = tempfile.mkdtemp(prefix="hyper_storage_test_")

    try:
        storage = FileSystemStorage(location=tmpdir, base_url="/uploads/")

        # Save
        name = await storage.save("hello.txt", b"filesystem content")
        check("fs save returns name", name == "hello.txt")
        check("fs file exists on disk", (Path(tmpdir) / "hello.txt").exists())

        # Open
        data = await storage.open("hello.txt")
        check("fs open returns content", data == b"filesystem content")

        # Exists
        check("fs exists", await storage.exists("hello.txt"))
        check("fs not exists", not await storage.exists("nope.txt"))

        # Size
        sz = await storage.size("hello.txt")
        check("fs size correct", sz == len(b"filesystem content"))

        # URL
        check("fs url", storage.url("hello.txt") == "/uploads/hello.txt")

        # Auto-rename duplicate
        name2 = await storage.save("hello.txt", b"different")
        check("fs duplicate renamed", name2 == "hello_1.txt")

        # Subdirectory
        name3 = await storage.save("sub/dir/file.bin", b"\x00\x01\x02")
        check("fs subdir save", name3 == "sub/dir/file.bin")
        check(
            "fs subdir exists",
            (Path(tmpdir) / "sub" / "dir" / "file.bin").exists(),
        )

        # Listdir
        dirs, files = await storage.listdir("")
        check("fs listdir root has subdir", "sub" in dirs)
        check("fs listdir root has files", "hello.txt" in files)

        # Delete
        await storage.delete("hello.txt")
        check("fs deleted", not (Path(tmpdir) / "hello.txt").exists())

        # Path traversal prevention: a name that escapes the storage root is
        # REJECTED (fail-closed), not silently scrubbed to a surprising path.
        from hyperdjango.storage import SuspiciousFileOperation

        raised = False
        try:
            await storage.save("../../../etc/passwd", b"attack")
        except SuspiciousFileOperation:
            raised = True
        check("path traversal raises SuspiciousFileOperation", raised)
        check("traversal file not in /etc", not Path("/etc/passwd.tmp").exists())

        # NUL byte in a name → clean SuspiciousFileOperation (400), not an
        # uncaught ValueError from realpath/lstat (which would 500 a public
        # upload endpoint).
        nul_raised = False
        try:
            await storage.save("evil\x00.txt", b"x")
        except SuspiciousFileOperation:
            nul_raised = True
        check("NUL byte in name raises SuspiciousFileOperation", nul_raised)

        # Binary content
        binary = bytes(range(256))
        name5 = await storage.save("binary.bin", binary)
        data5 = await storage.open(name5)
        check("fs binary roundtrip", data5 == binary)

        # Large file
        large = b"x" * (1024 * 1024)  # 1MB
        name6 = await storage.save("large.dat", large)
        sz6 = await storage.size(name6)
        check("fs large file size", sz6 == 1024 * 1024)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_concurrent_saves():
    """Regression: concurrent saves in one process must not collide on the
    temp filename. The old ".upload.{pid}.tmp" was shared, so two in-flight
    saves clobbered each other's bytes mid-write (corruption)."""
    print("\n--- Concurrent Saves (no temp collision) ---")

    tmpdir = tempfile.mkdtemp(prefix="hyper_storage_concurrent_")
    try:
        storage = FileSystemStorage(location=tmpdir, base_url="/uploads/")

        # Many distinct files, each with distinctive content of varying size.
        n = 40

        async def save_one(i):
            name = f"file_{i:03d}.bin"
            # Distinct, sizeable payload so a cross-write would be detectable.
            payload = (f"CONTENT-{i:03d}-".encode()) * (500 + i * 37)
            saved = await storage.save(name, payload)
            return saved, payload

        results = await asyncio.gather(*(save_one(i) for i in range(n)))

        all_ok = True
        for saved_name, payload in results:
            data = await storage.open(saved_name)
            if data != payload:
                all_ok = False
                break
        check("concurrent distinct saves keep intact content", all_ok)
        check("all concurrent files saved", len(set(r[0] for r in results)) == n)

        # Concurrent saves of the SAME name: no corruption, and every returned
        # name resolves to intact bytes (last-writer-wins on the base name is
        # acceptable; interleaved/corrupt bytes are not).
        same_payloads = {}

        async def save_same(i):
            payload = (f"SAME-{i:04d}-".encode()) * 1000
            saved = await storage.save("dup.bin", payload)
            same_payloads[saved] = payload
            return saved

        same_names = await asyncio.gather(*(save_same(i) for i in range(10)))
        intact = True
        for nm in set(same_names):
            data = await storage.open(nm)
            if data != same_payloads[nm]:
                intact = False
                break
        check("concurrent same-name saves are never corrupt", intact)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_global_storage():
    print("\n--- Global Storage ---")

    # Initially no storage configured
    try:
        get_storage()
        check("get_storage raises without config", False)
    except RuntimeError:
        check("get_storage raises without config", True)

    # Set storage
    mem = MemoryStorage()
    set_storage(mem)
    check("set_storage works", get_storage() is mem)

    # Reset
    set_storage(None)


def test_url_generation():
    print("\n--- URL Generation ---")

    s1 = FileSystemStorage(location="/tmp/test", base_url="/media/")
    check("url with trailing slash", s1.url("file.txt") == "/media/file.txt")

    s2 = FileSystemStorage(location="/tmp/test", base_url="/media")
    check("url without trailing slash", s2.url("file.txt") == "/media/file.txt")

    s3 = MemoryStorage(base_url="/assets/")
    check("memory url", s3.url("img/logo.png") == "/assets/img/logo.png")

    # URL with subdirectory
    check("url subdir", s1.url("photos/avatar.jpg") == "/media/photos/avatar.jpg")


def test_storage_interface():
    print("\n--- Storage Interface ---")

    # Verify abstract base class methods exist
    base = Storage()
    for method in [
        "save",
        "open",
        "delete",
        "exists",
        "url",
        "listdir",
        "size",
        "get_available_name",
    ]:
        check(f"Storage has {method}", hasattr(base, method))

    # Verify subclasses implement all methods
    for cls_name, cls in [
        ("FileSystemStorage", FileSystemStorage),
        ("MemoryStorage", MemoryStorage),
    ]:
        for method in [
            "save",
            "open",
            "delete",
            "exists",
            "url",
            "listdir",
            "size",
            "get_available_name",
        ]:
            impl = getattr(cls, method)
            base_impl = getattr(Storage, method)
            check(f"{cls_name} implements {method}", impl is not base_impl)


if __name__ == "__main__":
    sys.exit(main())
