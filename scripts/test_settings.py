#!/usr/bin/env python3
"""
Tests for the settings system — defaults, validation, env loading, definitions.

Usage:
    uv run hyper-test settings
"""

# hyper-test: unit

import os
import pathlib
import sys
import tempfile

# Isolate from HYPER_* env-var overrides BEFORE importing conf — CI
# sets HYPER_POOL_SIZE / HYPER_THREAD_POOL_SIZE / HYPER_TEST_PROFILE
# which would override the DEFAULTS values this test asserts on.
for _k in list(os.environ):
    if _k.startswith("HYPER_"):
        os.environ.pop(_k, None)

from hyperdjango.conf import (
    CONTENT_TYPE_FORM,
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MULTIPART,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_CACHE_BYTES,
    DEFAULT_MAX_PAGE_SIZE,
    DEFAULT_PAGE_SIZE,
    DEFAULT_RATE_LIMIT_MAX_REQUESTS,
    DEFAULT_RATE_LIMIT_WINDOW,
    DEFAULT_SLOW_QUERY_THRESHOLD_MS,
    DEFAULT_THREAD_POOL_SIZE,
    DEFAULTS,
    FALSY_STRINGS,
    HEADER_CONTENT_TYPE,
    MAX_REGEX_LENGTH,
    MAX_SEARCH_LENGTH,
    MAX_THREAD_POOL_SIZE,
    METERING_BUCKETS,
    ONE_DAY,
    ONE_HOUR,
    ONE_MINUTE,
    ONE_WEEK,
    SETTING_DEFINITIONS,
    STATIC_FILE_IMMUTABLE_MAX_AGE,
    STATIC_FILE_MAX_AGE,
    TEST_TIMEOUT_SECONDS,
    TRUTHY_STRINGS,
    WRITE_METHODS,
    SettingDefinition,
    SettingNotConfigured,
    _coerce_value,
    _parse_env_file,
    clear_settings_cache,
    get_all_settings,
    get_setting,
    is_explicitly_set,
    load_env_settings,
    require_setting,
    resolve_database_url,
    validate_settings,
)

RESULTS: dict[str, int | list[str]] = {"passed": 0, "failed": 0, "errors": []}


def check(name: str, condition: bool, details: str = "") -> None:
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} -- {details}")


def main() -> None:
    print("=" * 60)
    print("Settings System Tests")
    print("=" * 60)

    print("\n--- Platform Constants ---")
    test_platform_constants()

    print("\n--- DEFAULTS Dict ---")
    test_defaults_dict()

    print("\n--- SettingDefinition Dataclass ---")
    test_setting_definition()

    print("\n--- SETTING_DEFINITIONS Registry ---")
    test_setting_definitions_registry()

    print("\n--- get_setting Defaults ---")
    test_get_setting_defaults()

    print("\n--- get_setting Overrides ---")
    test_get_setting_overrides()
    test_require_setting()

    print("\n--- validate_settings: Required ---")
    test_validate_required()

    print("\n--- validate_settings: Types ---")
    test_validate_types()

    print("\n--- validate_settings: Ranges ---")
    test_validate_ranges()

    print("\n--- validate_settings: Choices ---")
    test_validate_choices()

    print("\n--- validate_settings: Valid Config ---")
    test_validate_valid_config()

    print("\n--- load_env_settings: HYPER_* Vars ---")
    test_load_env_hyper_vars()

    print("\n--- load_env_settings: Type Coercion ---")
    test_load_env_coercion()

    print("\n--- load_env_settings: Malformed Key Isolation ---")
    test_load_env_malformed_key_isolated()

    print("\n--- load_env_settings: .env File ---")
    test_load_env_dotenv()

    print("\n--- resolve_database_url: Single Source + Precedence ---")
    test_resolve_database_url_single_source()

    print("\n--- get_all_settings ---")
    test_get_all_settings()

    print("\n--- Internationalization Settings ---")
    test_i18n_settings()

    print("\n--- New Settings (45 additions) ---")
    test_new_settings_45()

    print("\n--- Config-Authority Settings (converted env reads) ---")
    test_config_authority_settings()

    print("\n--- Coerce Edge Cases ---")
    test_coerce_edge_cases()

    print("\n--- .env File Parsing ---")
    test_env_file_parsing()

    print("\n--- validate_settings: DEBUG Mode ---")
    test_validate_debug_mode()

    # ── Summary ──
    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Settings Tests: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print("FAILURES:")
        for err in RESULTS["errors"]:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("ALL PASSED")


# ── Test Functions ──────────────────────────────────────────────────────────


def test_platform_constants() -> None:
    """Verify all shared platform constants exist and have correct values."""
    check("ONE_MINUTE is 60", ONE_MINUTE == 60)
    check("ONE_HOUR is 3600", ONE_HOUR == 3600)
    check("ONE_DAY is 86400", ONE_DAY == 86400)
    check("ONE_WEEK is 604800", ONE_WEEK == 604800)
    check("DEFAULT_PAGE_SIZE is 25", DEFAULT_PAGE_SIZE == 25)
    check("DEFAULT_MAX_PAGE_SIZE is 100", DEFAULT_MAX_PAGE_SIZE == 100)
    check(
        "DEFAULT_RATE_LIMIT_MAX_REQUESTS is 100", DEFAULT_RATE_LIMIT_MAX_REQUESTS == 100
    )
    check("DEFAULT_RATE_LIMIT_WINDOW is 60", DEFAULT_RATE_LIMIT_WINDOW == 60)
    check("DEFAULT_CACHE_TTL is 300", DEFAULT_CACHE_TTL == 300)
    check(
        "DEFAULT_MAX_CACHE_BYTES is 256MB", DEFAULT_MAX_CACHE_BYTES == 256 * 1024 * 1024
    )
    check(
        "DEFAULT_SLOW_QUERY_THRESHOLD_MS is 100.0",
        DEFAULT_SLOW_QUERY_THRESHOLD_MS == 100.0,
    )
    check("CONTENT_TYPE_JSON correct", CONTENT_TYPE_JSON == "application/json")
    check(
        "CONTENT_TYPE_FORM correct",
        CONTENT_TYPE_FORM == "application/x-www-form-urlencoded",
    )
    check(
        "CONTENT_TYPE_MULTIPART correct",
        CONTENT_TYPE_MULTIPART == "multipart/form-data",
    )
    check("HEADER_CONTENT_TYPE correct", HEADER_CONTENT_TYPE == "content-type")
    check("MAX_SEARCH_LENGTH is 200", MAX_SEARCH_LENGTH == 200)
    check("MAX_REGEX_LENGTH is 100", MAX_REGEX_LENGTH == 100)
    check("TRUTHY_STRINGS is frozenset", isinstance(TRUTHY_STRINGS, frozenset))
    check(
        "TRUTHY_STRINGS has 1/true/yes/on",
        frozenset({"1", "true", "yes", "on"}) == TRUTHY_STRINGS,
    )
    check("FALSY_STRINGS is frozenset", isinstance(FALSY_STRINGS, frozenset))
    check(
        "FALSY_STRINGS has 0/false/no/empty",
        frozenset({"0", "false", "no", ""}) == FALSY_STRINGS,
    )
    check("WRITE_METHODS correct", frozenset({"POST", "PUT", "PATCH"}) == WRITE_METHODS)
    check(
        "METERING_BUCKETS correct", METERING_BUCKETS == ("hourly", "daily", "monthly")
    )
    check("DEFAULT_THREAD_POOL_SIZE is 24", DEFAULT_THREAD_POOL_SIZE == 24)
    check("MAX_THREAD_POOL_SIZE is 128", MAX_THREAD_POOL_SIZE == 128)
    check("STATIC_FILE_MAX_AGE is ONE_HOUR", STATIC_FILE_MAX_AGE == ONE_HOUR)
    check(
        "STATIC_FILE_IMMUTABLE_MAX_AGE is 1 year",
        STATIC_FILE_IMMUTABLE_MAX_AGE == 365 * ONE_DAY,
    )
    check("TEST_TIMEOUT_SECONDS is 300", TEST_TIMEOUT_SECONDS == 300)


