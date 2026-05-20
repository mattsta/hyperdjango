"""
Test that service apps can actually import and initialize.

Tests each service app's module-level code (imports, HyperApp creation,
middleware setup) without starting the server. This catches import errors,
wrong API usage, and configuration problems.

Usage:
    uv run python scripts/test_service_launch.py
"""

# hyper-test: unit

import os
import sys
import traceback
from pathlib import Path

# The service apps require_setting("SECRET_KEY"/"ADMIN_SECRET", min_length=32)
# at import time so they fail closed on an unconfigured signing secret. This
# smoke test imports them in-process, so provide >=32-char values before any
# app (and hence hyperdjango.conf) is imported. Set here rather than in the
# shared runner env so framework tests that exercise SECRET_KEY resolution keep
# their unset-by-default assumption. ADMIN_SECRET is force-set because the
# shared runner env already seeds a shorter value that would fail the floor.
os.environ.setdefault("HYPER_SECRET_KEY", "service-launch-secret-key-0123456789")
os.environ["HYPER_ADMIN_SECRET"] = "service-launch-admin-secret-0123456789"

sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = 0
FAIL = 0


def test(name, passed, detail=""):
    global PASS, FAIL
    if passed:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        if detail:
            print(f"    {detail}")


def test_import(service_dir, app_module):
    """Test that a service app can be imported without crashing."""
    # Add service dir to path so local imports work (e.g., `from models import ...`)
    service_path = (Path(__file__).parent.parent / "services" / service_dir).resolve()

    if not service_path.is_dir():
        test(f"{service_dir}: directory exists", False, f"Not found: {service_path}")
        return

    test(f"{service_dir}: directory exists", True)

    # Check required files exist
    app_file = service_path / f"{app_module}.py"
    test(f"{service_dir}: {app_module}.py exists", app_file.is_file())

    # Try to compile
    import py_compile

    try:
        py_compile.compile(app_file, doraise=True)
        test(f"{service_dir}: {app_module}.py compiles", True)
    except py_compile.PyCompileError as e:
        test(f"{service_dir}: {app_module}.py compiles", False, str(e))
        return

    # Try to import as a package (supports relative imports like `from .models import ...`)
    import importlib

    package_name = f"services.{service_dir}"
    full_module = f"{package_name}.{app_module}"

    # Clear any previously imported modules from this service
    mods_to_remove = [m for m in sys.modules if m.startswith(package_name)]
    for m in mods_to_remove:
        del sys.modules[m]

    try:
        mod = importlib.import_module(full_module)
        test(f"{service_dir}: {app_module}.py imports", True)

        # Check it has an 'app' object
        if hasattr(mod, "app"):
            test(f"{service_dir}: has 'app' object", True)
            app_obj = mod.app
            test(
                f"{service_dir}: app.title set",
                bool(app_obj.title),
                f"title={app_obj.title!r}",
            )
        else:
            test(f"{service_dir}: has 'app' object", False, "No 'app' attribute found")

    except Exception as e:
        test(
            f"{service_dir}: {app_module}.py imports", False, f"{type(e).__name__}: {e}"
        )
        traceback.print_exc()
    finally:
        # Clean up imported modules
        mods_to_remove = [m for m in sys.modules if m.startswith(package_name)]
        for m in mods_to_remove:
            del sys.modules[m]


def test_setup_script(service_dir):
    """Test that a setup.py script can compile."""
    service_path = Path(__file__).parent.parent / "services" / service_dir
    setup_file = service_path / "setup.py"
    if setup_file.is_file():
        import py_compile

        try:
            py_compile.compile(setup_file, doraise=True)
            test(f"{service_dir}: setup.py compiles", True)
        except py_compile.PyCompileError as e:
            test(f"{service_dir}: setup.py compiles", False, str(e))
    else:
        pass  # setup.py is optional — skip silently


def test_templates(service_dir):
    """Test that template files exist."""
    service_path = Path(__file__).parent.parent / "services" / service_dir
    templates_dir = service_path / "templates"
    if templates_dir.is_dir():
        templates = [
            str(f.relative_to(templates_dir)) for f in templates_dir.rglob("*.html")
        ]
        test(
            f"{service_dir}: has templates ({len(templates)})",
            len(templates) > 0,
            f"Found: {templates}" if templates else "No .html files",
        )
    else:
        # Not all services have templates (hello, rest_api)
        pass


if __name__ == "__main__":
    print("=" * 60)
    print("Service App Launch Tests")
    print("=" * 60)

    # Test each service
    services = [
        "hello",
        "full_stack",
        "rest_api",
        "hypernews",
        "hyperai",
        "hypersecret",
        "hypermanager",
    ]
    if (Path(__file__).parent.parent / "services" / "benchmark_app").is_dir():
        services.append("benchmark_app")

    for ex in services:
        print(f"\n--- {ex} ---")
        test_import(ex, "app")
        test_setup_script(ex)
        test_templates(ex)

    print(f"\n{'=' * 60}")
    print(f"service_launch: {PASS} passed, {FAIL} failed")
    print(f"{'=' * 60}")

    if FAIL:
        sys.exit(1)
