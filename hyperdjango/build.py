"""
Build the native Zig extension.

Usage:
    uv run hyper-build              # ReleaseFast build + install (default)
    uv run hyper-build --debug      # Debug build + install
    uv run hyper-build --no-install # build only, don't copy into the package

This ensures the extension is always built with the SAME Python that uv uses,
avoiding ABI mismatches between pyenv/system Python and uv's managed Python.
"""

import subprocess
import sys
from pathlib import Path

from hyperdjango.logging import logger


def main() -> None:
    # Find build_hyperdjango.py relative to this file
    pkg_dir = Path(__file__).resolve().parent
    project_dir = pkg_dir.parent
    build_script = project_dir / "zig" / "build_hyperdjango.py"

    if not build_script.exists():
        logger.error("Build script not found: {path}", path=build_script)
        sys.exit(1)

    # Forward all args to the build script, using THIS Python interpreter.
    forwarded = list(sys.argv[1:])

    # hyper-build installs into the package by default (ReleaseFast + install).
    # `--no-install` opts out; it's consumed here, not understood by the Zig build.
    if "--no-install" in forwarded:
        forwarded.remove("--no-install")
    elif "--install" not in forwarded:
        forwarded.append("--install")

    args = [sys.executable, str(build_script)] + forwarded

    result = subprocess.run(args, cwd=str(project_dir))
    sys.exit(result.returncode)