def test_defaults_dict() -> None:
    """Verify DEFAULTS contains all expected categories."""
    # Database
    check("DEFAULTS has POOL_SIZE", "POOL_SIZE" in DEFAULTS)
    check("DEFAULTS POOL_SIZE is 0", DEFAULTS["POOL_SIZE"] == 0)
    check("DEFAULTS has PREPARED_STATEMENTS", "PREPARED_STATEMENTS" in DEFAULTS)
    check("DEFAULTS has DATABASE_URL", "DATABASE_URL" in DEFAULTS)

    # Security
    check("DEFAULTS has SECRET_KEY", "SECRET_KEY" in DEFAULTS)
    check("DEFAULTS has DEBUG", "DEBUG" in DEFAULTS)
    check("DEFAULTS DEBUG is False", DEFAULTS["DEBUG"] is False)
    check("DEFAULTS has ALLOWED_HOSTS", "ALLOWED_HOSTS" in DEFAULTS)
    check("DEFAULTS has CSRF_COOKIE_SAMESITE", "CSRF_COOKIE_SAMESITE" in DEFAULTS)

    # Cache
    check("DEFAULTS has CACHE_BACKEND", "CACHE_BACKEND" in DEFAULTS)
    check("DEFAULTS CACHE_BACKEND is memory", DEFAULTS["CACHE_BACKEND"] == "memory")

    # Auth
    check("DEFAULTS has PASSWORD_HASHER", "PASSWORD_HASHER" in DEFAULTS)
    check(
        "DEFAULTS PASSWORD_HASHER is argon2id",
        DEFAULTS["PASSWORD_HASHER"] == "argon2id",
    )
    check("DEFAULTS has SESSION_COOKIE_NAME", "SESSION_COOKIE_NAME" in DEFAULTS)

    # Email
    check("DEFAULTS has EMAIL_HOST", "EMAIL_HOST" in DEFAULTS)
    check("DEFAULTS has EMAIL_BACKEND", "EMAIL_BACKEND" in DEFAULTS)

    # Static
    check("DEFAULTS has STATIC_URL", "STATIC_URL" in DEFAULTS)

    # Upload
    check("DEFAULTS has MAX_UPLOAD_SIZE", "MAX_UPLOAD_SIZE" in DEFAULTS)
    check(
        "DEFAULTS MAX_UPLOAD_SIZE is 10MB",
        DEFAULTS["MAX_UPLOAD_SIZE"] == 10 * 1024 * 1024,
    )

    # Server
    check("DEFAULTS has HTTP_SERVER", "HTTP_SERVER" in DEFAULTS)
    check("DEFAULTS has THREAD_POOL_SIZE", "THREAD_POOL_SIZE" in DEFAULTS)

    # Logging
    check("DEFAULTS has LOG_LEVEL", "LOG_LEVEL" in DEFAULTS)
    check("DEFAULTS has LOG_FORMAT", "LOG_FORMAT" in DEFAULTS)

    # Internationalization
    check("DEFAULTS has LANGUAGE_CODE", "LANGUAGE_CODE" in DEFAULTS)
    check("DEFAULTS LANGUAGE_CODE is en", DEFAULTS["LANGUAGE_CODE"] == "en")
    check("DEFAULTS has TIME_ZONE", "TIME_ZONE" in DEFAULTS)
    check("DEFAULTS TIME_ZONE is UTC", DEFAULTS["TIME_ZONE"] == "UTC")
    check("DEFAULTS has USE_TZ", "USE_TZ" in DEFAULTS)
    check("DEFAULTS USE_TZ is True", DEFAULTS["USE_TZ"] is True)
    check("DEFAULTS has DATE_FORMAT", "DATE_FORMAT" in DEFAULTS)
    check("DEFAULTS DATE_FORMAT is N j, Y", DEFAULTS["DATE_FORMAT"] == "N j, Y")
    check("DEFAULTS has DATETIME_FORMAT", "DATETIME_FORMAT" in DEFAULTS)
    check("DEFAULTS has TIME_FORMAT", "TIME_FORMAT" in DEFAULTS)
    check("DEFAULTS has SHORT_DATE_FORMAT", "SHORT_DATE_FORMAT" in DEFAULTS)
    check("DEFAULTS has SHORT_DATETIME_FORMAT", "SHORT_DATETIME_FORMAT" in DEFAULTS)
    check("DEFAULTS has DECIMAL_SEPARATOR", "DECIMAL_SEPARATOR" in DEFAULTS)
    check("DEFAULTS DECIMAL_SEPARATOR is .", DEFAULTS["DECIMAL_SEPARATOR"] == ".")
    check("DEFAULTS has THOUSAND_SEPARATOR", "THOUSAND_SEPARATOR" in DEFAULTS)
    check("DEFAULTS THOUSAND_SEPARATOR is comma", DEFAULTS["THOUSAND_SEPARATOR"] == ",")
    check("DEFAULTS has USE_THOUSAND_SEPARATOR", "USE_THOUSAND_SEPARATOR" in DEFAULTS)
    check(
        "DEFAULTS USE_THOUSAND_SEPARATOR is False",
        DEFAULTS["USE_THOUSAND_SEPARATOR"] is False,
    )
    check("DEFAULTS has NUMBER_GROUPING", "NUMBER_GROUPING" in DEFAULTS)
    check("DEFAULTS NUMBER_GROUPING is 3", DEFAULTS["NUMBER_GROUPING"] == 3)
    check("DEFAULTS has FIRST_DAY_OF_WEEK", "FIRST_DAY_OF_WEEK" in DEFAULTS)
    check("DEFAULTS FIRST_DAY_OF_WEEK is 0", DEFAULTS["FIRST_DAY_OF_WEEK"] == 0)
    check("DEFAULTS has DATE_INPUT_FORMATS", "DATE_INPUT_FORMATS" in DEFAULTS)
    check(
        "DEFAULTS DATE_INPUT_FORMATS is list",
        isinstance(DEFAULTS["DATE_INPUT_FORMATS"], list),
    )
    check("DEFAULTS has DATETIME_INPUT_FORMATS", "DATETIME_INPUT_FORMATS" in DEFAULTS)
    check(
        "DEFAULTS DATETIME_INPUT_FORMATS is list",
        isinstance(DEFAULTS["DATETIME_INPUT_FORMATS"], list),
    )

    # Features
    check("DEFAULTS has FILE_ROUTING", "FILE_ROUTING" in DEFAULTS)
    check("DEFAULTS has VALIDATION_BACKEND", "VALIDATION_BACKEND" in DEFAULTS)

    # Count total (48 original + 15 i18n + 45 new + 6 features = 114)
    check("DEFAULTS has 110+ entries", len(DEFAULTS) >= 110, f"got {len(DEFAULTS)}")


