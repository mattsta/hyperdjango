#!/usr/bin/env python3
"""Version cohort routing + operator-owned reload policy.

Covers the pieces of the cohort/reload-policy system that the older
``test_versioning.py`` (AppVersion + script builders) and
``test_version_middleware.py`` (header/injection/router basics) do not:

- ``versioning.client_version()`` adversarial parsing — header-over-cookie
  precedence, CRLF sanitization, the 128-char cap, percent-decoded cookies.
- ``VersionMiddleware`` end-to-end through a real ``HyperApp`` +
  ``TestClient``: the ``X-App-Version-Action`` policy header, the
  ``request.client_version`` handoff to handlers, and the
  ``hyperdjango_version_skew_requests_total{relation}`` classification.
- The explicit Content-Encoding skip (never splice a ``<script>`` into
  gzipped bytes).
- Startup validation through the generic platform extension point:
  ``MiddlewareStack.validate()`` dispatching to every ``StackValidator``, and
  ``VersionMiddleware.validate_stack()`` rejecting any ``BodyEncoder``
  positioned to encode the body first. Plus the ``APP_VERSION_MISMATCH`` enum,
  now enforced at middleware construction.
- ``VersionRouterMiddleware`` hostile-input paths (header injection through
  the routing map / 409 detail, percent-decoded cookie routing).
- The ``hyper:version-mismatch`` DOM event contract across policies.

Metric assertions are DELTAS against a pre-action scrape: the CounterVec is
a module-level singleton shared with every other test in this process.

No database, no native server.

# hyper-test: unit

Usage:
    uv run hyper-test version_cohort
"""

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

from hyperdjango import HyperApp, Response
from hyperdjango.conf import DEFAULTS
from hyperdjango.request import Request
from hyperdjango.standalone_middleware import (
    BodyEncoder,
    CompressionMiddleware,
    MiddlewareStack,
    StackValidator,
    VersionMiddleware,
    VersionRouterMiddleware,
)
from hyperdjango.telemetry import disable, enable
from hyperdjango.telemetry.metrics import collect_prometheus_text
from hyperdjango.testing import TestClient
from hyperdjango.testkit import check, finish, run_main
from hyperdjango.versioning import (
    VERSION_ACTIONS,
    AppVersion,
    client_version,
    get_client_script,
    set_app_version,
)

_SKEW_METRIC = "hyperdjango_version_skew_requests_total"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_request(headers: dict[str, str] | None = None, cookie: str = "") -> Request:
    """A Request whose cookies come from a real Cookie header (native parse)."""
    hdrs = dict(headers or {})
    if cookie:
        hdrs["cookie"] = cookie
    return Request(method="GET", path="/", headers=hdrs, query_string="", body=b"")


async def html_handler(request: Request) -> Response:
    return Response(
        body=b"<html><head><title>T</title></head><body><h1>Hi</h1></body></html>",
        status=200,
        content_type="text/html; charset=utf-8",
    )


async def gzipped_html_handler(request: Request) -> Response:
    """HTML that a compressor already touched — injection must stand down."""
    resp = await html_handler(request)
    resp.headers["Content-Encoding"] = "gzip"
    return resp


async def json_handler(request: Request) -> Response:
    return Response.json({"ok": True})


def _series_value(text: str, name: str, labels: dict[str, str] | None = None) -> float:
    """Value of ``name{labels}`` in a Prometheus exposition, label-order agnostic.

    Returns 0.0 when the series is absent, so a not-yet-bumped counter still
    gives an arithmetic baseline (same parser shape as the telemetry suite).
    """
    for line in text.splitlines():
        if line.startswith("#") or not line.strip() or not line.startswith(name):
            continue
        head, _, value_str = line.rpartition(" ")
        if labels is None:
            if head != name:
                continue
        else:
            if not head.startswith(name + "{") or not head.endswith("}"):
                continue
            parts: dict[str, str] = {}
            for kv in head[len(name) + 1 : -1].split(","):
                if "=" not in kv:
                    continue
                k, v = kv.split("=", 1)
                parts[k.strip()] = v.strip().strip('"')
            if any(parts.get(k) != v for k, v in labels.items()):
                continue
        try:
            return float(value_str)
        except ValueError:
            return 0.0
    return 0.0


