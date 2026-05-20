#!/usr/bin/env python3
"""Benchmark: compiled template caching vs compile-every-render vs Jinja2.

Proves that compile-once-render-many is the fast path, and measures the
overhead of compilation vs rendering separately.

Benchmarks:
1. Zig compile time (one-shot)
2. Zig render (cached template, different context each time)
3. Zig compile + render every call (no caching — worst case)
4. Jinja2 render (cached template)
5. Jinja2 compile + render every call (no caching)
6. TemplateEngine.render() (full Python→Zig path with LRU cache)
"""

import time

from hyperdjango._hyperdjango_native import _template_compile, _template_render

TEMPLATE = """<!DOCTYPE html>
<html>
<head><title>{{ title }}</title></head>
<body>
<h1>{{ title }}</h1>
{% if user is defined and user %}
<p>Welcome, {{ user.name }}!</p>
{% endif %}
<ul>
{% for item in items %}
<li class="{% if loop.first %}first{% endif %}{% if loop.last %}last{% endif %}">
  {{ loop.index }}. {{ item.name|upper }} — ${{ item.price }}
  {% if item.on_sale %}<span class="sale">SALE!</span>{% endif %}
</li>
{% endfor %}
</ul>
{% if items|length == 0 %}
<p>No items available.</p>
{% endif %}
<footer>{{ footer|default('Copyright 2026')|safe }}</footer>
</body>
</html>"""

CONTEXT = {
    "title": "Product Catalog",
    "user": {"name": "Alice"},
    "items": [
        {"name": f"Widget {i}", "price": f"{i * 9.99:.2f}", "on_sale": i % 3 == 0}
        for i in range(20)
    ],
    "footer": "<em>HyperDjango Store</em>",
}


def bench(name, func, iterations, warmup=100):
    for _ in range(warmup):
        func()
    t0 = time.perf_counter_ns()
    for _ in range(iterations):
        func()
    elapsed = time.perf_counter_ns() - t0
    ns_per = elapsed / iterations
    ops_sec = 1_000_000_000 / ns_per if ns_per > 0 else 0
    print(f"  {name:<45} {ns_per / 1000:>8.1f} μs/op  ({ops_sec:>10,.0f}/sec)")
    return ns_per


def main():
    iterations = 10_000

    print(f"\n{'=' * 75}")
    print(f"  Template Benchmark — {iterations:,} iterations each")
    print(
        f"  Template: {len(TEMPLATE)} chars, {TEMPLATE.count('{%')} tags, {TEMPLATE.count('{{')} vars"
    )
    print(f"  Context: {len(CONTEXT['items'])} items, user, title, footer")
    print(f"{'=' * 75}\n")

    # ── 1. Zig compile time ───────────────────────────────────────────────
    print("--- Zig Native Engine ---")
    compile_ns = bench(
        "Compile (one-shot, not cached)",
        lambda: _template_compile(TEMPLATE, "<bench>"),
        1000,
    )

    # ── 2. Zig render (cached capsule) ────────────────────────────────────
    capsule = _template_compile(TEMPLATE, "<bench>")
    cached_ns = bench(
        "Render (cached compiled template)",
        lambda: _template_render(capsule, CONTEXT),
        iterations,
    )

    # ── 3. Zig compile + render every time ────────────────────────────────
    uncached_ns = bench(
        "Compile + Render (no cache, every call)",
        lambda: _template_render(_template_compile(TEMPLATE, "<b>"), CONTEXT),
        iterations,
    )

    cache_speedup = uncached_ns / cached_ns if cached_ns > 0 else 0
    print(f"\n  Cache speedup: {cache_speedup:.1f}x (cached render vs compile+render)")
    print(
        f"  Compile overhead: {(compile_ns - cached_ns) / 1000:.1f} μs per template\n"
    )

    # ── 4. TemplateEngine (Python LRU cache path) ─────────────────────────
    print("--- TemplateEngine (Python → Zig with LRU) ---")
    from hyperdjango.templating import TemplateEngine

    engine = TemplateEngine(template_dir="/nonexistent")  # won't load files
    # Pre-cache via render_string
    engine.render_string(TEMPLATE, CONTEXT)
    engine_ns = bench(
        "TemplateEngine.render_string (LRU cached)",
        lambda: engine.render_string(TEMPLATE, CONTEXT),
        iterations,
    )
    print()

    # ── 5. Jinja2 comparison ──────────────────────────────────────────────
    try:
        import jinja2

        print("--- Jinja2 ---")
        env = jinja2.Environment(autoescape=jinja2.select_autoescape(["html"]))

        jinja_tmpl = env.from_string(TEMPLATE)
        jinja_cached_ns = bench(
            "Render (cached template)", lambda: jinja_tmpl.render(**CONTEXT), iterations
        )

        jinja_uncached_ns = bench(
            "Compile + Render (no cache)",
            lambda: env.from_string(TEMPLATE).render(**CONTEXT),
            iterations,
        )

        print(f"\n  Jinja2 cache speedup: {jinja_uncached_ns / jinja_cached_ns:.1f}x")

        # ── Summary ───────────────────────────────────────────────────────
        print(f"\n{'=' * 75}")
        print("  SUMMARY")
        print(f"{'=' * 75}")
        print(f"  Zig cached render:      {cached_ns / 1000:>8.1f} μs")
        print(f"  Jinja2 cached render:   {jinja_cached_ns / 1000:>8.1f} μs")
        print(f"  Speedup (cached):       {jinja_cached_ns / cached_ns:.1f}x faster")
        print()
        print(f"  Zig compile+render:     {uncached_ns / 1000:>8.1f} μs")
        print(f"  Jinja2 compile+render:  {jinja_uncached_ns / 1000:>8.1f} μs")
        print(
            f"  Speedup (uncached):     {jinja_uncached_ns / uncached_ns:.1f}x faster"
        )
        print()
        print(f"  TemplateEngine (LRU):   {engine_ns / 1000:>8.1f} μs")
        print(f"  vs Jinja2 cached:       {jinja_cached_ns / engine_ns:.1f}x faster")
        print(f"{'=' * 75}\n")

    except ImportError:
        print("  Jinja2 not installed — skipping comparison")


if __name__ == "__main__":
    main()
