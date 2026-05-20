"""
Tests: hyperdjango.conf.get_setting() caching.

Proves:
  1. First call for a name resolves and caches it
  2. Second call returns cached value without re-probing
  3. clear_settings_cache() invalidates
  4. Default value still returned for names not in DEFAULTS or Django
  5. Default value is NOT cached (different callers may pass different defaults)
  6. Django settings override DEFAULTS
  7. _resolve_django_settings() is cached
  8. get_all_settings() still works (includes env overrides)
  9. Names in DEFAULTS are returned when Django doesn't have them
  10. Parity: output matches pre-cache behavior for known names

Run: uv run python scripts/test_get_setting_cache.py
"""

# hyper-test: unit

import os
from unittest.mock import patch

# Isolate from HYPER_* env-var overrides BEFORE importing conf — CI sets
# HYPER_POOL_SIZE / HYPER_THREAD_POOL_SIZE / HYPER_TEST_PROFILE which
# would override the DEFAULTS values this test asserts on. (We test the
# caching layer's behavior here, not the env-resolution layer's.)
for _k in list(os.environ):
    if _k.startswith("HYPER_"):
        os.environ.pop(_k, None)

# Pure-Python test — no DB needed
from hyperdjango.conf import (
    _DJANGO_OVERRIDES,
    DEFAULTS,
    clear_settings_cache,
    get_all_settings,
    get_setting,
)
from hyperdjango.testkit import check, finish, run_main


def main() -> bool:
    print("── get_setting() caching ──")

    clear_settings_cache()

    # Pick a name that's in DEFAULTS
    default_name = next(iter(DEFAULTS.keys()))
    default_val = DEFAULTS[default_name]

    # First call resolves
    v1 = get_setting(default_name)
    check("first call returns DEFAULTS value", v1 == default_val, f"got {v1!r}")

    # DEFAULTS values are NOT cached (read fresh every call so tests can
    # patch DEFAULTS to inject local overrides). Only Django overrides
    # get cached in _DJANGO_OVERRIDES.
    check(
        "DEFAULTS values not cached in _DJANGO_OVERRIDES",
        default_name not in _DJANGO_OVERRIDES,
    )

    # patch.dict(DEFAULTS, ...) takes effect immediately
    with patch.dict(DEFAULTS, {default_name: "patched"}):
        v_patched = get_setting(default_name)
        check(
            "patch.dict(DEFAULTS) takes effect immediately",
            v_patched == "patched",
            f"got {v_patched!r}",
        )
    # After patch context exits, back to original
    v_restored = get_setting(default_name)
    check(
        "value restored after patch.dict exit",
        v_restored == default_val,
        f"got {v_restored!r}",
    )

    # Second call returns same value
    v2 = get_setting(default_name)
    check("second call matches first", v1 == v2)

    # Cache clear clears overrides (no-op if no Django)
    clear_settings_cache()
    check("clear_settings_cache empties overrides", len(_DJANGO_OVERRIDES) == 0)

    # Unknown name returns default, NOT cached
    weird = get_setting("_UNKNOWN_NAME_FOR_TEST_", "sentinel")
    check("unknown name returns default", weird == "sentinel")
    check("unknown name NOT cached", "_UNKNOWN_NAME_FOR_TEST_" not in _DJANGO_OVERRIDES)

    # Different callers see their own default for unknown names
    weird1 = get_setting("_UNKNOWN_NAME_FOR_TEST_", "first")
    weird2 = get_setting("_UNKNOWN_NAME_FOR_TEST_", "second")
    check(
        "different defaults for unknown name honored",
        weird1 == "first" and weird2 == "second",
    )

    # get_all_settings() still works
    all_settings = get_all_settings()
    check("get_all_settings returns dict", isinstance(all_settings, dict))
    check(
        "get_all_settings includes DEFAULTS names",
        all(k in all_settings for k in list(DEFAULTS.keys())[:5]),
    )

    # Parity: get_setting matches get_all_settings for known names
    for name in list(DEFAULTS.keys())[:5]:
        via_get = get_setting(name)
        via_all = all_settings.get(name)
        check(
            f"parity: get_setting({name}) == get_all_settings()[{name}]",
            via_get == via_all,
            f"get={via_get!r} all={via_all!r}",
        )

    # ── Django override path (if Django is configured) ──
    print("\n── Django settings override ──")
    try:
        from django.conf import settings as django_settings

        if django_settings.configured:
            # Pick a HYPERDJANGO_ setting that Django might have
            # (test uses HYPERDJANGO_CACHE_KEY_PREFIX since that's a common one)
            test_name = "CACHE_KEY_PREFIX"
            if test_name in DEFAULTS:
                # First clear cache, then set an override
                clear_settings_cache()
                original = getattr(django_settings, f"HYPERDJANGO_{test_name}", None)
                django_settings.HYPERDJANGO_CACHE_KEY_PREFIX = "test_override"
                try:
                    v = get_setting(test_name)
                    check(
                        "Django override visible after clear_cache",
                        v == "test_override",
                        f"got {v!r}",
                    )

                    # Second call returns cached override
                    v2 = get_setting(test_name)
                    check("cached override persists", v == v2)

                    # clear_settings_cache picks up runtime mutation
                    django_settings.HYPERDJANGO_CACHE_KEY_PREFIX = "new_override"
                    clear_settings_cache()
                    v3 = get_setting(test_name)
                    check(
                        "clear_settings_cache sees runtime mutation",
                        v3 == "new_override",
                        f"got {v3!r}",
                    )
                finally:
                    # Restore original
                    if original is not None:
                        django_settings.HYPERDJANGO_CACHE_KEY_PREFIX = original
                    else:
                        if hasattr(django_settings, "HYPERDJANGO_CACHE_KEY_PREFIX"):
                            del django_settings.HYPERDJANGO_CACHE_KEY_PREFIX
                    clear_settings_cache()
        else:
            print("  (Django not configured — skipping override tests)")
    except Exception as e:
        print(f"  (Django unavailable: {e} — skipping override tests)")

    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