def _skew_counts() -> dict[str, float]:
    text = collect_prometheus_text().decode("utf-8")
    return {
        rel: _series_value(text, _SKEW_METRIC, {"relation": rel})
        for rel in ("match", "skew", "unversioned")
    }


def _build_client(version: str, **settings: object) -> TestClient:
    """A real HyperApp behind VersionMiddleware, built under `settings`.

    The middleware snapshots its policy in ``__post_init__``, so it must be
    constructed inside the patch context.
    """
    av = AppVersion()
    av.set_explicit(version)
    set_app_version(av)
    app = HyperApp()
    with patch.dict(DEFAULTS, settings):
        app.use(VersionMiddleware())

    @app.get("/html")
    async def _html(request):
        return await html_handler(request)

    @app.get("/echo")
    async def _echo(request):
        return Response.json({"client_version": request.client_version})

    return TestClient(app)


# ---------------------------------------------------------------------------
# 1. client_version() — adversarial parsing
# ---------------------------------------------------------------------------


def check_client_version_parsing() -> None:
    check(
        "client_version: header wins over cookie",
        client_version(
            make_request({"x-client-version": "v2"}, "hyper_client_version=v1")
        )
        == "v2",
    )
    check(
        "client_version: cookie-only path works",
        client_version(make_request(cookie="hyper_client_version=v1")) == "v1",
    )
    check(
        "client_version: neither header nor cookie -> ''",
        client_version(make_request()) == "",
    )
    check(
        "client_version: empty header falls back to the cookie",
        client_version(
            make_request({"x-client-version": ""}, "hyper_client_version=vc")
        )
        == "vc",
    )
    check(
        "client_version: header lookup is case-insensitive",
        client_version(make_request({"X-Client-Version": "vU"})) == "vU",
    )

    crlf = client_version(make_request({"x-client-version": "v1\r\nX-Injected: pwned"}))
    check(
        "client_version: CRLF stripped from the header value",
        "\r" not in crlf and "\n" not in crlf,
        f"got {crlf!r}",
    )
    check(
        "client_version: a header-split attempt truncates at the CRLF "
        "(nothing after the split is part of the value)",
        crlf == "v1",
        f"got {crlf!r}",
    )
    check(
        "client_version: C0 controls and DEL are stripped",
        client_version(make_request({"x-client-version": "v\t1\x00b\x7f"})) == "v1b",
    )
    crlf_cookie = client_version(
        make_request(cookie="hyper_client_version=v1%0d%0aX-Injected:%20pwned")
    )
    check(
        "client_version: CRLF stripped from a percent-encoded cookie value",
        "\r" not in crlf_cookie and "\n" not in crlf_cookie,
        f"got {crlf_cookie!r}",
    )

    long_value = client_version(make_request({"x-client-version": "a" * 500}))
    check(
        "client_version: >128-char header capped at 128",
        len(long_value) == 128,
        f"got len {len(long_value)}",
    )
    long_cookie = client_version(
        make_request(cookie="hyper_client_version=" + "b" * 500)
    )
    check(
        "client_version: >128-char cookie capped at 128",
        len(long_cookie) == 128,
        f"got len {len(long_cookie)}",
    )
    capped = client_version(
        make_request({"x-client-version": "a" * 127 + "\r\n" + "b" * 50})
    )
    check(
        "client_version: sanitization (CRLF truncation) runs before the cap",
        capped == "a" * 127,
        f"got {capped!r}",
    )

    check(
        "client_version: percent-encoded cookie value is decoded",
        client_version(make_request(cookie="hyper_client_version=v1%2Fbeta"))
        == "v1/beta",
    )
    check(
        "client_version: percent-encoded space in a cookie decodes",
        client_version(make_request(cookie="hyper_client_version=a%20b")) == "a b",
    )
    check(
        "client_version: honors custom header/cookie names",
        client_version(
            make_request({"x-cohort": "vX"}, "my_cohort=vY"),
            header_name="x-cohort",
            cookie_name="my_cohort",
        )
        == "vX",
    )
    check(
        "client_version: custom cookie name falls back when header absent",
        client_version(
            make_request(cookie="my_cohort=vY"),
            header_name="x-cohort",
            cookie_name="my_cohort",
        )
        == "vY",
    )
    check(
        "client_version: the default names are ignored when custom ones are given",
        client_version(
            make_request({"x-client-version": "vDefault"}, "hyper_client_version=vD2"),
            header_name="x-cohort",
            cookie_name="my_cohort",
        )
        == "",
    )