def test_setting_definition() -> None:
    """Verify SettingDefinition dataclass structure."""
    defn = SettingDefinition(
        name="TEST_SETTING",
        type=int,
        default=42,
        required=False,
        min_value=0,
        max_value=100,
        choices=None,
        description="A test setting",
    )
    check("SettingDefinition.name", defn.name == "TEST_SETTING")
    check("SettingDefinition.type", defn.type is int)
    check("SettingDefinition.default", defn.default == 42)
    check("SettingDefinition.required", defn.required is False)
    check("SettingDefinition.min_value", defn.min_value == 0)
    check("SettingDefinition.max_value", defn.max_value == 100)
    check("SettingDefinition.choices", defn.choices is None)
    check("SettingDefinition.description", defn.description == "A test setting")
    check("SettingDefinition has __slots__", hasattr(SettingDefinition, "__slots__"))

    # Defaults for optional fields
    defn2 = SettingDefinition(name="MINIMAL", type=str, default="")
    check("SettingDefinition defaults: required=False", defn2.required is False)
    check("SettingDefinition defaults: min_value=None", defn2.min_value is None)
    check("SettingDefinition defaults: max_value=None", defn2.max_value is None)
    check("SettingDefinition defaults: choices=None", defn2.choices is None)
    check("SettingDefinition defaults: description=''", defn2.description == "")


def test_setting_definitions_registry() -> None:
    """Verify SETTING_DEFINITIONS covers all DEFAULTS keys."""
    for key in DEFAULTS:
        check(
            f"SETTING_DEFINITIONS has {key}",
            key in SETTING_DEFINITIONS,
            "missing from SETTING_DEFINITIONS",
        )

    # Verify all definitions match their DEFAULTS
    # Security settings use per-session random DEFAULTS, so their
    # SETTING_DEFINITIONS.default is a description, not the actual value.
    _random_secret_settings = frozenset(
        {
            "CSRF_SECRET",
            "SESSION_SECRET",
            "SESSION_SIGNING_KEY",
            "ADMIN_SECRET",
            "API_KEY",
        }
    )
    for name, defn in SETTING_DEFINITIONS.items():
        if name in _random_secret_settings:
            check(
                f"SETTING_DEFINITIONS[{name}].default is descriptive",
                defn.default == "<random per session>",
            )
        else:
            check(
                f"SETTING_DEFINITIONS[{name}].default matches DEFAULTS",
                defn.default == DEFAULTS.get(name),
                f"defn.default={defn.default!r}, DEFAULTS={DEFAULTS.get(name)!r}",
            )


def test_get_setting_defaults() -> None:
    """get_setting returns defaults when Django is not configured."""
    check("get_setting POOL_SIZE default", get_setting("POOL_SIZE") == 0)
    # DEBUG may be overridden by HYPER_DEBUG env var (set by test runner)
    import os

    if os.environ.get("HYPER_DEBUG"):
        check("get_setting DEBUG default", get_setting("DEBUG") is True)
    else:
        check("get_setting DEBUG default", get_setting("DEBUG") is False)
    check("get_setting HTTP_SERVER default", get_setting("HTTP_SERVER") == "auto")
    check(
        "get_setting unknown with default",
        get_setting("NONEXISTENT", "fallback") == "fallback",
    )
    check("get_setting unknown no default", get_setting("NONEXISTENT") is None)


def test_get_setting_overrides() -> None:
    """get_setting returns DEFAULTS value for known settings."""
    # These all come from DEFAULTS since Django isn't configured
    check("get_setting PREPARED_STATEMENTS", get_setting("PREPARED_STATEMENTS") is True)
    check("get_setting CACHE_BACKEND", get_setting("CACHE_BACKEND") == "memory")
    check("get_setting PASSWORD_HASHER", get_setting("PASSWORD_HASHER") == "argon2id")
    check(
        "get_setting SESSION_COOKIE_NAME",
        get_setting("SESSION_COOKIE_NAME") == "sessionid",
    )
    check("get_setting VALIDATION_BACKEND", get_setting("VALIDATION_BACKEND") == "dhi")


def test_require_setting() -> None:
    """require_setting fails on an unconfigured (auto-defaulted) security
    setting and returns the value once explicitly configured.

    Exercises the real registered SESSION_SIGNING_KEY. is_explicitly_set only
    reflects REGISTERED settings (like get_setting's own env resolution), so a
    registered security secret is the correct subject. os is imported at module
    scope in this file."""
    import os

    import hyperdjango.conf as _conf

    name = "SESSION_SIGNING_KEY"
    env_key = "HYPER_SESSION_SIGNING_KEY"
    saved = os.environ.get(env_key)

    def _rescan() -> None:
        _conf._ENV_OVERRIDES.clear()
        _conf._ENV_OVERRIDES_POPULATED = False

    try:
        # Unset -> falls through to the random DEFAULT, so is_explicitly_set is
        # False and require_setting refuses it (even though get_setting returns
        # the auto value).
        os.environ.pop(env_key, None)
        _rescan()
        check("is_explicitly_set False when unset", is_explicitly_set(name) is False)
        check("get_setting still returns the auto default", bool(get_setting(name)))
        raised = False
        try:
            require_setting(name)
        except SettingNotConfigured:
            raised = True
        check("require_setting raises on auto-defaulted security setting", raised)

        # Explicitly configured -> accepted.
        os.environ[env_key] = "explicitly-configured-signing-key"
        _rescan()
        check("is_explicitly_set True when configured", is_explicitly_set(name) is True)
        check(
            "require_setting returns the configured value",
            require_setting(name) == "explicitly-configured-signing-key",
        )

        # Explicitly set BUT empty -> still rejected (being set is not enough).
        os.environ[env_key] = ""
        _rescan()
        empty_raised = False
        try:
            require_setting(name)
        except SettingNotConfigured:
            empty_raised = True
        check("require_setting rejects an explicitly-empty value", empty_raised)

        # Too short for min_length -> rejected.
        os.environ[env_key] = "short"
        _rescan()
        short_raised = False
        try:
            require_setting(name, min_length=32)
        except SettingNotConfigured:
            short_raised = True
        check("require_setting rejects a value below min_length", short_raised)

        # Surrounding whitespace is not entropy: a value whose TRIMMED length is
        # below min_length is rejected even though its raw length would pass.
        os.environ[env_key] = "  ab  "  # raw len 6, trimmed len 2
        _rescan()
        ws_raised = False
        try:
            require_setting(name, min_length=6)
        except SettingNotConfigured:
            ws_raised = True
        check("require_setting min_length ignores surrounding whitespace", ws_raised)

        # Long enough passes min_length; a validator can still reject.
        os.environ[env_key] = "x" * 40
        _rescan()
        check(
            "require_setting accepts a value >= min_length",
            require_setting(name, min_length=32) == "x" * 40,
        )
        val_raised = False

        def _reject(_v):
            raise SettingNotConfigured("validator says no")

        try:
            require_setting(name, validator=_reject)
        except SettingNotConfigured:
            val_raised = True
        check("require_setting honors a custom validator", val_raised)
    finally:
        if saved is not None:
            os.environ[env_key] = saved
        else:
            os.environ.pop(env_key, None)
        _rescan()


