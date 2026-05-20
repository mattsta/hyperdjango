"""Tests for the urlize filter in the Zig template engine."""

# hyper-test: unit

import sys
import time

from hyperdjango.templating import TemplateEngine

passed = 0
failed = 0
errors: list[str] = []


def test(name: str, template: str, context: dict, expected: str) -> None:
    global passed, failed
    engine = TemplateEngine()
    try:
        result = engine.render_string(template, context)
        if result.strip() == expected.strip():
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name}")
            print(f"    Expected: {expected!r}")
            print(f"    Got:      {result.strip()!r}")
            failed += 1
            errors.append(name)
    except Exception as e:
        print(f"  ERROR: {name}: {e}")
        failed += 1
        errors.append(name)


print("=" * 60)
print("TEST: urlize filter")
print("=" * 60)

# ── Basic HTTP URL ──
test(
    "http URL",
    "{{ text|urlize }}",
    {"text": "Visit http://example.com for info"},
    'Visit <a href="http://example.com" rel="noopener">http://example.com</a> for info',
)

# ── HTTPS URL ──
test(
    "https URL",
    "{{ text|urlize }}",
    {"text": "Go to https://example.com/path"},
    'Go to <a href="https://example.com/path" rel="noopener">https://example.com/path</a>',
)

# ── www. prefix ──
test(
    "www. prefix",
    "{{ text|urlize }}",
    {"text": "Check www.example.com today"},
    'Check <a href="http://www.example.com" rel="noopener">www.example.com</a> today',
)

# ── Email address ──
test(
    "email address",
    "{{ text|urlize }}",
    {"text": "Contact user@example.com for help"},
    'Contact <a href="mailto:user@example.com">user@example.com</a> for help',
)

# ── Multiple URLs in text ──
test(
    "multiple URLs",
    "{{ text|urlize }}",
    {"text": "http://a.com and http://b.com"},
    '<a href="http://a.com" rel="noopener">http://a.com</a> and <a href="http://b.com" rel="noopener">http://b.com</a>',
)

# ── URL with query string ──
test(
    "URL with query string",
    "{{ text|urlize }}",
    {"text": "See https://example.com/search?q=test&page=1"},
    'See <a href="https://example.com/search?q=test&amp;page=1" rel="noopener">https://example.com/search?q=test&amp;page=1</a>',
)

# ── URL at end of sentence ──
test(
    "URL followed by period",
    "{{ text|urlize }}",
    {"text": "Visit http://example.com."},
    'Visit <a href="http://example.com" rel="noopener">http://example.com</a>.',
)

# ── URL followed by comma ──
test(
    "URL followed by comma",
    "{{ text|urlize }}",
    {"text": "Go to http://a.com, then http://b.com"},
    'Go to <a href="http://a.com" rel="noopener">http://a.com</a>, then <a href="http://b.com" rel="noopener">http://b.com</a>',
)

# ── No URLs — plain text ──
test(
    "no URLs in text",
    "{{ text|urlize }}",
    {"text": "Just plain text here"},
    "Just plain text here",
)

# ── HTML entities in surrounding text ──
test(
    "HTML entities in text",
    "{{ text|urlize }}",
    {"text": "A & B at http://example.com"},
    'A &amp; B at <a href="http://example.com" rel="noopener">http://example.com</a>',
)

# ── URL with path and fragment ──
test(
    "URL with path and fragment",
    "{{ text|urlize }}",
    {"text": "See https://docs.example.com/api/v2#section"},
    'See <a href="https://docs.example.com/api/v2#section" rel="noopener">https://docs.example.com/api/v2#section</a>',
)

# ── Mixed URL and email ──
test(
    "mixed URL and email",
    "{{ text|urlize }}",
    {"text": "Visit http://example.com or email support@example.com"},
    'Visit <a href="http://example.com" rel="noopener">http://example.com</a> or email <a href="mailto:support@example.com">support@example.com</a>',
)

# ── Empty string ──
test("empty string", "{{ text|urlize }}", {"text": ""}, "")

# ── URL only ──
test(
    "URL only — no surrounding text",
    "{{ text|urlize }}",
    {"text": "https://example.com"},
    '<a href="https://example.com" rel="noopener">https://example.com</a>',
)

# ── Performance ──
print("\n── Performance ──")
engine = TemplateEngine()
tmpl = "{{ text|urlize }}"
ctx = {
    "text": "Visit http://example.com and https://docs.example.com/api for info. Contact admin@example.com for help."
}

for _ in range(100):
    engine.render_string(tmpl, ctx)

start = time.perf_counter_ns()
N = 10000
for _ in range(N):
    engine.render_string(tmpl, ctx)
elapsed = time.perf_counter_ns() - start
print(f"  urlize (3 links in text): {elapsed / N:.0f} ns/render ({N} iterations)")

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
if errors:
    print(f"Failed: {', '.join(errors)}")
print(f"{'=' * 60}")
sys.exit(1 if failed else 0)