# ---------------------------------------------------------------------------
# 2. VersionMiddleware end-to-end (real app + TestClient)
# ---------------------------------------------------------------------------


def check_middleware_end_to_end() -> None:
    try:
        client = _build_client(
            "e2e-v1", APP_VERSION_HEADER=True, APP_VERSION_MISMATCH="prompt"
        )
        resp = client.get("/html")
        check(
            "e2e: HTML response carries X-App-Version",
            resp.headers.get("x-app-version") == "e2e-v1",
        )
        check(
            "e2e: X-App-Version-Action advertises APP_VERSION_MISMATCH (prompt)",
            resp.headers.get("x-app-version-action") == "prompt",
        )
        body = resp.text()
        check(
            "e2e: injected version tag + client API reach the browser",
            'window.__hyperAppVersion="e2e-v1"' in body
            and "window.hyperVersion" in body,
        )
        check(
            "e2e: injected body script carries the mismatch listener",
            "htmx:afterRequest" in body and "hyper:version-mismatch" in body,
        )
        check(
            "e2e: content-length matches the injected body",
            int(resp.headers.get("content-length", "0")) == len(resp.body),
        )
    finally:
        set_app_version(None)

    try:
        client = _build_client(
            "e2e-v1", APP_VERSION_HEADER=True, APP_VERSION_MISMATCH="warn"
        )
        resp = client.get("/echo")
        check(
            "e2e: the action header tracks the setting (warn), also on JSON",
            resp.headers.get("x-app-version-action") == "warn",
        )
    finally:
        set_app_version(None)

    # request.client_version handoff — asserted from INSIDE a handler.
    try:
        client = _build_client(
            "e2e-v1",
            APP_VERSION_HEADER=True,
            APP_VERSION_MISMATCH="prompt",
            APP_VERSION_CLIENT_BROADCAST=True,
        )
        got = client.get("/echo", headers={"x-client-version": "e2e-v0"}).json()
        check(
            "e2e: X-Client-Version lands on request.client_version",
            got["client_version"] == "e2e-v0",
            f"got {got!r}",
        )
        got = client.get(
            "/echo", headers={"cookie": "hyper_client_version=e2e-cookie"}
        ).json()
        check(
            "e2e: cookie cohort lands on request.client_version",
            got["client_version"] == "e2e-cookie",
            f"got {got!r}",
        )
        got = client.get("/echo").json()
        check(
            "e2e: request.client_version is '' when the client sent nothing",
            got["client_version"] == "",
            f"got {got!r}",
        )
    finally:
        set_app_version(None)

    try:
        client = _build_client(
            "e2e-v1",
            APP_VERSION_HEADER=True,
            APP_VERSION_MISMATCH="prompt",
            APP_VERSION_CLIENT_BROADCAST=False,
        )
        got = client.get("/echo", headers={"x-client-version": "e2e-v0"}).json()
        check(
            "e2e: broadcast off -> no inbound parse, client_version stays ''",
            got["client_version"] == "",
            f"got {got!r}",
        )
        body = client.get("/html").text()
        check(
            "e2e: broadcast off -> no cohort header/cookie machinery in the page",
            "htmx:configRequest" not in body and "hyper_client_version=" not in body,
        )
    finally:
        set_app_version(None)