def test_validate_required() -> None:
    """validate_settings catches missing required SECRET_KEY in production."""
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = ""
    settings["DEBUG"] = False
    errors = validate_settings(settings)
    found = any("SECRET_KEY" in e and "required" in e for e in errors)
    check("missing SECRET_KEY in production", found, f"errors={errors}")

    # With DEBUG=True, SECRET_KEY is not required
    settings["DEBUG"] = True
    errors = validate_settings(settings)
    found = any("SECRET_KEY" in e and "required" in e for e in errors)
    check("SECRET_KEY not required in DEBUG mode", not found, f"errors={errors}")


def test_validate_types() -> None:
    """validate_settings catches wrong types."""
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = "test-secret"
    settings["POOL_SIZE"] = "abc"  # should be int
    errors = validate_settings(settings)
    found = any("POOL_SIZE" in e and "expected type int" in e for e in errors)
    check("wrong type POOL_SIZE=str", found, f"errors={errors}")

    settings2 = dict(DEFAULTS)
    settings2["SECRET_KEY"] = "test-secret"
    settings2["DEBUG"] = "yes"  # should be bool
    errors2 = validate_settings(settings2)
    found2 = any("DEBUG" in e and "expected type bool" in e for e in errors2)
    check("wrong type DEBUG=str", found2, f"errors={errors2}")

    settings3 = dict(DEFAULTS)
    settings3["SECRET_KEY"] = "test-secret"
    settings3["ALLOWED_HOSTS"] = "example.com"  # should be list
    errors3 = validate_settings(settings3)
    found3 = any("ALLOWED_HOSTS" in e and "expected type list" in e for e in errors3)
    check("wrong type ALLOWED_HOSTS=str", found3, f"errors={errors3}")


def test_validate_ranges() -> None:
    """validate_settings catches out-of-range values."""
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = "test-secret"
    settings["THREAD_POOL_SIZE"] = 999
    errors = validate_settings(settings)
    found = any("THREAD_POOL_SIZE" in e and "above maximum" in e for e in errors)
    check("THREAD_POOL_SIZE=999 above max", found, f"errors={errors}")

    settings2 = dict(DEFAULTS)
    settings2["SECRET_KEY"] = "test-secret"
    settings2["THREAD_POOL_SIZE"] = 0
    errors2 = validate_settings(settings2)
    found2 = any("THREAD_POOL_SIZE" in e and "below minimum" in e for e in errors2)
    check("THREAD_POOL_SIZE=0 below min", found2, f"errors={errors2}")

    settings3 = dict(DEFAULTS)
    settings3["SECRET_KEY"] = "test-secret"
    settings3["EMAIL_PORT"] = 99999
    errors3 = validate_settings(settings3)
    found3 = any("EMAIL_PORT" in e and "above maximum" in e for e in errors3)
    check("EMAIL_PORT=99999 above max", found3, f"errors={errors3}")

    settings4 = dict(DEFAULTS)
    settings4["SECRET_KEY"] = "test-secret"
    settings4["PASSWORD_MIN_LENGTH"] = 0
    errors4 = validate_settings(settings4)
    found4 = any("PASSWORD_MIN_LENGTH" in e and "below minimum" in e for e in errors4)
    check("PASSWORD_MIN_LENGTH=0 below min", found4, f"errors={errors4}")


def test_validate_choices() -> None:
    """validate_settings catches invalid choice values."""
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = "test-secret"
    settings["HTTP_SERVER"] = "nginx"
    errors = validate_settings(settings)
    found = any("HTTP_SERVER" in e and "not one of" in e for e in errors)
    check("HTTP_SERVER=nginx invalid choice", found, f"errors={errors}")

    settings2 = dict(DEFAULTS)
    settings2["SECRET_KEY"] = "test-secret"
    settings2["LOG_LEVEL"] = "TRACE"
    errors2 = validate_settings(settings2)
    found2 = any("LOG_LEVEL" in e and "not one of" in e for e in errors2)
    check("LOG_LEVEL=TRACE invalid choice", found2, f"errors={errors2}")

    settings3 = dict(DEFAULTS)
    settings3["SECRET_KEY"] = "test-secret"
    settings3["CACHE_BACKEND"] = "bogus"
    errors3 = validate_settings(settings3)
    found3 = any("CACHE_BACKEND" in e and "not one of" in e for e in errors3)
    check("CACHE_BACKEND=bogus invalid choice", found3, f"errors={errors3}")

    settings4 = dict(DEFAULTS)
    settings4["SECRET_KEY"] = "test-secret"
    settings4["EMAIL_BACKEND"] = "sendgrid"
    errors4 = validate_settings(settings4)
    found4 = any("EMAIL_BACKEND" in e and "not one of" in e for e in errors4)
    check("EMAIL_BACKEND=sendgrid invalid choice", found4, f"errors={errors4}")


def test_validate_valid_config() -> None:
    """validate_settings passes with a fully valid configuration."""
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = "a-very-secure-secret-key-for-testing"
    errors = validate_settings(settings)
    check("valid config has no errors", len(errors) == 0, f"errors={errors}")


def test_load_env_hyper_vars() -> None:
    """load_env_settings reads HYPER_* environment variables."""
    env = {
        "HYPER_SECRET_KEY": "env-secret",
        "HYPER_DEBUG": "true",
        "HYPER_POOL_SIZE": "20",
        "HYPER_HTTP_SERVER": "zig",
        "OTHER_VAR": "ignored",
    }
    result = load_env_settings(env=env, dotenv_path=pathlib.Path("/nonexistent/.env"))
    check("env SECRET_KEY loaded", result.get("SECRET_KEY") == "env-secret")
    check("env DEBUG coerced to True", result.get("DEBUG") is True)
    check("env POOL_SIZE coerced to int", result.get("POOL_SIZE") == 20)
    check("env HTTP_SERVER loaded", result.get("HTTP_SERVER") == "zig")
    check("env OTHER_VAR ignored", "OTHER_VAR" not in result)


def test_load_env_coercion() -> None:
    """load_env_settings correctly coerces bool, int, list types."""
    env = {
        "HYPER_DEBUG": "false",
        "HYPER_PREPARED_STATEMENTS": "1",
        "HYPER_EMAIL_USE_TLS": "yes",
        "HYPER_POOL_SIZE": "0",
        "HYPER_EMAIL_PORT": "587",
        "HYPER_ALLOWED_HOSTS": "example.com, api.example.com",
        "HYPER_ALLOWED_UPLOAD_EXTENSIONS": ".jpg,.png,.gif",
        "HYPER_LOG_LEVEL": "DEBUG",
    }
    result = load_env_settings(env=env, dotenv_path=pathlib.Path("/nonexistent/.env"))

    check("coerce bool false", result.get("DEBUG") is False)
    check("coerce bool 1 -> True", result.get("PREPARED_STATEMENTS") is True)
    check("coerce bool yes -> True", result.get("EMAIL_USE_TLS") is True)
    check("coerce int 0", result.get("POOL_SIZE") == 0)
    check("coerce int 587", result.get("EMAIL_PORT") == 587)
    check(
        "coerce list comma-separated",
        result.get("ALLOWED_HOSTS") == ["example.com", "api.example.com"],
    )
    check(
        "coerce list extensions",
        result.get("ALLOWED_UPLOAD_EXTENSIONS") == [".jpg", ".png", ".gif"],
    )
    check("coerce str passthrough", result.get("LOG_LEVEL") == "DEBUG")


