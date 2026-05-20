"""Suite-local e2e entrypoint: port registry, seed credentials, and a re-export
of the generic HTTP/WebSocket primitives now living in ``hyperdjango.testkit``.

The reusable machinery (``AppRunner``, ``Session``, ``E2EResponse``, ``http_*``,
``sse_post``, ``build_multipart``) is framework surface and lives in
``hyperdjango.testkit.e2e``; new tests should import it from there. This module
keeps what is genuinely suite-local — the ``TEST_PORTS`` registry and the seed
credential constants — and re-exports the testkit primitives so the existing
``from e2e_helper import ...`` call sites keep working unchanged.

Usage:
    from e2e_helper import AppRunner, TEST_PORTS, http_get

    with AppRunner("services.rest_api.app:app", port=TEST_PORTS["rest_api"]) as r:
        status, headers, body = http_get(r.url("/health"))
"""

import os

from hyperdjango.services_registry import (
    SERVICES,
)
from hyperdjango.services_registry import (
    app_path as service_app,
)
from hyperdjango.services_registry import (
    seed_path as service_seed,
)
from hyperdjango.testkit.e2e import (
    AppRunner,
    E2EResponse,
    Session,
    _http_request,
    _kill_port,
    _stream_pipe,
    build_multipart,
    http_delete,
    http_get,
    http_post,
    http_put,
    sse_post,
)

__all__ = [
    "ADMIN_PASSWORD",
    "SERVICES",
    "SEED_PASSWORD",
    "TEST_PORTS",
    "AppRunner",
    "E2EResponse",
    "Session",
    "_http_request",
    "_kill_port",
    "_stream_pipe",
    "build_multipart",
    "service_app",
    "service_seed",
    "http_delete",
    "http_get",
    "http_post",
    "http_put",
    "sse_post",
]

# ── Service app + seed paths ─────────────────────────────────────────────────
# App and seed import paths are NOT redeclared here. They live in
# ``hyperdjango.services_registry``, the single source of truth shared with the
# ``hyper service`` CLI verb, and are re-exported so tests can write:
#
#     from e2e_helper import AppRunner, TEST_PORTS, service_app, service_seed
#
#     subprocess.run(["uv", "run", "hyper", "setup",
#                     "--app", service_app("bookstore_api"), "--drop",
#                     "--seed", service_seed("bookstore_api")])
#     with AppRunner(service_app("bookstore_api"),
#                    port=TEST_PORTS["bookstore_api"]) as runner:
#         ...
#
# A test that hardcodes "services.x.app:app" still works, but the registry is
# where a path change should be made.

# ── Port allocation ──────────────────────────────────────────────────────────
# Central registry of TEST ports — a suite-local concern, deliberately NOT in
# the shared registry: a test port has to survive parallel suite execution,
# while a service's default port only has to be pleasant to type. The two
# ranges are disjoint by construction (tests 18100-19260, `hyper service run`
# 8600-8699) so a developer serving a service never collides with a test run;
# ``scripts/test_services_registry.py`` enforces that.
#
# Usage: from e2e_helper import TEST_PORTS
#        AppRunner(..., port=TEST_PORTS["rest_api"])

# HTTP client timeout for e2e service clients.
#
# A service client's default (5s) answers "is the server reachable" for a
# PRODUCTION caller on a machine with cores to spare. An e2e runner is not
# that: CI reports "3 usable core(s) -> workers=24", and two e2e files each
# holding a 24-thread server on three cores means a perfectly healthy request
# can take longer than five seconds to be scheduled, answered and returned.
# One such request then fails a whole file, on one runner, while five others
# pass the identical code — which is a statement about the runner, not the
# server. This is deliberately generous: it bounds "the server never answered"
# and asserts nothing about latency, so a longer ceiling can only ever wait
# longer for a thing that should happen. Latency claims belong in the
# benchmarks, which measure on a quiet machine for exactly this reason.
E2E_CLIENT_TIMEOUT = 30.0

TEST_PORTS = {
    # Service e2e tests
    "hyperai": 18100,
    "hyperai_workflow": 18110,
    "rest_api": 18200,
    "rest_api_workflow": 18210,
    "hypernews": 18300,
    "hypernews_workflow": 18310,
    "full_stack": 18400,
    "voting_system": 18410,
    "security_rest": 18500,
    "security_hypernews": 18510,
    "security_hyperai": 18520,
    "load_rest": 18600,
    "load_hypernews": 18610,
    "load_hello": 18620,
    "performance": 18710,
    "websocket_chat": 18800,
    "websocket_stress": 18810,
    "websocket_shared_loops": 18811,
    "websocket_rfc_hardening": 18812,
    "websocket_protocol_fuzz": 18813,
    "unicode_trace": 18815,
    "http_reactor_scaling_reactor": 18816,
    "http_reactor_scaling_threaded": 18817,
    "semantic_search": 18820,
    "openapi": 18900,
    "task_queue": 18910,
    "multi_tenant": 18920,
    "deployment": 18930,
    "hello": 18940,
    "benchmark_app": 18950,
    "content_hub": 18960,
    "forms_demo": 18970,
    "graceful_shutdown": 18980,
    "hypernews_forums": 18990,
    "hypernews_social": 19000,
    "hypernews_p2": 19010,
    "hypernews_p3": 19020,
    "cookie_security_hn": 19030,
    "cookie_security_ai": 19040,
    "hyperticket": 19050,
    "hyperticket_app": 19060,
    "load_orm": 19070,
    "ws_fuzz": 19080,
    "notes_api": 19090,
    "bookstore_api": 19100,
    "blog_platform": 19110,
    "cms_lite": 19120,
    "metering_api": 19130,
    "content_length": 19140,
    "native_protocol_fuzz": 19160,
    "native_db_route": 19170,
    "hypersecret": 19180,
    "hypermanager": 19190,
    "hypersecret_live": 19200,
    "hypersecret_live_manager": 19210,
    "hypersecret_live_mtls": 19220,
    "hypermanager_mtls": 19230,
    "live_config": 19240,
    "live_config_manager": 19250,
    "live_config_secret": 19260,
    # Benchmarks (not parallel with tests, but reserved)
    "bench_hello": 18700,
    "bench_benchmark_app": 18710,
    "bench_rest": 18720,
    "bench_hypernews": 18730,
    "bench_hypernews_wrk": 18750,
}

# Seed password for E2E tests — matches HYPER_SEED_PASSWORD set by test_runner.py.
# All seed files use seed_password() which resolves this env var.
# env-boundary: suite-local seed credential, not a framework configuration read.
SEED_PASSWORD = os.environ.get("HYPER_SEED_PASSWORD", "test-seed-password")

# Admin panel password (hyper_users) — matches HYPER_ADMIN_PASSWORD set by test_runner.py.
# ensure_admin_user() resolves this via get_setting("ADMIN_PASSWORD").
# env-boundary: suite-local seed credential, not a framework configuration read.
ADMIN_PASSWORD = os.environ.get("HYPER_ADMIN_PASSWORD", "test-admin-password")