# ---------------------------------------------------------------------------
# 3. Cohort skew metric — deltas, never absolutes
# ---------------------------------------------------------------------------


def check_skew_metric() -> None:
    enable()
    try:
        try:
            client = _build_client(
                "metric-v2",
                APP_VERSION_HEADER=True,
                APP_VERSION_MISMATCH="prompt",
                APP_VERSION_CLIENT_BROADCAST=True,
            )
            before = _skew_counts()
            client.get("/echo", headers={"x-client-version": "metric-v2"})
            after = _skew_counts()
            check(
                "skew metric: same version -> relation=match +1",
                after["match"] - before["match"] == 1.0,
                f"{before} -> {after}",
            )
            check(
                "skew metric: a match does not bump skew/unversioned",
                after["skew"] == before["skew"]
                and after["unversioned"] == before["unversioned"],
            )

            before = _skew_counts()
            client.get("/echo", headers={"x-client-version": "metric-v1"})
            after = _skew_counts()
            check(
                "skew metric: stale client version -> relation=skew +1",
                after["skew"] - before["skew"] == 1.0,
                f"{before} -> {after}",
            )

            before = _skew_counts()
            client.get("/echo")
            after = _skew_counts()
            check(
                "skew metric: no client version -> relation=unversioned +1",
                after["unversioned"] - before["unversioned"] == 1.0,
                f"{before} -> {after}",
            )

            before = _skew_counts()
            client.get("/echo", headers={"cookie": "hyper_client_version=metric-v2"})
            after = _skew_counts()
            check(
                "skew metric: cookie cohort classifies as match too",
                after["match"] - before["match"] == 1.0,
                f"{before} -> {after}",
            )
        finally:
            set_app_version(None)

        try:
            client = _build_client(
                "metric-v2",
                APP_VERSION_HEADER=True,
                APP_VERSION_MISMATCH="prompt",
                APP_VERSION_CLIENT_BROADCAST=False,
            )
            before = _skew_counts()
            client.get("/echo", headers={"x-client-version": "metric-v1"})
            client.get("/echo")
            after = _skew_counts()
            check(
                "skew metric: broadcast off -> no relation is bumped at all",
                after == before,
                f"{before} -> {after}",
            )
        finally:
            set_app_version(None)
    finally:
        disable()


# ---------------------------------------------------------------------------
# 4. Content-Encoding responses are never injected into
# ---------------------------------------------------------------------------


async def _encoded_response_checks() -> None:
    av = AppVersion()
    av.set_explicit("gz-v1")
    set_app_version(av)
    try:
        with patch.dict(
            DEFAULTS,
            {"APP_VERSION_HEADER": True, "APP_VERSION_MISMATCH": "prompt"},
        ):
            mw = VersionMiddleware()

        plain = await mw(make_request(), html_handler)
        original = plain.body
        check(
            "content-encoding: the same HTML IS injected when unencoded (control)",
            b"__hyperAppVersion" in original,
        )

        encoded = await mw(make_request(), gzipped_html_handler)
        untouched = (await gzipped_html_handler(make_request())).body
        check(
            "content-encoding: gzipped body passes through byte-identical",
            encoded.body == untouched,
            f"len {len(encoded.body)} vs {len(untouched)}",
        )
        check(
            "content-encoding: no script spliced into encoded bytes",
            b"__hyperAppVersion" not in encoded.body
            and b"hyper:version-mismatch" not in encoded.body,
        )
        check(
            "content-encoding: version header still emitted on the skip path",
            encoded.headers.get("x-app-version") == "gz-v1",
        )
        check(
            "content-encoding: action header still emitted on the skip path",
            encoded.headers.get("x-app-version-action") == "prompt",
        )
        check(
            "content-encoding: no content-length rewritten on the skip path",
            "content-length" not in encoded.headers,
        )

        async def empty_encoding_handler(request):
            resp = await html_handler(request)
            resp.headers["content-encoding"] = ""
            return resp

        empty_enc = await mw(make_request(), empty_encoding_handler)
        check(
            "content-encoding: an EMPTY content-encoding is not a skip",
            b"__hyperAppVersion" in empty_enc.body,
        )
    finally:
        set_app_version(None)