def test_load_env_malformed_key_isolated() -> None:
    """A single malformed HYPER_* value must not abort the whole env load:
    sibling keys still load, and the offending KEY (never its value) is logged."""
    from unittest.mock import patch

    import hyperdjango.conf as _conf

    env = {
        "HYPER_SECRET_KEY": "sibling-secret",
        "HYPER_HTTP_SERVER": "zig",
        "HYPER_POOL_SIZE": "not-an-int",  # int() rejects this
    }
    logged: list[tuple] = []

    def _capture(msg, *args, **kwargs):
        logged.append((msg, args))

    with patch.object(_conf._logger, "error", _capture):
        result = load_env_settings(
            env=env, dotenv_path=pathlib.Path("/nonexistent/.env")
        )

    check(
        "sibling str key still loads despite a malformed sibling",
        result.get("SECRET_KEY") == "sibling-secret",
        str(result),
    )
    check("sibling choice key still loads", result.get("HTTP_SERVER") == "zig")
    check("malformed key dropped, not partially coerced", "POOL_SIZE" not in result)
    check("an error was logged for the malformed key", len(logged) >= 1, str(logged))
    # The offending KEY is named; its (possibly-secret) VALUE never appears.
    flat = " ".join(str(x) for entry in logged for x in (entry[0], *entry[1]))
    check("error names the offending key", "HYPER_POOL_SIZE" in flat, flat)
    check("error does NOT leak the offending value", "not-an-int" not in flat, flat)


