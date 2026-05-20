#!/usr/bin/env python3
"""
Build the unified hyperdjango native extension.

Compiles dhi (SIMD validation), pg.zig (native Postgres), and
turboAPI patterns (HTTP server, radix trie router) into a single
shared library: _hyperdjango_native.so

Builds are ReleaseFast by default (build.zig defaults -Doptimize to ReleaseFast).

Usage:
    python zig/build_hyperdjango.py              # ReleaseFast build (production)
    python zig/build_hyperdjango.py --install    # ReleaseFast build + install
    python zig/build_hyperdjango.py --safe        # ReleaseSafe build (test gate: keeps
                                                   # bounds/overflow/UB panics, ~production speed)
    python zig/build_hyperdjango.py --debug      # Debug build (opt out of release)

ReleaseFast (production) silences UB. The test gate should build --safe so that
out-of-bounds writes, integer overflow, and illegal-behavior become LOUD panics
instead of silent memory corruption.
"""

import argparse
import importlib.machinery
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ZIG_MIN_MINOR = 16


def _zig_version(zig: str) -> str | None:
    """`zig version` output, or None when the binary doesn't run."""
    try:
        out = subprocess.run(
            [zig, "version"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return None
    return out or None


def _zig_version_ok(version: str) -> bool:
    m = re.match(r"^\d+\.(\d+)", version)
    return bool(m) and int(m.group(1)) >= ZIG_MIN_MINOR


def resolve_zig(project_dir: Path) -> str | None:
    """Locate a usable Zig >= 0.16 without requiring PATH setup.

    Order: HYPER_ZIG env (explicit pin) -> PATH -> repo-local toolchains
    (`.toolchain/zig*/zig` — where `make bootstrap` auto-downloads it — and
    `zig-*/zig` at the repo root, the hand-extracted-tarball layout) ->
    `~/.zig/zig` (the CI action's install dir). A fresh checkout on a server
    where someone extracted a Zig tarball next to the repo therefore builds
    with NO environment configuration — the exact failure this replaces was
    `hyper-build` dying with "zig not found on PATH" while a perfectly good
    toolchain sat in the working tree.
    """
    env_zig = os.environ.get("HYPER_ZIG")
    if env_zig:
        ver = _zig_version(env_zig)
        return env_zig if ver and _zig_version_ok(ver) else None
    candidates: list[Path] = []
    on_path = shutil.which("zig")
    if on_path:
        candidates.append(Path(on_path))
    for pattern in (".toolchain/zig*/zig", "zig-*/zig"):
        candidates.extend(sorted(project_dir.glob(pattern)))
    candidates.append(Path.home() / ".zig" / "zig")
    # Rank every usable candidate: STABLE releases beat dev snapshots (a
    # `0.17.0-dev` next to the pinned `0.16.0` must not win — dev compilers
    # routinely fail to build code written for the stable release), newest
    # stable first, PATH position breaking ties.
    usable: list[tuple[int, str, str]] = []
    for cand in candidates:
        if not (cand.is_file() and os.access(cand, os.X_OK)):
            continue
        ver = _zig_version(str(cand))
        if ver is None or not _zig_version_ok(ver):
            continue
        usable.append((1 if "-dev" in ver else 0, ver, str(cand)))
    if not usable:
        return None
    usable.sort(key=lambda t: (t[0], [-int(x) for x in re.findall(r"\d+", t[1])[:3]]))
    return usable[0][2]


def detect_python():
    ver = sys.version_info
    # Compile-time truth, not runtime state: the build needs the interpreter's
    # HEADERS/ABI to be free-threaded (Py_GIL_DISABLED changes the PyObject
    # field layout the Zig code compiles against). sys._is_gil_enabled() only
    # reports whether the GIL is active right now — a 3.14t interpreter run
    # with PYTHON_GIL=1 would misreport as incompatible, and that check does
    # not exist at all on older interpreters.
    free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    include = sysconfig.get_path("include")
    libdir = sysconfig.get_config_var("LIBDIR")
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]

    label = f"{ver.major}.{ver.minor}t" if free_threaded else f"{ver.major}.{ver.minor}"

    return {
        "version": f"{ver.major}.{ver.minor}.{ver.micro}",
        "label": label,
        "free_threaded": free_threaded,
        "include": include,
        "libdir": libdir,
        "suffix": suffix,
        "gil": "DISABLED" if free_threaded else "enabled",
    }


def require_compatible_toolchain(info) -> None:
    """Abort BEFORE invoking zig when the environment cannot produce a working
    build — a wrong interpreter otherwise surfaces minutes later as a cryptic
    Zig compile error deep inside the cimport (e.g. "no field named
    'ob_refcnt'": the GIL-enabled PyObject keeps its refcount in an anonymous
    union the Zig code cannot address, and the extension's whole architecture
    assumes free-threading anyway)."""
    if not info["free_threaded"]:
        print(
            f"\nERROR: incompatible Python — {info['version']} is a standard "
            "(GIL-enabled) CPython build.\n"
            "The native extension requires FREE-THREADED CPython (3.14t): the\n"
            "PyObject ABI differs under Py_GIL_DISABLED and the runtime is\n"
            "free-threading-only. This repo pins 3.14t in .python-version.\n"
            "\n"
            "Fix (a stale .venv created with the wrong interpreter is the\n"
            "usual cause):\n"
            "    uv python install 3.14t\n"
            "    rm -rf .venv\n"
            "    uv sync --group dev\n"
            "or run `make bootstrap`, which does all of this and verifies the\n"
            "result.",
            file=sys.stderr,
        )
        sys.exit(1)


def require_zig(zig: str | None) -> str:
    """Abort with remediation when no usable Zig toolchain was found."""
    if zig is None:
        print(
            "\nERROR: no usable Zig >= 0.16 found (checked HYPER_ZIG, PATH, "
            ".toolchain/zig*/, zig-*/, ~/.zig).\n"
            "Run `make bootstrap` — it downloads the pinned Zig into "
            ".toolchain/ automatically —\nor install one from "
            "https://ziglang.org/download/.",
            file=sys.stderr,
        )
        sys.exit(1)
    return zig


def _atomic_install(source: Path, target: Path) -> None:
    """Install a freshly built shared object WITHOUT disturbing running code.

    A plain copy opens the destination with O_TRUNC and rewrites it in place.
    The extension is a MAPPED file: every process that imported it — a live
    server, a test subprocess, this build's own earlier import — holds mmap
    pages backed by that inode's page cache. Rewriting it swaps those pages
    underneath running code, so the next call through a function pointer lands
    in whatever now occupies that file offset. Observed exactly that on the
    benchmark box:

        hyper-build[500038]: segfault at 78ecb3a04ce4 ip 000078ecb3a04ce4
        Code: ... <00> 5f 5f 67 6d 6f 6e 5f 73 74 61 72 74 5f 5f  (__gmon_start__)

    — ip equal to the fault address (an execute, not a read) at file offset
    0x4ce4, whose bytes are the ELF STRING TABLE. The process jumped into
    `.dynstr` because the text it meant to run had been replaced mid-flight.

    Writing a sibling temp file and renaming makes the swap atomic: rename
    only re-points the directory entry, so existing mappings keep the old
    inode (unlinked but alive and intact) while every new import gets the new
    one. No reader can ever observe a half-written or mixed-generation file.
    """
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
    try:
        shutil.copyfile(source, tmp)
        shutil.copystat(source, tmp)
        tmp.replace(target)  # atomic within the directory
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Build hyperdjango native extension")
    parser.add_argument(
        "--install", action="store_true", help="Install into hyperdjango/ package"
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="Build with ReleaseFast (now the default; kept for compatibility)",
    )
    parser.add_argument(
        "--safe",
        action="store_true",
        help="Build with ReleaseSafe (test/CI gate: keeps safety+UB checks at ~production speed)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Build with Debug (opt out of the release default)",
    )
    parser.add_argument(
        "--heap-safety",
        action="store_true",
        help=(
            "Swap the raw c_allocator for Zig's safety-checking DebugAllocator on "
            "the pool/db path (double-free / UAF / OOB detection). Composes with "
            "--safe/--debug. Slower; for memory-safety hunting, not production."
        ),
    )
    parser.add_argument(
        "--sanitize-thread",
        action="store_true",
        help=(
            "Compile the Zig code with ThreadSanitizer (data-race detection). "
            "Requires a ThreadSanitizer-instrumented CPython to RUN — the stock "
            "free-threaded interpreter SIGSEGVs under TSan. Deep-validation lane "
            "only, never production."
        ),
    )
    args = parser.parse_args()

    info = detect_python()
    zig_dir = Path(__file__).resolve().parent
    project_dir = zig_dir.parent

    print(f"Python {info['version']} (GIL: {info['gil']})")
    print(f"Extension suffix: {info['suffix']}")
    print(f"Include: {info['include']}")
    print(f"Lib: {info['libdir']}")

    require_compatible_toolchain(info)
    zig = require_zig(resolve_zig(project_dir))
    print(f"Zig: {zig}")

    # Map to build.zig -Dpython= value
    if info["free_threaded"]:
        py_arg = "3.14t"
    elif info["label"].startswith("3.14"):
        py_arg = "3.14"
    else:
        py_arg = "3.13"

    cmd = [
        zig,
        "build",
        f"-Dpython={py_arg}",
        f"-Dpy-include={info['include']}",
        f"-Dpy-libdir={info['libdir']}",
    ]

    # build.zig defaults to ReleaseFast; override for explicit Debug/Safe builds.
    if args.debug:
        optimize = "Debug"
    elif args.safe:
        optimize = "ReleaseSafe"
    else:
        optimize = "ReleaseFast"  # production default (also when --release is passed)
    cmd.append(f"-Doptimize={optimize}")

    if args.heap_safety:
        cmd.append("-Dheap-safety=true")
    if args.sanitize_thread:
        cmd.append("-Dsanitize-thread=true")

    print(f"\nOptimize mode: {optimize}")
    if args.heap_safety:
        print("Heap safety: DebugAllocator (double-free/UAF/OOB detection)")
    if args.sanitize_thread:
        print("ThreadSanitizer: ON (needs a --with-thread-sanitizer CPython to run)")
    print(f"\n{' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=zig_dir)
    if result.returncode != 0:
        print("Build failed!")
        sys.exit(result.returncode)

    lib_ext = ".dylib" if platform.system() == "Darwin" else ".so"
    dylib = zig_dir / "zig-out" / "lib" / f"libhyperdjango{lib_ext}"
    target = project_dir / "hyperdjango" / f"_hyperdjango_native{info['suffix']}"

    if args.install:
        _atomic_install(dylib, target)
        # Also copy with original name — the .so's install_name references
        # libhyperdjango.dylib and the dynamic linker needs to find it
        if platform.system() == "Darwin":
            original_name = project_dir / "hyperdjango" / f"libhyperdjango{lib_ext}"
            _atomic_install(dylib, original_name)
            # Ad-hoc codesign — macOS kills unsigned .so files with SIGKILL (exit 137)
            subprocess.run(["codesign", "-s", "-", target], check=False)
            if original_name.exists():
                subprocess.run(["codesign", "-s", "-", original_name], check=False)
        print(f"\nInstalled: {target}")
    else:
        print(f"\nBuilt: {dylib}")
        print(f"To install: cp {dylib} {target}")

    print(f"Python: {sys.executable}")
    if info["free_threaded"]:
        print("Free-threaded build (GIL disabled)")


if __name__ == "__main__":
    main()