# ---------------------------------------------------------------------------
# 5. Startup validation via the generic platform extension point
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ThirdPartyEncoder(BodyEncoder):
    """A body encoder the platform (and VersionMiddleware) never heard of.

    Proves the ordering check keys off the ``BodyEncoder`` CAPABILITY, not off
    ``CompressionMiddleware`` by name.
    """

    async def __call__(self, request, call_next):
        return await call_next(request)


async def _passthrough(request, call_next):
    """A plain function middleware — implements neither marker."""
    return await call_next(request)


def _validate(middlewares: list[object]) -> Exception | None:
    """Drive the platform dispatch over a stack; return the raised error.

    ``HyperApp.run()`` calls exactly this — ``self._middleware.validate()`` —
    so exercising ``MiddlewareStack`` is exercising the startup path.
    """
    stack = MiddlewareStack()
    for mw in middlewares:
        stack.add(mw)
    try:
        stack.validate()
    except RuntimeError as exc:
        return exc
    return None


def check_startup_validation() -> None:
    with patch.dict(DEFAULTS, {"APP_VERSION_MISMATCH": "prompt"}):
        compression = CompressionMiddleware()
        version = VersionMiddleware()
    encoder = _ThirdPartyEncoder()

    # --- the contract itself: two generic markers ---------------------------
    check(
        "contract: CompressionMiddleware declares the BodyEncoder capability",
        isinstance(compression, BodyEncoder),
    )
    check(
        "contract: VersionMiddleware declares the StackValidator capability",
        isinstance(version, StackValidator),
    )
    check(
        "contract: the markers are independent (Version is no BodyEncoder)",
        not isinstance(version, BodyEncoder)
        and not isinstance(compression, StackValidator),
    )
    check(
        "contract: markers are empty slots — no __dict__ leaks into plugins",
        not hasattr(compression, "__dict__") and not hasattr(version, "__dict__"),
    )
    check(
        "contract: StackValidator cannot be instantiated unimplemented",
        _abstract_rejects(),
    )

    # --- generic dispatch ----------------------------------------------------
    err = _validate([compression, version])
    check(
        "startup: BodyEncoder registered BEFORE Version boots fine",
        err is None,
        f"raised {err!r}",
    )

    err = _validate([version, compression])
    check(
        "startup: Version before a BodyEncoder raises RuntimeError",
        isinstance(err, RuntimeError),
        f"raised {err!r}",
    )
    message = str(err) if err else ""
    check(
        "startup: the order error names the capability AND the offender",
        "BodyEncoder" in message
        and "VersionMiddleware" in message
        and "CompressionMiddleware" in message,
        message,
    )
    check(
        "startup: the order error says how to fix it",
        "app.use(CompressionMiddleware" in message,
        message,
    )

    check(
        "startup: VersionMiddleware alone is fine",
        _validate([version]) is None,
    )
    check(
        "startup: CompressionMiddleware alone is fine",
        _validate([compression]) is None,
    )
    check(
        "startup: no middleware at all is fine",
        _validate([]) is None,
    )
    check(
        "startup: middlewares implementing neither marker are ignored",
        _validate([_passthrough, version]) is None,
    )

    # --- capability-based, not name-based ------------------------------------
    err = _validate([version, encoder])
    check(
        "startup: an UNKNOWN BodyEncoder trips the same ordering error",
        isinstance(err, RuntimeError),
        f"raised {err!r}",
    )
    check(
        "startup: the error names the third-party encoder, not the compressor",
        "_ThirdPartyEncoder" in str(err) if err else False,
        str(err),
    )
    check(
        "startup: an unknown BodyEncoder registered first is fine",
        _validate([encoder, version]) is None,
    )
    check(
        "startup: every BodyEncoder must precede Version (one trailing = error)",
        isinstance(_validate([compression, version, encoder]), RuntimeError),
    )
    check(
        "startup: several BodyEncoders all preceding Version is fine",
        _validate([compression, encoder, version]) is None,
    )

    # --- position is identity-based, not dataclass-equality-based ------------
    with patch.dict(DEFAULTS, {"APP_VERSION_MISMATCH": "prompt"}):
        twin = VersionMiddleware()
    check(
        "startup: an equal-but-distinct twin cannot mask our real position",
        isinstance(_validate([twin, version, compression]), RuntimeError),
    )

    # --- validate_stack is directly callable and side-effect free ------------
    ordered = [version, compression]
    check(
        "startup: validate_stack raises the same error called directly",
        isinstance(_direct_validate(version, ordered), RuntimeError),
    )
    check(
        "startup: validate_stack is repeatable (no state mutated)",
        isinstance(_direct_validate(version, ordered), RuntimeError)
        and _direct_validate(version, [compression, version]) is None,
    )
    check(
        "startup: a validator absent from the stack validates vacuously",
        _direct_validate(version, [compression]) is None,
    )

    # --- the action enum is a construction-time concern now ------------------
    err = _construct_version(APP_VERSION_MISMATCH="reboot")
    check(
        "construction: an invalid APP_VERSION_MISMATCH raises RuntimeError",
        isinstance(err, RuntimeError),
        f"raised {err!r}",
    )
    message = str(err) if err else ""
    check(
        "construction: the invalid-action error quotes the offending value",
        "'reboot'" in message,
        message,
    )
    check(
        "construction: the invalid-action error lists every allowed value",
        all(action in message for action in VERSION_ACTIONS),
        message,
    )
    for action in sorted(VERSION_ACTIONS):
        check(
            f"construction: APP_VERSION_MISMATCH={action!r} is accepted",
            _construct_version(APP_VERSION_MISMATCH=action) is None,
        )