def test_load_env_dotenv() -> None:
    """load_env_settings reads .env file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("# Comment line\n")
        f.write("\n")
        f.write("HYPER_SECRET_KEY=dotenv-secret\n")
        f.write('HYPER_LOG_LEVEL="WARNING"\n')
        f.write("DEBUG=true\n")  # bare name (no HYPER_ prefix)
        f.write("HYPER_POOL_SIZE=10\n")
        dotenv_path = pathlib.Path(f.name)

    try:
        result = load_env_settings(env={}, dotenv_path=dotenv_path)
        check("dotenv SECRET_KEY loaded", result.get("SECRET_KEY") == "dotenv-secret")
        check("dotenv LOG_LEVEL stripped quotes", result.get("LOG_LEVEL") == "WARNING")
        check("dotenv bare name DEBUG", result.get("DEBUG") is True)
        check("dotenv POOL_SIZE coerced", result.get("POOL_SIZE") == 10)
    finally:
        dotenv_path.unlink()


def test_resolve_database_url_single_source() -> None:
    """resolve_database_url is the ONE connection-URL authority.

    In a clean, controlled env each accepted convention resolves in isolation,
    and the documented precedence holds:
        explicit/Django  >  HYPER_DATABASE_URL  >  DATABASE_URL  >  PG*  >  ""
    This guards against the readers re-diverging (server vs CLI vs driver).
    os.environ + DEFAULTS + cwd are mutated and restored so nothing leaks to
    sibling tests.
    """
    import shutil

    db_keys = (
        "HYPER_DATABASE_URL",
        "DATABASE_URL",
        "PGDATABASE",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
    )
    saved_env = {k: os.environ.get(k) for k in db_keys}
    saved_default = DEFAULTS.get("DATABASE_URL")
    saved_cwd = pathlib.Path.cwd()
    tmp = tempfile.mkdtemp()

    def _clear() -> None:
        for k in db_keys:
            os.environ.pop(k, None)
        DEFAULTS["DATABASE_URL"] = ""
        clear_settings_cache()

    try:
        # chdir to a dir with no .env so the file layer can't pollute isolation.
        os.chdir(tmp)

        # (b) only HYPER_DATABASE_URL
        _clear()
        os.environ["HYPER_DATABASE_URL"] = "postgres://h/hyperonly"
        check(
            "resolve: HYPER_DATABASE_URL alone",
            resolve_database_url() == "postgres://h/hyperonly",
        )
        check(
            "get_setting(DATABASE_URL) delegates to resolver",
            get_setting("DATABASE_URL") == "postgres://h/hyperonly",
        )

        # (c) only bare DATABASE_URL (12-factor) — the previously-broken case
        _clear()
        os.environ["DATABASE_URL"] = "postgres://d/bareonly"
        check(
            "resolve: bare DATABASE_URL alone",
            resolve_database_url() == "postgres://d/bareonly",
        )

        # (d) only the libpq PG* set (PGDATABASE + PGHOST + PGUSER)
        _clear()
        os.environ["PGDATABASE"] = "pgdb"
        os.environ["PGHOST"] = "pghost"
        os.environ["PGUSER"] = "pguser"
        check(
            "resolve: PG* set assembled",
            resolve_database_url() == "postgresql://pguser@pghost:5432/pgdb",
            resolve_database_url(),
        )

        # (e) nothing configured -> empty string
        _clear()
        check("resolve: unconfigured -> empty", resolve_database_url() == "")

        # precedence: HYPER_DATABASE_URL > DATABASE_URL > PG*
        _clear()
        os.environ["HYPER_DATABASE_URL"] = "postgres://h/win"
        os.environ["DATABASE_URL"] = "postgres://d/lose"
        os.environ["PGDATABASE"] = "pg_lose"
        check(
            "precedence: HYPER_DATABASE_URL beats DATABASE_URL and PG*",
            resolve_database_url() == "postgres://h/win",
        )

        # precedence: DATABASE_URL > PG*
        _clear()
        os.environ["DATABASE_URL"] = "postgres://d/win"
        os.environ["PGDATABASE"] = "pg_lose"
        check(
            "precedence: DATABASE_URL beats PG*",
            resolve_database_url() == "postgres://d/win",
        )

        # precedence: explicit override (constructor bridge / Django) beats all env
        _clear()
        os.environ["HYPER_DATABASE_URL"] = "postgres://h/lose"
        os.environ["DATABASE_URL"] = "postgres://d/lose"
        DEFAULTS["DATABASE_URL"] = "postgres://explicit/win"
        check(
            "precedence: explicit override beats every env var",
            resolve_database_url() == "postgres://explicit/win",
        )
    finally:
        os.chdir(saved_cwd)
        DEFAULTS["DATABASE_URL"] = saved_default if saved_default is not None else ""
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clear_settings_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_get_all_settings() -> None:
    """get_all_settings returns a complete dict with all setting keys."""
    all_settings = get_all_settings()
    check("get_all_settings returns dict", isinstance(all_settings, dict))
    check(
        "get_all_settings has all DEFAULTS keys",
        all(k in all_settings for k in DEFAULTS),
    )
    check(
        "get_all_settings has 110+ keys",
        len(all_settings) >= 110,
        f"got {len(all_settings)}",
    )

    # Verify some values match defaults
    check("get_all_settings POOL_SIZE default", all_settings["POOL_SIZE"] == 0)
    check("get_all_settings HTTP_SERVER default", all_settings["HTTP_SERVER"] == "auto")
    check(
        "get_all_settings PASSWORD_HASHER default",
        all_settings["PASSWORD_HASHER"] == "argon2id",
    )


def test_i18n_settings() -> None:
    """Verify all 15 internationalization settings exist in DEFAULTS and SETTING_DEFINITIONS."""
    i18n_settings = [
        "LANGUAGE_CODE",
        "TIME_ZONE",
        "USE_TZ",
        "DATE_FORMAT",
        "DATETIME_FORMAT",
        "TIME_FORMAT",
        "SHORT_DATE_FORMAT",
        "SHORT_DATETIME_FORMAT",
        "DECIMAL_SEPARATOR",
        "THOUSAND_SEPARATOR",
        "USE_THOUSAND_SEPARATOR",
        "NUMBER_GROUPING",
        "FIRST_DAY_OF_WEEK",
        "DATE_INPUT_FORMATS",
        "DATETIME_INPUT_FORMATS",
    ]
    check("i18n has 15 settings", len(i18n_settings) == 15)

    for name in i18n_settings:
        check(f"i18n {name} in DEFAULTS", name in DEFAULTS, "missing from DEFAULTS")
        check(
            f"i18n {name} in SETTING_DEFINITIONS",
            name in SETTING_DEFINITIONS,
            "missing from SETTING_DEFINITIONS",
        )

    # Verify defaults via get_setting
    check("get_setting LANGUAGE_CODE", get_setting("LANGUAGE_CODE") == "en")
    check("get_setting TIME_ZONE", get_setting("TIME_ZONE") == "UTC")
    check("get_setting USE_TZ", get_setting("USE_TZ") is True)
    check("get_setting DATE_FORMAT", get_setting("DATE_FORMAT") == "N j, Y")
    check("get_setting DATETIME_FORMAT", get_setting("DATETIME_FORMAT") == "N j, Y, P")
    check("get_setting TIME_FORMAT", get_setting("TIME_FORMAT") == "P")
    check("get_setting SHORT_DATE_FORMAT", get_setting("SHORT_DATE_FORMAT") == "m/d/Y")
    check(
        "get_setting SHORT_DATETIME_FORMAT",
        get_setting("SHORT_DATETIME_FORMAT") == "m/d/Y P",
    )
    check("get_setting DECIMAL_SEPARATOR", get_setting("DECIMAL_SEPARATOR") == ".")
    check("get_setting THOUSAND_SEPARATOR", get_setting("THOUSAND_SEPARATOR") == ",")
    check(
        "get_setting USE_THOUSAND_SEPARATOR",
        get_setting("USE_THOUSAND_SEPARATOR") is False,
    )
    check("get_setting NUMBER_GROUPING", get_setting("NUMBER_GROUPING") == 3)
    check("get_setting FIRST_DAY_OF_WEEK", get_setting("FIRST_DAY_OF_WEEK") == 0)
    check(
        "get_setting DATE_INPUT_FORMATS",
        get_setting("DATE_INPUT_FORMATS") == ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"],
    )
    check(
        "get_setting DATETIME_INPUT_FORMATS",
        get_setting("DATETIME_INPUT_FORMATS")
        == ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S"],
    )

    # Verify SETTING_DEFINITIONS types
    check("LANGUAGE_CODE type is str", SETTING_DEFINITIONS["LANGUAGE_CODE"].type is str)
    check("TIME_ZONE type is str", SETTING_DEFINITIONS["TIME_ZONE"].type is str)
    check("USE_TZ type is bool", SETTING_DEFINITIONS["USE_TZ"].type is bool)
    check(
        "NUMBER_GROUPING type is int",
        SETTING_DEFINITIONS["NUMBER_GROUPING"].type is int,
    )
    check(
        "FIRST_DAY_OF_WEEK type is int",
        SETTING_DEFINITIONS["FIRST_DAY_OF_WEEK"].type is int,
    )
    check(
        "DATE_INPUT_FORMATS type is list",
        SETTING_DEFINITIONS["DATE_INPUT_FORMATS"].type is list,
    )
    check(
        "DATETIME_INPUT_FORMATS type is list",
        SETTING_DEFINITIONS["DATETIME_INPUT_FORMATS"].type is list,
    )

    # Verify range constraints
    check(
        "NUMBER_GROUPING min 0", SETTING_DEFINITIONS["NUMBER_GROUPING"].min_value == 0
    )
    check(
        "NUMBER_GROUPING max 10", SETTING_DEFINITIONS["NUMBER_GROUPING"].max_value == 10
    )
    check(
        "FIRST_DAY_OF_WEEK min 0",
        SETTING_DEFINITIONS["FIRST_DAY_OF_WEEK"].min_value == 0,
    )
    check(
        "FIRST_DAY_OF_WEEK max 6",
        SETTING_DEFINITIONS["FIRST_DAY_OF_WEEK"].max_value == 6,
    )

    # Validate i18n settings pass validation
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = "test-secret-for-i18n"
    errors = validate_settings(settings)
    i18n_errors = [e for e in errors if any(n in e for n in i18n_settings)]
    check(
        "i18n defaults pass validation", len(i18n_errors) == 0, f"errors={i18n_errors}"
    )


def test_new_settings_45() -> None:
    """Verify all 45 new settings exist in DEFAULTS and SETTING_DEFINITIONS with correct defaults."""
    new_settings = {
        # Auth (5)
        "LOGIN_URL": "/login/",
        "LOGIN_REDIRECT_URL": "/",
        "LOGOUT_REDIRECT_URL": "/",
        "PASSWORD_RESET_TIMEOUT": 259200,
        "AUTH_PASSWORD_VALIDATORS": [],
        # CSRF (6)
        "CSRF_TRUSTED_ORIGINS": [],
        "CSRF_COOKIE_DOMAIN": "",
        "CSRF_COOKIE_NAME": "csrftoken",
        "CSRF_COOKIE_PATH": "/",
        "CSRF_COOKIE_AGE": 31449600,
        "CSRF_HEADER_NAME": "X-CSRFToken",
        # Session (4)
        "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
        "SESSION_COOKIE_DOMAIN": "",
        "SESSION_COOKIE_PATH": "/",
        "SESSION_SAVE_EVERY_REQUEST": False,
        # Security (6)
        "SECURE_PROXY_SSL_HEADER": "",
        "SECURE_REDIRECT_EXEMPT": [],
        "SECURE_SSL_HOST": "",
        "SECURE_CROSS_ORIGIN_OPENER_POLICY": "same-origin",
        "SECURE_CSP": {},
        "X_FRAME_OPTIONS": "DENY",
        # Upload (5)
        "DATA_UPLOAD_MAX_NUMBER_FIELDS": 1000,
        "DATA_UPLOAD_MAX_NUMBER_FILES": 100,
        "FILE_UPLOAD_TEMP_DIR": "",
        "FILE_UPLOAD_PERMISSIONS": 0o644,
        "FILE_UPLOAD_DIRECTORY_PERMISSIONS": 0o755,
        # Email (5)
        "EMAIL_SUBJECT_PREFIX": "[HyperDjango] ",
        "EMAIL_TIMEOUT": 30,
        "EMAIL_SSL_CERTFILE": "",
        "EMAIL_SSL_KEYFILE": "",
        "SERVER_EMAIL": "root@localhost",
        # Media (2)
        "MEDIA_URL": "/media/",
        "MEDIA_ROOT": "",
        # Static (1)
        "STATICFILES_DIRS": [],
        # Proxy (2)
        "USE_X_FORWARDED_HOST": False,
        "USE_X_FORWARDED_PORT": False,
        # URL (2)
        "APPEND_SLASH": True,
        "PREPEND_WWW": False,
        # Cache (2)
        "CACHE_KEY_PREFIX": "",
        "CACHE_VERSION": 1,
        # Messages (2)
        "MESSAGE_LEVEL": 20,
        "MESSAGE_TAGS": {},
        # Other (3)
        "ADMINS": [],
        "MANAGERS": [],
        "DISALLOWED_USER_AGENTS": [],
    }

    check(
        "new settings count is 45", len(new_settings) == 45, f"got {len(new_settings)}"
    )

    # Verify all 45 exist in DEFAULTS with correct values
    for name, expected_default in new_settings.items():
        check(
            f"DEFAULTS has {name}",
            name in DEFAULTS,
            "missing from DEFAULTS",
        )
        check(
            f"DEFAULTS[{name}] == {expected_default!r}",
            DEFAULTS.get(name) == expected_default,
            f"got {DEFAULTS.get(name)!r}",
        )

    # Verify all 45 have SettingDefinition entries
    for name in new_settings:
        check(
            f"SETTING_DEFINITIONS has {name}",
            name in SETTING_DEFINITIONS,
            "missing from SETTING_DEFINITIONS",
        )

    # Verify all definitions have matching defaults
    for name, expected_default in new_settings.items():
        if name in SETTING_DEFINITIONS:
            check(
                f"SETTING_DEFINITIONS[{name}].default matches",
                SETTING_DEFINITIONS[name].default == expected_default,
                f"defn={SETTING_DEFINITIONS[name].default!r}, expected={expected_default!r}",
            )

    # ── Type validation for key settings ──
    # X_FRAME_OPTIONS choices
    defn = SETTING_DEFINITIONS["X_FRAME_OPTIONS"]
    check(
        "X_FRAME_OPTIONS has choices",
        defn.choices is not None,
    )
    check(
        "X_FRAME_OPTIONS choices are DENY/SAMEORIGIN/empty",
        defn.choices == frozenset({"DENY", "SAMEORIGIN", ""}),
        f"got {defn.choices}",
    )

    # X_FRAME_OPTIONS invalid choice
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = "test-secret"
    settings["X_FRAME_OPTIONS"] = "ALLOW"
    errors = validate_settings(settings)
    found = any("X_FRAME_OPTIONS" in e and "not one of" in e for e in errors)
    check("X_FRAME_OPTIONS=ALLOW invalid choice", found, f"errors={errors}")

    # PASSWORD_RESET_TIMEOUT must be > 0
    defn = SETTING_DEFINITIONS["PASSWORD_RESET_TIMEOUT"]
    check("PASSWORD_RESET_TIMEOUT min_value is 1", defn.min_value == 1)
    settings2 = dict(DEFAULTS)
    settings2["SECRET_KEY"] = "test-secret"
    settings2["PASSWORD_RESET_TIMEOUT"] = 0
    errors2 = validate_settings(settings2)
    found2 = any(
        "PASSWORD_RESET_TIMEOUT" in e and "below minimum" in e for e in errors2
    )
    check("PASSWORD_RESET_TIMEOUT=0 below min", found2, f"errors={errors2}")

    # CSRF_COOKIE_AGE min_value is 0
    defn = SETTING_DEFINITIONS["CSRF_COOKIE_AGE"]
    check("CSRF_COOKIE_AGE type is int", defn.type is int)
    check("CSRF_COOKIE_AGE min_value is 0", defn.min_value == 0)

    # EMAIL_TIMEOUT range
    defn = SETTING_DEFINITIONS["EMAIL_TIMEOUT"]
    check("EMAIL_TIMEOUT min_value is 1", defn.min_value == 1)
    check("EMAIL_TIMEOUT max_value is 300", defn.max_value == 300)

    # CACHE_VERSION min_value
    defn = SETTING_DEFINITIONS["CACHE_VERSION"]
    check("CACHE_VERSION min_value is 1", defn.min_value == 1)

    # SECURE_CROSS_ORIGIN_OPENER_POLICY choices
    defn = SETTING_DEFINITIONS["SECURE_CROSS_ORIGIN_OPENER_POLICY"]
    check(
        "COOP has correct choices",
        defn.choices
        == frozenset({"same-origin", "same-origin-allow-popups", "unsafe-none"}),
        f"got {defn.choices}",
    )

    # DATA_UPLOAD_MAX_NUMBER_FIELDS min_value
    defn = SETTING_DEFINITIONS["DATA_UPLOAD_MAX_NUMBER_FIELDS"]
    check("DATA_UPLOAD_MAX_NUMBER_FIELDS min is 1", defn.min_value == 1)

    # Verify the two registries stay in sync — the actual invariant.
    # No hard-coded count: telemetry / other feature work will continue
    # to add settings, and the test should grow with them automatically.
    total = len(DEFAULTS)
    total_defs = len(SETTING_DEFINITIONS)
    check(
        "DEFAULTS and SETTING_DEFINITIONS have the same key count",
        total == total_defs,
        f"DEFAULTS={total}, SETTING_DEFINITIONS={total_defs}",
    )
    check(
        "DEFAULTS has at least 118 entries",
        total >= 118,
        f"got {total}",
    )

    # All new settings pass validation with defaults
    settings3 = dict(DEFAULTS)
    settings3["SECRET_KEY"] = "test-secret-for-new-settings"
    errors3 = validate_settings(settings3)
    new_errors = [e for e in errors3 if any(n in e for n in new_settings)]
    check(
        "all new settings pass validation", len(new_errors) == 0, f"errors={new_errors}"
    )


def test_config_authority_settings() -> None:
    """Settings converted from ad-hoc os.environ reads: registered with correct
    defaults, resolved from HYPER_<NAME> via get_setting, plus the two non-trivial
    behaviors (WEBSOCKET_CONCURRENCY mode selection and fill_url_auth completion)."""
    import os

    import hyperdjango.conf as _conf
    from hyperdjango.conf import fill_url_auth

    new = {
        "LISTEN_BACKLOG": 4096,
        "SEND_TIMEOUT_MS": 30000,
        "CURSOR_SECRET": "",
        "SUPERUSER_PASSWORD": "",
        "COMMANDS": "",
    }
    for name, default in new.items():
        check(f"DEFAULTS has {name}", name in DEFAULTS, "missing from DEFAULTS")
        check(
            f"DEFAULTS[{name}] == {default!r}",
            DEFAULTS.get(name) == default,
            f"got {DEFAULTS.get(name)!r}",
        )
        check(
            f"SETTING_DEFINITIONS has {name}",
            name in SETTING_DEFINITIONS,
            "missing from SETTING_DEFINITIONS",
        )

    def _rescan() -> None:
        _conf._ENV_OVERRIDES.clear()
        _conf._ENV_OVERRIDES_POPULATED = False

    # get_setting resolves each new setting from its HYPER_<NAME> env var, with
    # the definition's type coercion applied.
    env_cases = {
        "HYPER_LISTEN_BACKLOG": ("LISTEN_BACKLOG", "2048", 2048),
        "HYPER_SEND_TIMEOUT_MS": ("SEND_TIMEOUT_MS", "0", 0),
        "HYPER_WEBSOCKET_LOOP_COUNT": ("WEBSOCKET_LOOP_COUNT", "4", 4),
        "HYPER_CURSOR_SECRET": ("CURSOR_SECRET", "sekret-value", "sekret-value"),
        "HYPER_COMMANDS": ("COMMANDS", "a.cmds,b.cmds", "a.cmds,b.cmds"),
    }
    saved = {k: os.environ.get(k) for k in env_cases}
    try:
        for env_key, (_name, raw, _expected) in env_cases.items():
            os.environ[env_key] = raw
        _rescan()
        for env_key, (name, _raw, expected) in env_cases.items():
            check(
                f"get_setting({name}) from {env_key}",
                get_setting(name) == expected,
                f"got {get_setting(name)!r}",
            )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _rescan()

    # WEBSOCKET_CONCURRENCY mode selection: "thread" (any case) forces
    # one-thread-per-connection; "shared"/unset/anything else is the shared pool.
    from hyperdjango.websocket import _ws_concurrency_mode

    ws_saved = os.environ.get("HYPER_WEBSOCKET_CONCURRENCY")
    try:
        for raw, expected in (
            ("thread", "thread"),
            ("Thread", "thread"),
            ("shared", "shared"),
            ("", "shared"),
        ):
            if raw == "":
                os.environ.pop("HYPER_WEBSOCKET_CONCURRENCY", None)
            else:
                os.environ["HYPER_WEBSOCKET_CONCURRENCY"] = raw
            _rescan()
            got = _ws_concurrency_mode()
            check(
                f"WEBSOCKET_CONCURRENCY={raw!r} -> {expected}",
                got == expected,
                f"got {got!r}",
            )
    finally:
        if ws_saved is None:
            os.environ.pop("HYPER_WEBSOCKET_CONCURRENCY", None)
        else:
            os.environ["HYPER_WEBSOCKET_CONCURRENCY"] = ws_saved
        _rescan()

    # fill_url_auth: conf is the SOLE reader of PG*/OS to complete a bare DB URL.
    pg_keys = ("PGUSER", "PGPASSWORD", "PGHOST", "PGPORT", "USER", "USERNAME")
    pg_saved = {k: os.environ.get(k) for k in pg_keys}
    try:
        for k in pg_keys:
            os.environ.pop(k, None)
        os.environ["PGUSER"] = "alice"
        os.environ["PGHOST"] = "db.internal"
        os.environ["PGPORT"] = "6432"
        filled = fill_url_auth("postgres:///mydb")
        check(
            "fill_url_auth completes bare URL from PG*",
            filled == "postgres://alice@db.internal:6432/mydb",
            f"got {filled!r}",
        )
        complete = "postgres://bob:pw@h:5432/db"
        check(
            "fill_url_auth leaves a complete URL unchanged",
            fill_url_auth(complete) == complete,
            f"got {fill_url_auth(complete)!r}",
        )
        check("fill_url_auth empty passthrough", fill_url_auth("") == "")
    finally:
        for k, v in pg_saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_coerce_edge_cases() -> None:
    """Test _coerce_value edge cases."""
    check("coerce empty string to list", _coerce_value("", list) == [])
    check("coerce whitespace to list", _coerce_value("  ", list) == [])
    check("coerce single item list", _coerce_value("one", list) == ["one"])
    check("coerce bool 0 -> False", _coerce_value("0", bool) is False)
    check("coerce bool empty -> False", _coerce_value("", bool) is False)
    check("coerce bool TRUE (case) -> True", _coerce_value("TRUE", bool) is True)
    check("coerce str passthrough", _coerce_value("hello", str) == "hello")


def test_env_file_parsing() -> None:
    """Test _parse_env_file with edge cases."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("KEY1=value1\n")
        f.write("KEY2='single quoted'\n")
        f.write('KEY3="double quoted"\n')
        f.write("# full line comment\n")
        f.write("  \n")  # blank with whitespace
        f.write("KEY4=value with spaces\n")
        f.write("NO_EQUALS_LINE\n")
        f.write("KEY5=\n")  # empty value
        dotenv_path = pathlib.Path(f.name)

    try:
        result = _parse_env_file(dotenv_path)
        check("parse KEY1", result.get("KEY1") == "value1")
        check("parse KEY2 single quoted", result.get("KEY2") == "single quoted")
        check("parse KEY3 double quoted", result.get("KEY3") == "double quoted")
        check("parse KEY4 with spaces", result.get("KEY4") == "value with spaces")
        check("parse skips no-equals", "NO_EQUALS_LINE" not in result)
        check("parse KEY5 empty", result.get("KEY5") == "")
        check("parse comment skipped", "#" not in "".join(result.keys()))
    finally:
        dotenv_path.unlink()

    # Nonexistent file returns empty dict
    result2 = _parse_env_file(pathlib.Path("/nonexistent/path/.env"))
    check("parse nonexistent file", result2 == {})


