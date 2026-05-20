#!/usr/bin/env python3
"""Test custom template tag library system.

Tests:
1. Library creation and registration
2. @register.filter — named and unnamed
3. @register.simple_tag — callable in templates
4. Filter integration with native Zig engine
5. Simple tag integration (as global callable)
6. Multiple libraries
7. Engine.load_library()
8. Library registry
9. Inclusion tags
10. Filter with arguments

Run: uv run hyper-test template_library
"""

# hyper-test: unit

import sys

from hyperdjango.templating import (
    Library,
    TemplateEngine,
    _library_registry,
    get_all_libraries,
    get_library,
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


def test_library_creation():
    """Test Library creation and auto-registration."""
    print("\n=== Library Creation ===")

    lib = Library("test_lib")
    check("library created", lib is not None)
    check("library has name", lib.name == "test_lib")
    check("auto-registered", "test_lib" in _library_registry)
    check("get_library works", get_library("test_lib") is lib)
    check("filters dict empty", len(lib.filters) == 0)
    check("simple_tags dict empty", len(lib.simple_tags) == 0)


def test_filter_decorator():
    """Test @register.filter decorator."""
    print("\n=== Filter Decorator ===")

    register = Library("filter_test")

    # Bare decorator
    @register.filter
    def upper(value):
        return str(value).upper()

    check("filter registered", "upper" in register.filters)
    check("filter callable", register.filters["upper"]("hello") == "HELLO")

    # Named decorator
    @register.filter("shout")
    def make_loud(value):
        return str(value).upper() + "!"

    check("named filter registered", "shout" in register.filters)
    check("named filter callable", register.filters["shout"]("hello") == "HELLO!")

    # Filter with extra args
    @register.filter
    def currency(value, symbol="$"):
        return f"{symbol}{float(value):,.2f}"

    check("currency filter", register.filters["currency"](1234.5) == "$1,234.50")
    check("currency with arg", register.filters["currency"](1234.5, "€") == "€1,234.50")


def test_simple_tag_decorator():
    """Test @register.simple_tag decorator."""
    print("\n=== Simple Tag Decorator ===")

    register = Library("tag_test")

    @register.simple_tag
    def greeting():
        return "Hello, World!"

    check("tag registered", "greeting" in register.simple_tags)
    check("tag callable", register.simple_tags["greeting"]() == "Hello, World!")

    @register.simple_tag("app_version")
    def get_version():
        return "2.0.0"

    check("named tag registered", "app_version" in register.simple_tags)
    check("named tag callable", register.simple_tags["app_version"]() == "2.0.0")

    @register.simple_tag
    def add(a, b):
        return a + b

    check("tag with args", register.simple_tags["add"](3, 4) == 7)


def test_engine_load_library():
    """Test TemplateEngine.load_library()."""
    print("\n=== Engine.load_library() ===")

    # Create a library
    register = Library("my_custom_lib")

    @register.filter
    def reverse_str(value):
        return str(value)[::-1]

    @register.simple_tag
    def site_name():
        return "HyperSite"

    # Create engine and load library
    engine = TemplateEngine(template_dir="/tmp/nonexistent")
    engine.load_library("my_custom_lib")

    check("filter loaded into engine", "reverse_str" in engine._custom_filters)
    check("tag loaded as global", "site_name" in engine._globals)

    # Error on unknown library
    error_raised = False
    try:
        engine.load_library("nonexistent_lib")
    except ValueError:
        error_raised = True
    check("unknown library raises ValueError", error_raised)


def test_filter_with_zig_engine():
    """Test custom filters work with the native Zig template engine."""
    print("\n=== Filter with Zig Engine ===")

    try:
        from hyperdjango._hyperdjango_native import (
            _template_compile,
            _template_register_filter,
            _template_render,
        )
    except ImportError:
        print("  SKIP: native extension not available")
        return

    register = Library("zig_filter_test")

    @register.filter
    def double(value):
        return str(value) * 2

    @register.filter
    def prefix(value, pre=">>"):
        return f"{pre}{value}"

    # Compile template
    capsule = _template_compile("{{ name|double }}", "<test>")

    # Register custom filter
    _template_register_filter(capsule, "double", register.filters["double"])

    # Render
    result = _template_render(capsule, {"name": "hi"})
    output = result.decode("utf-8") if isinstance(result, bytes) else result
    check("custom filter in Zig engine", output == "hihi", f"got '{output}'")


def test_simple_tag_in_template():
    """Test simple tags work as callables in templates."""
    print("\n=== Simple Tag in Template ===")

    try:
        from hyperdjango._hyperdjango_native import (
            _template_compile,
            _template_render,
        )
    except ImportError:
        print("  SKIP: native extension not available")
        return

    register = Library("tag_render_test")

    import html as _html

    class _Safe(str):
        """Already-safe HTML (implements the __html__ protocol the engine honors)."""

        def __html__(self):
            return str(self)

    @register.simple_tag
    def bold(text):
        # A tag emitting HTML must escape its untrusted arg and mark the result
        # safe (the Django format_html/mark_safe pattern); the native engine
        # auto-escapes a plain-str tag return to prevent XSS.
        return _Safe(f"<b>{_html.escape(text)}</b>")

    # Simple tags are passed as context globals
    capsule = _template_compile("{{ bold('hello') }}", "<test>")
    result = _template_render(capsule, {"bold": register.simple_tags["bold"]})
    output = result.decode("utf-8") if isinstance(result, bytes) else result
    check(
        "simple tag renders (safe HTML honored)",
        output == "<b>hello</b>",
        f"got '{output}'",
    )

    # A tag returning a PLAIN str with markup is auto-escaped (XSS-safe default).
    @register.simple_tag
    def plainbold(text):
        return f"<b>{text}</b>"

    capsule2 = _template_compile("{{ pb('hi') }}", "<test>")
    out2 = _template_render(capsule2, {"pb": register.simple_tags["plainbold"]})
    out2 = out2.decode("utf-8") if isinstance(out2, bytes) else out2
    check(
        "plain-str tag auto-escaped", out2 == "&lt;b&gt;hi&lt;/b&gt;", f"got '{out2}'"
    )


def test_multiple_libraries():
    """Test multiple libraries coexist."""
    print("\n=== Multiple Libraries ===")

    lib1 = Library("lib_alpha")
    lib2 = Library("lib_beta")

    @lib1.filter
    def alpha_filter(v):
        return f"A:{v}"

    @lib2.filter
    def beta_filter(v):
        return f"B:{v}"

    check("lib1 registered", "lib_alpha" in _library_registry)
    check("lib2 registered", "lib_beta" in _library_registry)
    check("lib1 filter works", lib1.filters["alpha_filter"]("x") == "A:x")
    check("lib2 filter works", lib2.filters["beta_filter"]("x") == "B:x")
    check("lib1 doesn't have lib2 filter", "beta_filter" not in lib1.filters)

    all_libs = get_all_libraries()
    check(
        "get_all_libraries includes both",
        "lib_alpha" in all_libs and "lib_beta" in all_libs,
    )


def test_inclusion_tag():
    """Test @register.inclusion_tag decorator."""
    print("\n=== Inclusion Tag ===")

    register = Library("inclusion_test")

    @register.inclusion_tag("_widget.html")
    def widget(title, count):
        return {"title": title, "count": count}

    check("inclusion tag registered", "widget" in register.inclusion_tags)
    # The wrapper returns rendered template or fallback comment
    result = register.inclusion_tags["widget"]("Stats", 42)
    check("inclusion tag returns something", isinstance(result, str))


def test_engine_render_with_library():
    """Test full render pipeline with custom library."""
    print("\n=== Full Render with Library ===")

    try:
        from hyperdjango._hyperdjango_native import (
            _template_compile,
            _template_register_filter,
            _template_render,
        )
    except ImportError:
        print("  SKIP: native extension not available")
        return

    register = Library("full_render_test")

    @register.filter
    def exclaim(value):
        return f"{value}!!!"

    @register.filter
    def wrap(value, tag="span"):
        return f"<{tag}>{value}</{tag}>"

    # Test rendering with custom filter
    source = "{{ name|exclaim }}"
    capsule = _template_compile(source, "<test>")
    _template_register_filter(capsule, "exclaim", register.filters["exclaim"])
    result = _template_render(capsule, {"name": "World"})
    output = result.decode("utf-8") if isinstance(result, bytes) else result
    check("exclaim filter renders", output == "World!!!", f"got '{output}'")


def main():
    global passed, failed

    test_library_creation()
    test_filter_decorator()
    test_simple_tag_decorator()
    test_engine_load_library()
    test_filter_with_zig_engine()
    test_simple_tag_in_template()
    test_multiple_libraries()
    test_inclusion_tag()
    test_engine_render_with_library()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All template library tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