def _abstract_rejects() -> bool:
    """True when StackValidator refuses to instantiate without the method."""

    class _Incomplete(StackValidator):
        pass

    try:
        _Incomplete()
    except TypeError:
        return True
    return False


def _direct_validate(
    validator: StackValidator, middlewares: list[object]
) -> Exception | None:
    """Call validate_stack directly; return the raised error."""
    try:
        validator.validate_stack(middlewares)
    except RuntimeError as exc:
        return exc
    return None


def _construct_version(**settings: object) -> Exception | None:
    """Construct a VersionMiddleware under settings; return the raised error."""
    with patch.dict(DEFAULTS, settings):
        try:
            VersionMiddleware()
        except RuntimeError as exc:
            return exc
    return None


# ---------------------------------------------------------------------------
# 6. VersionRouterMiddleware — hostile input paths
# ---------------------------------------------------------------------------


async def _router_checks() -> None:
    av = AppVersion()
    av.set_explicit("router-current")
    set_app_version(av)
    try:
        mw = VersionRouterMiddleware(version_map={"v1": "backend-v1"})

        hostile = "v9\r\nX-Injected: pwned"
        resp = await mw(make_request({"x-client-version": hostile}), json_handler)
        check("router: an unknown hostile version still 409s", resp.status == 409)
        body = resp.body.decode("utf-8")
        check(
            "router: no CRLF survives into the 409 payload",
            "\r" not in body and "\n" not in body,
            body,
        )
        check(
            "router: no injected header reached the response",
            "x-injected" not in {k.lower() for k in resp.headers},
        )

        long_req = "z" * 500
        resp = await mw(make_request({"x-client-version": long_req}), json_handler)
        check(
            "router: an over-long version is capped, then 409s",
            resp.status == 409 and ("z" * 128) in resp.body.decode("utf-8"),
        )
        check(
            "router: the capped version is not echoed at full length",
            ("z" * 129) not in resp.body.decode("utf-8"),
        )

        resp = await mw(make_request(cookie="hyper_client_version=v%31"), json_handler)
        check(
            "router: a percent-encoded cookie decodes to a known map key",
            resp.headers.get("x-backend-target") == "backend-v1",
            str(dict(resp.headers)),
        )

        crlf_map = VersionRouterMiddleware(
            version_map={"v1": "backend-v1\r\nX-Injected: pwned"}
        )
        resp = await crlf_map(make_request({"x-client-version": "v1"}), json_handler)
        target = resp.headers.get("x-backend-target", "")
        check(
            "router: version_map values are CRLF-sanitized into the routing header",
            "\r" not in target
            and "\n" not in target
            and target.startswith("backend-v1"),
            repr(target),
        )

        resp = await mw(make_request({"x-client-version": "v1"}), html_handler)
        check(
            "router: routing headers ride on HTML responses too",
            resp.headers.get("x-backend-target") == "backend-v1"
            and resp.headers.get("x-app-served-version") == "v1",
        )

        try:
            VersionRouterMiddleware(
                version_map={"v1": "backend-v1"}, default_version="v9"
            )
        except ValueError as exc:
            check(
                "router: a default_version outside version_map is a boot "
                "error naming the bad value and the available versions",
                "v9" in str(exc) and "v1" in str(exc),
                str(exc),
            )
        else:
            check(
                "router: a default_version outside version_map is a boot error",
                False,
                "construction succeeded",
            )
    finally:
        set_app_version(None)