def test_validate_debug_mode() -> None:
    """SECRET_KEY auto-generated when DEBUG=True and not set."""
    settings = dict(DEFAULTS)
    settings["SECRET_KEY"] = ""
    settings["DEBUG"] = True
    errors = validate_settings(settings)
    secret_errors = [e for e in errors if "SECRET_KEY" in e and "required" in e]
    check(
        "no SECRET_KEY error in DEBUG mode", len(secret_errors) == 0, f"errors={errors}"
    )

    # Verify SECRET_KEY was auto-generated
    check(
        "SECRET_KEY auto-generated in DEBUG",
        isinstance(settings["SECRET_KEY"], str) and len(settings["SECRET_KEY"]) == 64,
        f"got SECRET_KEY={settings['SECRET_KEY']!r}",
    )

    # Verify auto-generated key is hex (token_hex output)
    try:
        int(settings["SECRET_KEY"], 16)
        is_hex = True
    except ValueError:
        is_hex = False
    check("SECRET_KEY auto-generated is hex", is_hex)

    # Two calls produce different keys (random)
    settings2 = dict(DEFAULTS)
    settings2["SECRET_KEY"] = ""
    settings2["DEBUG"] = True
    validate_settings(settings2)
    check(
        "SECRET_KEY auto-generated is unique per call",
        settings["SECRET_KEY"] != settings2["SECRET_KEY"],
        f"both got {settings['SECRET_KEY']!r}",
    )

    # But other validation still applies
    settings3 = dict(DEFAULTS)
    settings3["SECRET_KEY"] = ""
    settings3["DEBUG"] = True
    settings3["THREAD_POOL_SIZE"] = 999
    errors = validate_settings(settings3)
    found = any("THREAD_POOL_SIZE" in e for e in errors)
    check("other validation still runs in DEBUG", found, f"errors={errors}")

    # Production mode still requires SECRET_KEY
    settings4 = dict(DEFAULTS)
    settings4["SECRET_KEY"] = ""
    settings4["DEBUG"] = False
    errors4 = validate_settings(settings4)
    found4 = any("SECRET_KEY" in e and "required" in e for e in errors4)
    check("SECRET_KEY still required in production", found4, f"errors={errors4}")


if __name__ == "__main__":
    main()
