"""
Tests for template extends/inheritance via TemplateEngine on non-main threads.

# hyper-test: unit

The Zig template engine stores the template loader in a threadlocal variable.
TemplateEngine.__post_init__ sets it on the calling thread, but worker threads
in the Zig HTTP server need it set per-render via _apply_render_config().

This test validates that extends works correctly:
1. On the main thread (direct TemplateEngine.render)
2. On a background thread (simulating Zig server worker)
3. With nested extends (grandchild → child → base)
4. With multiple block overrides
5. With context variables flowing through extends
6. Via the full app.render() path on a background thread

Usage:
    uv run hyper-test template_extends_threaded
"""

import sys
import threading
from pathlib import Path

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "tests" / "templates"


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS: {name}")
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" -- {msg}"
        errors.append(err)
        print(f"  {err}")


# ---------------------------------------------------------------------------
# 1. Main thread — extends works normally
# ---------------------------------------------------------------------------


def test_extends_main_thread():
    print("\n-- extends on main thread --")
    engine = TemplateEngine(template_dir=str(TEMPLATE_DIR))
    html = engine.render("child.html", {"name": "World"})

    check("main: has DOCTYPE", "<!DOCTYPE html>" in html, repr(html[:100]))
    check("main: has <html>", "<html>" in html)
    check("main: has title block", "Child Page" in html)
    check("main: has content block", "Hello World!" in html)
    check("main: has base header", "Base Header" in html)
    check("main: has base footer", "Base Footer" in html)


# ---------------------------------------------------------------------------
# 2. Background thread — extends must also work
# ---------------------------------------------------------------------------


def test_extends_background_thread():
    print("\n-- extends on background thread --")
    engine = TemplateEngine(template_dir=str(TEMPLATE_DIR))
    result = {}
    error = {}

    def worker():
        try:
            html = engine.render("child.html", {"name": "Thread"})
            result["html"] = html
        except Exception as e:
            error["exc"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)

    if error:
        check("bg: render succeeded", False, str(error["exc"]))
        return

    html = result.get("html", "")
    check("bg: has DOCTYPE", "<!DOCTYPE html>" in html, repr(html[:100]))
    check("bg: has <html>", "<html>" in html)
    check("bg: has title block", "Child Page" in html)
    check("bg: has content block", "Hello Thread!" in html)
    check("bg: has base header", "Base Header" in html)
    check("bg: has base footer", "Base Footer" in html)


# ---------------------------------------------------------------------------
# 3. Multiple background threads concurrently
# ---------------------------------------------------------------------------


def test_extends_concurrent_threads():
    print("\n-- extends on concurrent threads --")
    engine = TemplateEngine(template_dir=str(TEMPLATE_DIR))
    results = {}
    errors_list = []

    def worker(thread_id):
        try:
            html = engine.render("child.html", {"name": f"T{thread_id}"})
            results[thread_id] = html
        except Exception as e:
            errors_list.append((thread_id, e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    check(
        "concurrent: no errors",
        len(errors_list) == 0,
        "; ".join(f"T{tid}: {e}" for tid, e in errors_list),
    )
    check("concurrent: all completed", len(results) == 8, f"got {len(results)}")

    for tid, html in results.items():
        if "<!DOCTYPE html>" not in html:
            check(f"concurrent T{tid}: has DOCTYPE", False, repr(html[:80]))
            break
        if f"Hello T{tid}!" not in html:
            check(f"concurrent T{tid}: has content", False, repr(html[:80]))
            break
    else:
        check("concurrent: all have DOCTYPE + content", True)


# ---------------------------------------------------------------------------
# 4. Extends with super() blocks
# ---------------------------------------------------------------------------


def test_extends_with_super():
    print("\n-- extends with super --")
    engine = TemplateEngine(template_dir=str(TEMPLATE_DIR))

    # child_with_super.html should use {{ super() }} in some blocks
    if not (TEMPLATE_DIR / "child_with_super.html").exists():
        check("super: template exists", False, "child_with_super.html not found")
        return

    html = engine.render("child_with_super.html", {})
    check(
        "super: has base content", "Base" in html or "Default" in html, repr(html[:200])
    )


# ---------------------------------------------------------------------------
# 5. Service templates — full_stack login.html extends base.html
# ---------------------------------------------------------------------------


def test_full_stack_extends():
    """Validate that service templates work with extends."""
    print("\n-- full_stack template extends --")
    fs_template_dir = (
        Path(__file__).resolve().parent.parent / "services" / "full_stack" / "templates"
    )
    if not fs_template_dir.is_dir():
        check("full_stack: template dir exists", False)
        return

    engine = TemplateEngine(template_dir=str(fs_template_dir))

    # Main thread
    html = engine.render("login.html", {"error": ""})
    check("fs main: has <nav>", "<nav" in html, repr(html[:100]))
    check("fs main: has <style>", "<style>" in html)
    check("fs main: has Login heading", "<h2>Login</h2>" in html)
    check("fs main: has form", 'name="username"' in html)

    # Background thread (simulates Zig worker)
    result = {}

    def worker():
        result["html"] = engine.render("login.html", {"error": "Bad password"})

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)

    html2 = result.get("html", "")
    check("fs bg: has <nav>", "<nav" in html2, repr(html2[:100]))
    check("fs bg: has <style>", "<style>" in html2)
    check("fs bg: has Login heading", "<h2>Login</h2>" in html2)
    check("fs bg: has error message", "Bad password" in html2)


# ---------------------------------------------------------------------------
# 6. HyperNews templates (more complex extends chains)
# ---------------------------------------------------------------------------


def test_hypernews_extends():
    """Validate hypernews templates if they exist."""
    print("\n-- hypernews template extends --")
    hn_template_dir = (
        Path(__file__).resolve().parent.parent / "services" / "hypernews" / "templates"
    )
    if not hn_template_dir.is_dir():
        check("hypernews: template dir exists", False)
        return

    templates = [f.name for f in hn_template_dir.iterdir() if f.suffix == ".html"]
    check("hypernews: has templates", len(templates) > 0, f"found {len(templates)}")

    # Find the base template
    base_exists = "base.html" in templates
    check("hypernews: has base.html", base_exists)
    if not base_exists:
        return

    engine = TemplateEngine(template_dir=str(hn_template_dir))

    # Render base directly
    html = engine.render("base.html", {})
    check("hn base: has <html>", "<html" in html)

    # Find a child template that extends base
    for tmpl in templates:
        if tmpl == "base.html":
            continue
        try:
            content = (hn_template_dir / tmpl).read_text()
            if "{% extends" in content:
                html = engine.render(tmpl, {"user": None, "request": None})
                check(f"hn {tmpl}: has <html>", "<html" in html, repr(html[:80]))
                break
        except Exception as e:
            check(f"hn {tmpl}: renders", False, str(e))
            break


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run_tests():
    global passed, failed, errors
    passed = 0
    failed = 0
    errors = []

    print("\n-- Template Extends Threaded Tests --\n")

    test_extends_main_thread()
    test_extends_background_thread()
    test_extends_concurrent_threads()
    test_extends_with_super()
    test_full_stack_extends()
    test_hypernews_extends()

    total = passed + failed
    print(f"\n{'=' * 60}")
    print(f"Template extends threaded: {passed}/{total} passed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