# ---------------------------------------------------------------------------
# 7. Script builder: the hyper:version-mismatch event contract
# ---------------------------------------------------------------------------


def check_script_event_contract() -> None:
    for action in ("prompt", "reload", "warn"):
        script = get_client_script(action, True)
        check(
            f"script[{action}]: dispatches the hyper:version-mismatch event",
            "hyper:version-mismatch" in script.body,
        )
        check(
            f"script[{action}]: the event is cancelable (apps can own the UX)",
            "cancelable:true" in script.body,
        )
        check(
            f"script[{action}]: the event detail carries current/server/action",
            "current:v" in script.body
            and "server:sv" in script.body
            and "action:a" in script.body,
        )
        check(
            f"script[{action}]: bakes {action!r} only as the fallback action",
            f'HYPER_ACTION="{action}"' in script.body,
        )

    ignore = get_client_script("ignore", True)
    check(
        "script[ignore]: emits no mismatch event at all",
        "hyper:version-mismatch" not in ignore.body,
    )

    for action in ("reload", "warn"):
        check(
            f"script[{action}]: configRequest broadcast present when enabled",
            "htmx:configRequest" in get_client_script(action, True).body,
        )
        check(
            f"script[{action}]: configRequest absent when broadcast is off",
            "htmx:configRequest" not in get_client_script(action, False).body,
        )
        check(
            f"script[{action}]: mismatch machinery survives broadcast being off",
            "hyper:version-mismatch" in get_client_script(action, False).body,
        )

    prompt = get_client_script("prompt", True)
    check(
        "script[prompt]: the banner is guarded once-per-version in sessionStorage",
        "hyper_prompted_for" in prompt.body,
    )
    check(
        "script[prompt]: the banner is announced to assistive tech",
        "'role','status'" in prompt.body,
    )


# ---------------------------------------------------------------------------


def main() -> bool:
    check_client_version_parsing()
    check_middleware_end_to_end()
    check_skew_metric()
    asyncio.run(_encoded_response_checks())
    check_startup_validation()
    asyncio.run(_router_checks())
    check_script_event_contract()
    return finish()


if __name__ == "__main__":
    run_main(main)
