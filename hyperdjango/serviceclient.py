"""
Outbound service client + change-feed watcher.

Two reusable building blocks for programs that talk to an internal
JSON-over-HTTP service and follow its change feed:

``ServiceClient``
    A synchronous, stdlib-only JSON transport with a bounded retry policy,
    a parameterizable auth header, and optional mTLS client identity. Only
    idempotent requests are retried; HTTP status responses are definitive
    and never retried; transport failures retry up to the policy then raise
    a typed error.

``ChangeFeedWatcher``
    A self-healing consumer of a change feed in one of three delivery models,
    selected by what the hub advertises in its hello frame. In the durable
    ``ledger`` model the replay endpoint is the single ordered source of truth
    and live WebSocket traffic is only a wake-up hint — the cursor advances
    only through contiguous replay pages, so ordering and at-least-once
    delivery hold by construction. In the lighter ``ephemeral`` and ``catchup``
    models the hub delivers each event in the frame itself: ephemeral resyncs
    on every (re)connect and keeps no per-client state, while catchup retains
    a per-client cursor so a brief disconnect replays only the missed events.

This module never reads process environment inside a class; env-driven
construction is offered by the ``service_client_from_env`` helper, which
client programs may call. The module has no dependency on any application.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import http.client
import json
import os
import random
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "AuthError",
    "ChangeFeedWatcher",
    "PeerSilence",
    "RequestError",
    "ResponseError",
    "RetryPolicy",
    "ServerError",
    "ServiceClient",
    "ServiceError",
    "ServiceUnavailable",
    "build_ssl_context",
    "classify_status",
    "service_client_env_kwargs",
    "service_client_from_env",
]

_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Handshake ingress cap: the upgrade response header block must fit in this
# many bytes before the CRLFCRLF terminator, else the peer is misbehaving.
_WS_HANDSHAKE_MAX_BYTES = 64 * 1024
# Consecutive idle ping intervals with zero inbound bytes tolerated before the
# wake channel is declared dead (a black-holed peer never answers a ping).
_WS_MAX_MISSED_PINGS = 3
# Extra idle intervals of grace granted for OBSERVED LOCAL STALL — wall-clock
# time this thread provably was not scheduled to watch the socket (a recv that
# overran the timeout the kernel was given, or a gap between two watch windows).
# One interval of grace per interval of blindness, because a frozen client
# cannot send its keepalive either: the peer was never asked in that time, so
# it must not be charged for the silence. The credit is capped so a permanently
# starved host still eventually reconnects rather than staying blind forever —
# the worst-case deadline is (_WS_MAX_MISSED_PINGS + this) ping intervals.
_WS_MAX_STALL_CREDIT_INTERVALS = 3 * _WS_MAX_MISSED_PINGS
# Max data/continuation fragments reassembled into one wake message. Bounds a
# hub that streams endless zero-length continuation frames (which never grow
# the byte total, so the size cap alone cannot stop the loop).
_WS_MAX_WAKE_FRAGMENTS = 64
# Bounded short-backoff window for re-draining toward a lagging wake target.
_REDRAIN_MIN_BACKOFF = 0.05
_REDRAIN_MAX_BACKOFF = 3.0
# Local-resource-transient connect errnos: the local ephemeral-port range (or
# the OS address table) is momentarily exhausted, typically from TIME_WAIT churn
# under a burst of short-lived connections. The connect never reached the
# server, so the request was NOT sent — it is safe to wait these out regardless
# of idempotency, and they self-heal within seconds as ports leave TIME_WAIT.
# They are retried against a wall-clock DEADLINE (not the request retry policy)
# so a chatty client behind a busy NAT — or the full parallel test suite — does
# not fail an otherwise-fine request on a purely local, transient condition.
_LOCAL_RESOURCE_ERRNOS = frozenset({errno.EADDRNOTAVAIL, errno.EADDRINUSE})
_LOCAL_RESOURCE_RETRY_DEADLINE = 30.0
_LOCAL_RESOURCE_BACKOFF = 0.2


def _is_local_resource_transient(exc: BaseException) -> bool:
    """True if ``exc`` is a connect-time local-resource exhaustion (EADDRNOTAVAIL
    / EADDRINUSE), unwrapping a ``URLError`` around the underlying ``OSError``."""
    candidate: BaseException = exc
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, OSError):
        candidate = exc.reason
    return isinstance(candidate, OSError) and candidate.errno in _LOCAL_RESOURCE_ERRNOS


# Sentinel returned by the wake-loop frame reader for a complete frame that
# carried no usable cursor hint: a wake with no target update (never a close).
_WAKE = object()


# ── Errors ────────────────────────────────────────────────────────────────


class ServiceError(Exception):
    """Base error for the service client. Apps alias or subclass this.

    ``status`` is the HTTP status for status-derived errors (None for
    transport exhaustion); ``detail`` is the server-supplied message when the
    error body carried a JSON ``detail`` field.
    """

    def __init__(
        self, message: str = "", *, status: int | None = None, detail: str = ""
    ):
        super().__init__(message)
        self.status = status
        self.detail = detail


class AuthError(ServiceError):
    """401 (bad/revoked credential) or 403 (missing grant/scope)."""


class RequestError(ServiceError):
    """A definitive 4xx other than 401/403 — the request itself is wrong."""


class ServerError(ServiceError):
    """A 5xx status. Definitive for this request: the server saw it and
    answered with an error, so retrying the same call is not the client's
    call to make (the caller decides)."""


class ResponseError(ServiceError):
    """A well-formed HTTP response whose body could not be used as JSON.

    Raised when a 2xx answer carries a body that is not decodable JSON (a
    captive portal or proxy error page served with a 200), or when a body
    exceeds the configured size cap. Distinct so a caller can tell "the
    server answered but not with the payload contract" from a status error;
    a subtype of ``ServiceError`` so feed consumers that guard on the base
    type stay resilient."""


class ServiceUnavailable(ServiceError):
    """The server was unreachable after the retry policy was exhausted —
    a transport failure (connection refused, timeout, reset), not an HTTP
    status. Distinct so a caller can tell "the service is down" from "the
    request was rejected"."""


# ── Transport ──────────────────────────────────────────────────────────────


def build_ssl_context(
    ca_file: str = "",
    client_cert_file: str = "",
    client_key_file: str = "",
) -> ssl.SSLContext | None:
    """Build an mTLS client context, or None when neither is configured.

    A pinned CA makes the context verify the server against it; the default
    trust store is used when no CA is given. A client certificate is always
    loaded when supplied — independent of the CA — so presenting a client
    identity never requires also pinning a CA (otherwise the certificate is
    silently dropped and the client authenticates as nobody).
    """
    if not ca_file and not client_cert_file:
        return None
    ctx = ssl.create_default_context(cafile=ca_file or None)
    if client_cert_file:
        ctx.load_cert_chain(client_cert_file, client_key_file or None)
    return ctx


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential-backoff-with-jitter policy for idempotent requests.

    ``max_attempts`` counts the first try plus retries (so 3 = one try + two
    retries). Backoff for retry ``n`` (0-based) is
    ``min(base_backoff * 2**n, max_backoff)`` plus uniform jitter in
    ``[0, base_backoff)``. The jitter breaks up synchronized retry storms.
    ``base_backoff``/``max_backoff`` must be non-negative; ``0`` is allowed and
    means retry immediately (no wait), a negative value is rejected because it
    would make ``time.sleep`` raise mid-retry.
    """

    max_attempts: int = 3
    base_backoff: float = 0.1
    max_backoff: float = 10.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_backoff < 0:
            raise ValueError("base_backoff must be >= 0")
        if self.max_backoff < 0:
            raise ValueError("max_backoff must be >= 0")

    def backoff(self, attempt: int) -> float:
        """Delay before the retry that follows a failed 0-based ``attempt``."""
        capped = min(self.base_backoff * (2**attempt), self.max_backoff)
        return capped + random.uniform(0, self.base_backoff)


class ServiceClient:
    """Synchronous JSON-over-HTTP client with bounded, idempotent-only retry.

    Args:
        base_url: e.g. ``http://127.0.0.1:8960`` or ``https://svc.internal``.
        token: bearer/API-key credential, or ``""`` for none (e.g. when a
            client certificate authenticates instead).
        token_header: header carrying the credential (default
            ``Authorization``); set e.g. ``X-API-Key`` for API-key services.
        token_scheme: scheme prefixing the token in the header value
            (default ``Bearer``); ``""`` sends the raw token (API-key style).
        timeout: per-request seconds.
        retry: a ``RetryPolicy`` (or None to build the default).
        ca_file / client_cert_file / client_key_file: optional mTLS identity;
            an ``https`` base_url with a CA to pin and, optionally, a client
            certificate to present.
        max_response_bytes: hard cap on a response body read into memory; a
            larger body raises ``ResponseError`` instead of exhausting memory.
        ws_max_frame_bytes: hard cap on an announced WebSocket frame length;
            a wake channel opened by this client rejects a larger frame.
        ws_ping_interval: seconds of read idleness on a wake WebSocket after
            which the client sends a keepalive ping (the hub answers), so an
            idle connection stays up instead of tripping a read timeout.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        token_header: str = "Authorization",
        token_scheme: str = "Bearer",
        timeout: float = 5.0,
        retry: RetryPolicy | None = None,
        ca_file: str = "",
        client_cert_file: str = "",
        client_key_file: str = "",
        max_response_bytes: int = 32 * 1024 * 1024,
        ws_max_frame_bytes: int = 8 * 1024 * 1024,
        ws_ping_interval: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._token_header = token_header
        self._token_scheme = token_scheme
        self._timeout = timeout
        self._retry = retry or RetryPolicy()
        self._ssl_context = build_ssl_context(
            ca_file, client_cert_file, client_key_file
        )
        self._max_response_bytes = max_response_bytes
        self._ws_max_frame_bytes = ws_max_frame_bytes
        self._ws_ping_interval = ws_ping_interval
        # A JSON API has no valid 3xx answer, and following a redirect would
        # re-send the credential to the redirect target (possibly a different
        # host). Refuse to follow: the 3xx surfaces as a definitive error.
        self._opener = urllib.request.build_opener(
            _NoRedirectHandler,
            urllib.request.HTTPSHandler(context=self._ssl_context),
        )

    def _auth_headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        value = (
            f"{self._token_scheme} {self._token}" if self._token_scheme else self._token
        )
        return {self._token_header: value}

    def _read_body(self, resp) -> bytes:
        """Read a response body, capped at ``max_response_bytes``.

        Reads one byte past the cap so an over-limit body is detected rather
        than silently truncated, then raises ``ResponseError``.
        """
        raw = resp.read(self._max_response_bytes + 1)
        if len(raw) > self._max_response_bytes:
            raise ResponseError(
                f"response body exceeds {self._max_response_bytes} bytes"
            )
        return raw

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        idempotent: bool | None = None,
    ) -> dict | None:
        """Perform a request and return the parsed JSON body (None if empty).

        Retries apply only to idempotent requests. ``idempotent`` defaults to
        True for GET/HEAD/OPTIONS and False otherwise; pass ``idempotent=True``
        explicitly for a safe-to-repeat POST (for example one carrying a
        dedupe key). HTTP status errors are definitive and never retried;
        transport errors retry up to the policy, then raise
        ``ServiceUnavailable``. A 2xx body that is not decodable JSON raises
        ``ResponseError``.
        """
        _status, _headers, body = self._execute(
            method,
            path,
            json_body=json_body,
            params=params,
            idempotent=idempotent,
            raise_for_status=True,
        )
        return body

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        idempotent: bool | None = None,
    ) -> tuple[int, dict[str, str], dict | None]:
        """Like ``request`` but return ``(status, headers, body)`` without
        raising on a non-2xx status.

        The same retry, backoff, TLS, and no-redirect machinery applies: a
        transport failure still retries (per idempotency) and raises
        ``ServiceUnavailable`` when exhausted, and a body over the size cap
        still raises ``ResponseError``. But a definitive HTTP status — 3xx,
        4xx, or 5xx — is returned as the ``status`` rather than mapped to an
        exception, so a caller can treat 304/404/409 as first-class results.
        ``body`` is the parsed JSON when the body decodes, else ``None``.
        """
        return self._execute(
            method,
            path,
            json_body=json_body,
            params=params,
            idempotent=idempotent,
            raise_for_status=False,
        )

    def _execute(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None,
        params: dict | None,
        idempotent: bool | None,
        raise_for_status: bool,
    ) -> tuple[int, dict[str, str], dict | None]:
        method = method.upper()
        if idempotent is None:
            idempotent = method in _IDEMPOTENT_METHODS
        attempts = self._retry.max_attempts if idempotent else 1

        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(json_body).encode() if json_body is not None else None

        last_exc: Exception | None = None
        attempt = 0
        local_deadline = time.monotonic() + _LOCAL_RESOURCE_RETRY_DEADLINE
        while attempt < attempts:
            headers = self._auth_headers()
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(req, timeout=self._timeout) as resp:
                    raw = self._read_body(resp)
                    return (
                        resp.status,
                        dict(resp.headers),
                        _decode_json_body(raw, status=resp.status, strict=True),
                    )
            except urllib.error.HTTPError as exc:
                # A definitive HTTP status (incl. a refused 3xx redirect). The
                # error body flows through the same size cap as the 2xx path.
                if raise_for_status:
                    # The status is the answer; a body we cannot read (oversized
                    # past the cap, or a mid-body transport failure) degrades to
                    # an empty detail rather than escaping untyped or ballooning
                    # memory. The caller always gets a typed ServiceError.
                    try:
                        raw = self._read_body(exc)
                    except ServiceError, OSError, http.client.HTTPException:
                        raw = b""
                    raise _map_http_error(exc, raw) from None
                # request_raw: an oversized body still raises ResponseError (a
                # typed ServiceError), but a mid-body transport failure must not
                # escape as a bare OSError/IncompleteRead — surface it typed.
                try:
                    raw = self._read_body(exc)
                except (OSError, http.client.HTTPException) as read_exc:
                    raise ServiceUnavailable(
                        f"{url} error body unreadable: {read_exc}"
                    ) from read_exc
                return (
                    exc.code,
                    dict(exc.headers),
                    _decode_json_body(raw, status=exc.code, strict=False),
                )
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as exc:
                # Transport failure — retry if the policy and idempotency allow.
                # http.client.HTTPException (IncompleteRead from a mid-flight
                # reset on the 2xx success-path body read, BadStatusLine from a
                # torn connection at open) sits on Exception, not OSError, so
                # without it a truncated success body would escape raw and
                # unretried; here it retries then surfaces as ServiceUnavailable.
                last_exc = exc
                # A connect-time local-resource exhaustion (EADDRNOTAVAIL /
                # EADDRINUSE) did not send the request, so wait it out against a
                # wall-clock deadline WITHOUT consuming a policy attempt — it is
                # safe regardless of idempotency and self-heals in seconds.
                if (
                    _is_local_resource_transient(exc)
                    and time.monotonic() < local_deadline
                ):
                    time.sleep(
                        _LOCAL_RESOURCE_BACKOFF
                        + random.uniform(0, _LOCAL_RESOURCE_BACKOFF)
                    )
                    continue
                attempt += 1
                if attempt < attempts:
                    time.sleep(self._retry.backoff(attempt - 1))
        raise ServiceUnavailable(
            f"{self.base_url} unreachable after {attempt} attempt(s): {last_exc}"
        )

    def open_websocket(
        self,
        path: str,
        *,
        extra_headers: dict[str, str] | None = None,
        on_socket: Callable[[socket.socket], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> _WebSocketConnection:
        """Open a WebSocket to ``path`` using this client's identity.

        Reuses the auth header and mTLS context, so the feed connection
        authenticates the same way the HTTP calls do.

        ``on_socket`` receives the socket as soon as it exists — before the TLS
        wrap and the upgrade handshake, each of which can block for the full
        connect timeout. A long-lived owner passes it so a shutdown can reach
        the connection during that window, when there is not yet an object to
        return. ``cancelled`` is polled during the local-resource connect wait
        (the one blocking stretch that holds no socket) so a shutting-down owner
        can abandon it instead of waiting the retry deadline out.
        """
        headers = self._auth_headers()
        if extra_headers:
            headers.update(extra_headers)
        return _WebSocketConnection(
            self.base_url,
            path,
            headers,
            ssl_context=self._ssl_context,
            timeout=self._timeout,
            max_frame_bytes=self._ws_max_frame_bytes,
            ping_interval=self._ws_ping_interval,
            on_socket=on_socket,
            cancelled=cancelled,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to auto-follow 3xx: returning None leaves the redirect
    unhandled, so ``urlopen`` raises the 3xx as an ``HTTPError`` instead of
    re-issuing the request (and re-sending the credential) to the target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_control_chars(label: str, value: str) -> None:
    """Reject CR, LF, and other control characters in a header/handshake field.

    A raw value carrying ``\\r\\n`` would inject additional header lines (or a
    forged request line) into the WebSocket upgrade written to the socket.

    A header value may be a credential (a bearer token, an API key), so it is
    never echoed into the error — only the offending byte and its position are
    reported. Non-secret fields (path, host, header name) are shown verbatim.
    """
    for i, ch in enumerate(value):
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            if label == "header value":
                raise ValueError(
                    f"illegal control character in {label} "
                    f"(0x{ord(ch):02x} at index {i}; value redacted)"
                )
            raise ValueError(f"illegal control character in {label}: {value!r}")


def _decode_json_body(raw: bytes, *, status: int, strict: bool) -> dict | None:
    """Decode a response body as JSON.

    Empty bodies are ``None``. A non-JSON body raises ``ResponseError`` in
    strict mode (the ``request`` contract: a 2xx must carry JSON) and returns
    ``None`` in lenient mode (``request_raw``, where the status is the answer).
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        if strict:
            raise ResponseError(
                f"{status}: response body is not valid JSON", status=status
            ) from exc
        return None


def classify_status(status: int, detail: str = "") -> ServiceError:
    """Map an HTTP status (and an optional server-supplied detail) to a typed
    ``ServiceError`` — the client's status taxonomy as a reusable function.

    ``401``/``403`` → ``AuthError``; a ``3xx`` (a JSON API has no valid redirect
    answer) or any other ``4xx`` → ``RequestError``; a ``5xx`` → ``ServerError``.
    An SDK that layers its own status meanings (a ``404`` that means "not found",
    a ``409`` that means "conflict") special-cases those and delegates every
    other status here, instead of re-deriving the base taxonomy.
    """
    message = f"{status}: {detail}" if detail else str(status)
    if status in (401, 403):
        return AuthError(message, status=status, detail=detail)
    if 300 <= status < 400:
        # A JSON API has no valid redirect answer; the opener refused to follow.
        return RequestError(
            message,
            status=status,
            detail=detail or "unexpected redirect (not followed)",
        )
    if 400 <= status < 500:
        return RequestError(message, status=status, detail=detail)
    return ServerError(message, status=status, detail=detail)


def _map_http_error(exc: urllib.error.HTTPError, body: bytes) -> ServiceError:
    detail = ""
    if body:
        with contextlib.suppress(ValueError, TypeError):
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = str(parsed.get("detail", ""))
    return classify_status(exc.code, detail)


def service_client_env_kwargs(prefix: str) -> dict:
    """Return the ``ServiceClient`` constructor kwargs read from ``{PREFIX}_*``
    environment variables — the base-URL, credential, and mTLS-identity shape
    every env-driven client shares.

    Reads ``{PREFIX}_URL`` (as ``base_url``), ``{PREFIX}_TOKEN`` (credential),
    and ``{PREFIX}_CA_FILE`` / ``{PREFIX}_CLIENT_CERT`` / ``{PREFIX}_CLIENT_KEY``
    (mTLS identity). An SDK subclass can splat the result straight into its own
    constructor (``cls(**service_client_env_kwargs(prefix), **app_specific)``)
    instead of re-parsing the same variables; ``service_client_from_env`` builds
    on it, so the variable shape lives in exactly one place. Reading the
    environment here (not inside ``ServiceClient``) keeps the class free of
    ambient configuration.
    """
    # Sanctioned env boundary: these {PREFIX}_* variables describe an EXTERNAL
    # service (its URL/token/mTLS identity), not framework settings, so they are
    # read straight from the environment rather than through get_setting.
    env = os.environ
    return {
        "base_url": env.get(f"{prefix}_URL", ""),
        "token": env.get(f"{prefix}_TOKEN", ""),
        "ca_file": env.get(f"{prefix}_CA_FILE", ""),
        "client_cert_file": env.get(f"{prefix}_CLIENT_CERT", ""),
        "client_key_file": env.get(f"{prefix}_CLIENT_KEY", ""),
    }


def service_client_from_env(
    prefix: str,
    *,
    token_header: str = "Authorization",
    token_scheme: str = "Bearer",
    **overrides,
) -> ServiceClient:
    """Build a ``ServiceClient`` from ``{PREFIX}_*`` environment variables.

    Reads the variables described by ``service_client_env_kwargs`` (all optional
    except the URL being useful). Any keyword in ``overrides`` wins over the
    environment, including ``base_url``.
    """
    kwargs = service_client_env_kwargs(prefix)
    kwargs["token_header"] = token_header
    kwargs["token_scheme"] = token_scheme
    kwargs.update(overrides)
    base_url = kwargs.pop("base_url", "")
    return ServiceClient(base_url, **kwargs)


# ── Minimal WebSocket client (RFC 6455) ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PeerSilence:
    """The evidence behind declaring a wake peer unresponsive.

    A liveness verdict is only as good as what it can show for itself, so the
    verdict carries its own accounting: how much silence was actually WATCHED
    (``observed_seconds`` — idle windows this thread was awake for, which is
    what the peer is charged with), how much grace this host's own blindness
    bought the peer (``stall_seconds`` — one ping interval for each interval
    this thread provably was not scheduled to look, capped), and the
    ``deadline_seconds`` the observed silence had to beat, which is exactly the
    documented deadline plus that grace. An operator reading a feed drop can
    tell "the hub went quiet" from "this host froze" without a packet capture.

    Sub-interval blindness that did not add up to a whole interval of grace is
    not counted here — it is not part of the verdict — but it is still
    observation, and it lands in the watcher's ``stall_seconds`` total.
    """

    observed_seconds: float
    stall_seconds: float
    deadline_seconds: float


class _WebSocketConnection:
    """Minimal RFC 6455 text-frame client: masking, ping/pong, close.

    Only what a change-feed wake channel needs: connect, receive JSON text
    messages, optionally send JSON, and close cleanly. Server frames are
    expected unmasked per the protocol.
    """

    def __init__(
        self,
        base_url: str,
        path: str,
        headers: dict[str, str],
        *,
        ssl_context: ssl.SSLContext | None,
        timeout: float,
        max_frame_bytes: int = 8 * 1024 * 1024,
        ping_interval: float = 20.0,
        on_socket: Callable[[socket.socket], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        parsed = urllib.parse.urlparse(base_url)
        # TLS and the default port both derive from the scheme, so a ws/wss (or
        # any other) base_url would silently connect plaintext on port 80 or
        # skip TLS. Fail loudly rather than downgrade: https carries wss.
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"base_url scheme must be http or https, not {parsed.scheme!r} "
                "(TLS and default port derive from it; use https for wss)"
            )
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._max_frame_bytes = max_frame_bytes
        self._ping_interval = ping_interval

        # Reject CRLF/control chars before writing the upgrade, so no field can
        # inject an extra header line or forge the request line.
        _reject_control_chars("path", path)
        _reject_control_chars("host", host)
        for k, v in headers.items():
            _reject_control_chars("header name", k)
            _reject_control_chars("header value", v)

        # Same local-resource policy as _request's HTTP path: EADDRNOTAVAIL /
        # EADDRINUSE means the connect never left this host (ephemeral-port
        # range exhausted by TIME_WAIT churn under bursts of short-lived
        # connections) — wait it out against the shared wall-clock deadline
        # instead of failing an otherwise-fine handshake on a self-healing
        # local condition.
        local_deadline = time.monotonic() + _LOCAL_RESOURCE_RETRY_DEADLINE
        while True:
            try:
                raw = socket.create_connection((host, port), timeout=timeout)
                break
            except OSError as exc:
                if (
                    _is_local_resource_transient(exc)
                    and time.monotonic() < local_deadline
                    # The wait is up to _LOCAL_RESOURCE_RETRY_DEADLINE long and
                    # holds no socket an owner could shut down, so it is the one
                    # place a shutting-down watcher cannot be reached through the
                    # transport. Give it an explicit exit instead: a cancelled
                    # connect fails now rather than outliving the stop() that
                    # asked for it.
                    and not (cancelled is not None and cancelled())
                ):
                    time.sleep(
                        _LOCAL_RESOURCE_BACKOFF
                        + random.uniform(0, _LOCAL_RESOURCE_BACKOFF)
                    )
                    continue
                # A transport failure reaching the wake hub (refused, timeout,
                # no local address) is typed like every other transport failure
                # in this module, so a direct caller and the watcher alike see
                # a ServiceError rather than a bare OSError.
                raise ServiceUnavailable(
                    f"WebSocket connect to {host}:{port} failed: {exc}"
                ) from exc
        # Every step past the raw socket can raise (TLS wrap, upgrade refusal,
        # accept-key mismatch, oversized handshake, hangup mid-handshake). None
        # of those paths reach _ws_loop's finally-close, because open_websocket
        # returns the object only after __init__ completes — so a construction
        # failure must close the socket here or leak one fd per reconnect
        # against a hub that accepts TCP then refuses the upgrade. Closing the
        # TLS-wrapped socket closes its underlying plain socket; if the wrap
        # itself fails, raw is still the plain socket, so raw covers both.
        try:
            # Publish the socket to its owner BEFORE anything that can block on
            # it. TLS wrap, the upgrade write and the handshake read all park on
            # this fd for up to `timeout`, and until now none of them was
            # reachable from a stop path — an owner could only see a connection
            # that had finished connecting, so a stop landing inside the
            # handshake had nothing to interrupt and waited the socket out.
            # Published here, the whole lifetime is interruptible.
            if on_socket is not None:
                on_socket(raw)
            if parsed.scheme == "https":
                if ssl_context is None:
                    ssl_context = ssl.create_default_context()
                # wrap_socket detaches `raw` (its fd moves to the SSLSocket), so
                # the previously published object no longer names the fd —
                # republish the wrapper that now owns it.
                raw = ssl_context.wrap_socket(raw, server_hostname=host)
                if on_socket is not None:
                    on_socket(raw)
            self._sock = raw
            self._buf = b""
            # Liveness accounting for this session, readable by the owner while
            # it runs (see ChangeFeedWatcher.keepalives / stall_seconds) and
            # after it dies (peer_silence names the cause). Written only by the
            # single reader thread; ints/floats are read atomically elsewhere.
            self.pings_sent = 0
            self.stall_seconds = 0.0
            self.peer_silence: PeerSilence | None = None

            key = base64.b64encode(secrets.token_bytes(16)).decode()
            lines = [
                f"GET {path} HTTP/1.1",
                f"Host: {host}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            lines += [f"{k}: {v}" for k, v in headers.items()]
            self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

            response = self._read_until(b"\r\n\r\n")
            status_line = response.split(b"\r\n", 1)[0]
            if b" 101 " not in status_line:
                raise ServiceUnavailable(
                    f"WebSocket upgrade refused: {status_line.decode(errors='replace')}"
                )
            expected = base64.b64encode(
                hashlib.sha1((key + _WS_ACCEPT_GUID).encode()).digest()
            ).decode()
            if expected.encode() not in response:
                raise ServiceUnavailable("WebSocket accept key mismatch")

            # The handshake is done: switch from the connect deadline to an idle
            # ping interval. A quiet hub does not trip a read timeout — the client
            # sends a keepalive ping and keeps the connection up.
            if self._ping_interval and self._ping_interval > 0:
                self._sock.settimeout(self._ping_interval)
            else:
                self._sock.settimeout(None)
        except BaseException:
            with contextlib.suppress(OSError):
                raw.close()
            raise

    def _read_until(self, marker: bytes) -> bytes:
        # Handshake-only: the connect deadline is still in force here, and no
        # upgrade has happened yet, so no keepalive ping is sent. The header
        # block is capped so a peer that streams bytes without ever sending the
        # CRLFCRLF terminator cannot grow the buffer without bound.
        while marker not in self._buf:
            if len(self._buf) > _WS_HANDSHAKE_MAX_BYTES:
                raise ServiceUnavailable(
                    f"WebSocket handshake response exceeds "
                    f"{_WS_HANDSHAKE_MAX_BYTES} bytes"
                )
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ServiceUnavailable("Connection closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head + marker

    def _send_ping(self) -> None:
        mask = secrets.token_bytes(4)
        self._sock.sendall(struct.pack("!BB", 0x89, 0x80) + mask)
        self.pings_sent += 1

    def _read_exact(self, n: int) -> bytes:
        """Read exactly ``n`` bytes, holding an idle session up with keepalive
        pings and judging the peer only on silence this thread actually watched.

        A healthy peer answers our keepalive ping (or sends anything), which
        clears the silence; a black-holed peer (NAT drop, power-off, no RST)
        never does, so after a few missed intervals we give up rather than
        pinging into the void for hours while wake latency silently degrades to
        the poll interval.

        The subtlety is what "a missed interval" may be allowed to mean. A bare
        count of expired socket timeouts conflates two different facts: "the
        peer sent nothing" and "I was not scheduled to look". The socket timeout
        is wall-clock, so on a loaded host — a starved core, a stop-the-world
        pause, a swap storm — a window the kernel closed at ``ping_interval``
        can be observed seconds later, and the same freeze that blinded us also
        delayed the keepalive we owed the peer and the peer's own reply. Charged
        naively, that is a healthy idle connection torn down precisely when the
        host is least able to absorb the reconnect — and on a shared hub every
        client does it at once, so the load spike lands on the one component
        already struggling.

        So the two facts are measured apart, against ``time.monotonic()``:

        * ``missed`` counts only windows whose silence we actually watched. A
          window contributes at most the timeout the kernel was given.
        * everything past that — a recv that overran its own timeout, plus the
          gap between one window closing and the next opening — is local
          BLINDNESS. It is never charged to the peer, and every whole interval
          of it buys the peer one extra interval of grace (capped, so a
          permanently frozen host still reconnects rather than staying blind).

        On an unloaded host stall is ~0 and this is exactly the old rule: a
        black-holed peer is still dropped after ``_WS_MAX_MISSED_PINGS``
        intervals. Under load the deadline stretches by as much as this thread
        was provably absent, and no more.
        """
        missed = 0  # idle windows whose silence this thread watched
        credit = 0  # extra windows granted for observed local stall
        carry = 0.0  # stall seconds not yet worth a whole interval of grace
        watched_since: float | None = None  # when the last window closed
        interval = self._ping_interval
        while len(self._buf) < n:
            entered = time.monotonic()
            if watched_since is not None:
                # Between two watch windows nobody was looking at this socket
                # and no keepalive was in flight — blindness, not silence.
                carry += entered - watched_since
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError:
                closed = time.monotonic()
                watched_since = closed
                # The kernel closed this window at `interval`; observing it any
                # later means this thread was descheduled inside the wait.
                carry += max(0.0, (closed - entered) - interval)
                missed += 1
                while carry >= interval and credit < _WS_MAX_STALL_CREDIT_INTERVALS:
                    carry -= interval
                    credit += 1
                if missed >= _WS_MAX_MISSED_PINGS + credit:
                    self.stall_seconds += credit * interval + carry
                    self.peer_silence = PeerSilence(
                        observed_seconds=missed * interval,
                        # The grace actually granted — sub-interval blindness
                        # bought nothing, so it is not part of the verdict.
                        stall_seconds=credit * interval,
                        deadline_seconds=(_WS_MAX_MISSED_PINGS + credit) * interval,
                    )
                    raise ServiceUnavailable(
                        "WebSocket peer unresponsive: no inbound data in "
                        f"{missed * interval:.3f}s of watched silence "
                        f"(deadline {(_WS_MAX_MISSED_PINGS + credit) * interval:.3f}s, "
                        f"widened by {credit * interval:.3f}s of local stall)"
                    ) from None
                # A closed socket surfaces as OSError (not a timeout) and
                # propagates so the caller can reconnect.
                self._send_ping()
                continue
            if not chunk:
                raise ServiceUnavailable("Connection closed")
            # Inbound bytes end the silence AND the blindness run: what came
            # before is spent, and the next silence is judged on its own.
            self.stall_seconds += credit * interval + carry
            missed = 0
            credit = 0
            carry = 0.0
            watched_since = None
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_json(self, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            head = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 65536:
            head = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            head = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(head + mask + masked)

    def recv_json(self) -> dict | None:
        """Next JSON text/binary message, or None when the server closes."""
        while True:
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            # Reject an oversized announced length before reading it into memory.
            # Control frames (opcode >= 0x8) are bounded to 125 bytes by RFC 6455.
            if opcode >= 0x8 and length > 125:
                raise ServiceUnavailable(
                    f"WebSocket control frame too large: {length} bytes"
                )
            if length > self._max_frame_bytes:
                raise ServiceUnavailable(
                    f"WebSocket frame exceeds {self._max_frame_bytes} bytes: {length}"
                )
            payload = self._read_exact(length)  # server frames are unmasked
            if opcode == 0x8:  # close
                return None
            if opcode == 0x9:  # ping → pong
                mask = secrets.token_bytes(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                self._sock.sendall(
                    struct.pack("!BB", 0x8A, 0x80 | len(payload)) + mask + masked
                )
                continue
            if opcode in (0x1, 0x2):
                return json.loads(payload)

    def recv_wake(self) -> object:
        """Consume the next complete message as a change-feed wake hint.

        Any complete data message is a wake; only a close (opcode 0x8) or a
        server hangup ends the channel (returns None). Unlike ``recv_json`` this
        never raises on a non-JSON or fragmented payload — the wake channel
        needs a frame's *arrival*, not its content, and reads the cursor only as
        an optional hint — so a malformed or continuation-split frame is a valid
        wake rather than a reconnect trigger. Fragments are reassembled so the
        read stays frame-aligned; ping is answered and pong is skipped. Returns
        the decoded dict when the payload is a JSON object, else the ``_WAKE``
        sentinel (a wake with no cursor hint).
        """
        payload = b""
        data_frames = 0
        while True:
            b1, b2 = self._read_exact(2)
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if opcode >= 0x8 and length > 125:
                raise ServiceUnavailable(
                    f"WebSocket control frame too large: {length} bytes"
                )
            if length > self._max_frame_bytes:
                raise ServiceUnavailable(
                    f"WebSocket frame exceeds {self._max_frame_bytes} bytes: {length}"
                )
            chunk = self._read_exact(length)  # server frames are unmasked
            if opcode == 0x8:  # close
                return None
            if opcode == 0x9:  # ping → pong
                mask = secrets.token_bytes(4)
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(chunk))
                self._sock.sendall(
                    struct.pack("!BB", 0x8A, 0x80 | len(chunk)) + mask + masked
                )
                continue
            if opcode == 0xA:  # pong (keepalive answer) — not a wake on its own
                continue
            # A data (0x1/0x2) or continuation (0x0) frame. Accumulate until FIN
            # so the buffer ends on a frame boundary. Two independent caps bound
            # the reassembly: total bytes (a large fragment stream) AND fragment
            # count — a hub streaming endless ZERO-length continuation frames
            # grows neither the byte total nor blocks on a read, so only the
            # frame-count cap stops it from spinning forever without a wake.
            payload += chunk
            data_frames += 1
            if (
                len(payload) > self._max_frame_bytes
                or data_frames > _WS_MAX_WAKE_FRAGMENTS
            ):
                raise ServiceUnavailable(
                    f"WebSocket message exceeds reassembly caps "
                    f"({len(payload)} bytes, {data_frames} fragments)"
                )
            if fin:
                break
        with contextlib.suppress(ValueError):
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                return parsed
        return _WAKE

    def shutdown(self) -> None:
        """Unblock this connection's reader, from ANY thread. Never closes.

        The only teardown step that is safe to run while another thread is
        parked in ``recv``. ``shutdown(SHUT_RDWR)`` makes the socket readable at
        end-of-stream, so the reader returns b"" at once and unwinds through its
        own ``finally`` — which is where the fd is actually closed, by the
        thread that owns it.

        Closing the fd here instead would be a RACE, not merely impolite: the
        reader is parked in ``poll()`` on that fd, and once the fd is gone the
        wakeup has nowhere to land. Measured on macOS/arm64 under 3.14t,
        ``shutdown()`` immediately followed by ``close()`` lost the wakeup 103
        times in 200 attempts, leaving the reader parked until its socket
        timeout expired — which is exactly a ``stop()`` that returns with its
        thread still running. Shutdown alone: 0 losses in 200, every wakeup
        under a millisecond. Linux happens to win that race consistently, so
        this reads as a no-op difference there; do not take that as licence to
        merge the two, because the same kernels disagree in the OTHER direction
        for the mirror-image case (a close on a socket some thread is parked in
        ``recv`` on does not send a FIN on Linux at all). ``shutdown`` is the
        one step both agree on.

        So: cross-thread callers call this; the owning thread calls ``close``.
        """
        with contextlib.suppress(OSError):
            mask = secrets.token_bytes(4)
            self._sock.sendall(struct.pack("!BB", 0x88, 0x80) + mask)
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        """Full teardown: unblock, then release the fd.

        Call from the thread that reads this connection (or when no reader is
        running). A concurrent reader must be stopped with ``shutdown`` and
        joined first — see the race documented there.
        """
        self.shutdown()
        with contextlib.suppress(OSError):
            self._sock.close()


# ── Change-feed watcher ──────────────────────────────────────────────────────

# Type of a replay function: given a cursor, return the server's page dict.
ReplayFn = Callable[[int], dict]


class ChangeFeedWatcher:
    """Self-healing consumer of a change feed across three delivery models.

    The hub advertises a delivery ``mode`` in its hello frame and the watcher
    runs the matching state machine — one class, one internal mode switch:

    - **ledger** — the durable model. The replay endpoint is the single
      ordered source of truth and the cursor advances only through contiguous
      replay pages; a live WebSocket frame is purely a wake-up hint (its
      payload is never delivered and never advances the cursor), so
      out-of-order, duplicated, or dropped wakes cannot corrupt ordering or
      lose events, and a periodic poll self-heals even when every wake is
      lost. Selected when a durable ``replay_path`` (or ``replay`` callable) is
      configured; the default when a hub sends no hello at all.
    - **ephemeral** — the hub keeps no per-client state and delivers each event
      in the frame. ``on_reset`` fires on every (re)connect (invalidate and
      lazily re-fetch); each ``event`` frame is delivered via ``on_event``.
    - **catchup** — the hub retains a per-client buffer keyed by ``client_id``.
      The watcher persists ``client_id`` (stable for its lifetime), the last
      delivered ``seq``, and the hub-incarnation ``epoch`` it saw; on reconnect
      it sends all three, and the hub replays only the missed event frames
      (``seq`` > ``last_seq``) then streams live ones. If the hub evicted past
      ``last_seq`` (``resync`` in the hello) it resyncs via ``on_reset`` instead.
      A changed ``epoch`` (a hub restart) also forces a resync: the new
      incarnation's seq space is unrelated to the retained ``last_seq``, so the
      stale key is discarded even when it now sits inside the new seq range.

    Connection state is observable: ``connected`` answers "is my live feed up
    right now?", ``wait_connected`` / ``wait_disconnected`` block on the
    transition, and ``connects`` / ``disconnects`` count sessions (a climbing
    pair is a flapping hub). A consumer that serves cached state off this feed
    needs exactly this to report itself degraded — while the feed is down no
    invalidation arrives, so cached values can be stale up to their own TTL.

    So is the liveness machinery behind it: ``keepalives`` counts the pings
    holding an idle session up (the only thing that moves against a quiet hub),
    ``peer_timeouts`` counts sessions dropped for missing the keepalive
    deadline with ``last_peer_silence`` carrying that verdict's evidence, and
    ``stall_seconds`` reports how long this host was not scheduled to watch the
    feed at all. Together they answer the question a reconnect always raises —
    was it the hub, or was it us? — because a client that churns connections
    whenever its own host is loaded adds load exactly when the system can least
    absorb it, and across a shared hub's clients it does so all at once.

    Negotiation: the watcher's configured intent (a durable ``replay_path`` →
    ledger; otherwise a ws-only feed → in-frame delivery) proposes a model, and
    the hub's advertised ``mode`` decides. A mismatch always falls back to a
    frame-delivery resync, which is safe: a hub advertising ledger to a watcher
    with no replay endpoint is served as ephemeral, and a hub advertising an
    in-frame model to a ledger-configured watcher quiesces the replay drain and
    takes the hub's frames.

    Args:
        client: a ``ServiceClient`` used for replay and (if ``ws_path`` is
            set) the WebSocket. Optional if a ``replay`` callable is given and
            no WebSocket is used.
        replay_path: HTTP path of the durable replay endpoint (built into a
            replay call against ``client`` when ``replay`` is not supplied).
            Optional: omit it for a ws-only ephemeral/catchup feed.
        on_event: ``on_event(event: dict)`` for each delivered event, in
            ledger order. Exceptions are contained and counted, never fatal.
        cursor: starting cursor (deliver everything after it).
        replay: optional ``replay(after: int) -> dict`` override; when given,
            ``client``/``replay_path`` are not used for replay.
        limit: page size requested per replay call.
        after_param / limit_param: query-parameter names for the cursor and
            page size.
        extra_params: static query parameters added to every replay call
            (for example a subscription prefix).
        events_field / cursor_field / reset_field: response keys for the
            event list, the page's end cursor, and the reset flag.
        event_id_field: key on each event holding its monotonic ledger id.
        ws_path: WebSocket path for the feed, or None for poll-only ledger.
            The subscribe/hello handshake and, in ledger mode, wake hints flow
            over it.
        wake_cursor_field: key on a wake frame carrying the ledger cursor the
            server has reached. Used ONLY as a liveness target: when a drain
            ends below the highest hinted cursor (the server's replay ceiling
            lagged the wake), the watcher re-drains on a short bounded backoff
            instead of sleeping a full poll interval. The hint never delivers
            an event and never advances the cursor — that is replay's job.
        on_reset: ``on_reset(response: dict)`` invoked for a full resync — in
            ledger mode when the cursor fell below the retention floor, in
            ephemeral mode on every (re)connect, and in catchup mode when the
            hub evicted past the retained ``last_seq``. Exceptions are
            contained.
        client_id: stable identity for catchup reconnect (generated per-watcher
            when omitted). Pass one to keep a stable identity across process
            restarts; sent in the subscribe frame so the hub can locate this
            client's retained buffer.
        prefixes: subscription prefixes announced in the subscribe frame (the
            subjects this watcher wants). Empty means the hub's default scope.
        poll_interval: seconds between replay pulls when no wake arrives —
            the self-healing floor for lost wakes.
        stable_period: seconds a WebSocket must stay connected before its
            drop resets reconnect backoff; a flapping hub keeps backing off.
    """

    def __init__(
        self,
        client: ServiceClient | None = None,
        *,
        replay_path: str = "",
        on_event: Callable[[dict], object],
        cursor: int = 0,
        replay: ReplayFn | None = None,
        limit: int = 500,
        after_param: str = "after",
        limit_param: str = "limit",
        extra_params: dict[str, str] | None = None,
        events_field: str = "events",
        cursor_field: str = "cursor",
        reset_field: str = "reset",
        event_id_field: str = "id",
        ws_path: str | None = None,
        wake_cursor_field: str = "cursor",
        on_reset: Callable[[dict], object] | None = None,
        poll_interval: float = 30.0,
        stable_period: float = 30.0,
        base_backoff: float = 0.2,
        max_backoff: float = 10.0,
        client_id: str | None = None,
        prefixes: list[str] | None = None,
    ):
        has_replay = replay is not None or (client is not None and bool(replay_path))
        if not has_replay and ws_path is None:
            raise ValueError(
                "provide a replay source (replay=, or client= with replay_path=) "
                "or a ws_path= for in-frame delivery"
            )
        if ws_path is not None and client is None:
            # The wake WebSocket is opened against the client. Without one the
            # ws thread would hit AttributeError on the first iteration, die
            # (that error is not in its reconnect except-tuple), and silently
            # degrade the watcher to poll-only forever. Fail loudly instead.
            raise ValueError("ws_path requires a client= to open the wake WebSocket")
        self._client = client
        self._replay_path = replay_path
        self._replay_fn = replay
        self._on_event = on_event
        self._on_reset = on_reset
        self._limit = limit
        self._after_param = after_param
        self._limit_param = limit_param
        self._extra_params = dict(extra_params or {})
        self._events_field = events_field
        self._cursor_field = cursor_field
        self._reset_field = reset_field
        self._event_id_field = event_id_field
        self._ws_path = ws_path
        self._wake_cursor_field = wake_cursor_field
        self._poll_interval = poll_interval
        self._stable_period = stable_period
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        # Stable across the watcher's lifetime; a generated per-watcher uuid is
        # fine when the caller does not need identity to survive a restart.
        self.client_id = client_id or uuid.uuid4().hex
        self._prefixes = list(prefixes or [])

        self.cursor = cursor
        # Last event seq delivered over the WebSocket. The catchup resume key
        # (sent in the subscribe frame) and the observable high-water mark in
        # ephemeral mode. None until the first in-frame event is delivered.
        self.last_seq: int | None = None
        # The hub-incarnation token last advertised in a catchup/ephemeral hello,
        # echoed in the next subscribe frame. None until the first hello. A hello
        # epoch that differs from this one names a NEW hub incarnation (a restart),
        # whose seq space is unrelated to the last_seq we hold — so we must resync,
        # not resume, however the seq numbers happen to line up. Written and read
        # only by the ws thread.
        self._epoch: str | None = None
        # Negotiated delivery mode, set by the ws thread from the hub's hello
        # (or inferred). None until negotiated — a poll-only ledger watcher
        # stays None and the drain treats None as ledger. Written by the ws
        # thread, read by the drain thread (a plain reference read is atomic).
        self._mode: str | None = None
        # Cursor value at which on_reset last fired. A reset that cannot advance
        # the cursor (empty page, no/non-int cursor_field) would otherwise
        # re-fire every drain; this pins each stuck-reset position to one firing.
        self._last_reset_cursor: int | None = None
        # Highest wake-hint cursor seen. Written only by the ws thread, read by
        # the drain thread as a liveness target (never a delivery source).
        self._wake_target = 0
        # Observable counters (safe to read from another thread).
        self.reconnect_delay = base_backoff
        self.event_callback_errors = 0
        self.reset_callback_errors = 0
        self.drain_errors = 0
        self.resets = 0
        # Sessions dropped because the peer missed the keepalive deadline, and
        # the accounting behind the most recent one. A climbing count against a
        # hub that is up names a black-holed path (or, if last_peer_silence
        # shows stall, a host too starved to keep watching).
        self.peer_timeouts = 0
        self.last_peer_silence: PeerSilence | None = None
        # Keepalive/stall totals from sessions that have already ended; the live
        # session's own counters are added by the properties below.
        self._closed_keepalives = 0
        self._closed_stall = 0.0
        # Live-feed session state, observable by the owner: a consumer serving
        # cached state off this feed must be able to ask "is my feed connected
        # right now?" (health/readiness, degraded-mode decisions) and to BLOCK on
        # a transition rather than poll for it. Guarded by one Condition so the
        # flag and its two counters move together and a waiter is woken exactly
        # when they do. Written only by the ws thread; read from anywhere.
        self._state_cond = threading.Condition()
        self._connected = False
        self.connects = 0
        self.disconnects = 0

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._drain_lock = threading.Lock()
        # The live wake connection, exposed so stop() can unblock an idle recv.
        self._ws_conn_lock = threading.Lock()
        self._active_ws: _WebSocketConnection | None = None
        # The live wake SOCKET, published by open_websocket the instant it
        # exists — before the TLS wrap and the upgrade handshake, which is the
        # window _active_ws cannot cover (there is no connection object until
        # __init__ returns). Held under the same lock so stop() always has
        # something to shut down for as long as the ws thread can be blocked on
        # the network. Cleared by the ws thread's finally.
        self._active_sock: socket.socket | None = None
        # The drain thread pulls replay pages; it exists only when a durable
        # replay source is configured. A ws-only ephemeral/catchup watcher has
        # no replay to pull — delivery is entirely the ws thread's job.
        self._drain_thread: threading.Thread | None = None
        if self._has_replay():
            self._drain_thread = threading.Thread(
                target=self._drain_loop, name="changefeed-drain", daemon=True
            )
        self._ws_thread: threading.Thread | None = None
        if self._ws_path is not None:
            self._ws_thread = threading.Thread(
                target=self._ws_loop, name="changefeed-wake", daemon=True
            )

    def _has_replay(self) -> bool:
        """Whether a durable replay source is configured (ledger intent)."""
        return self._replay_fn is not None or bool(self._replay_path)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> ChangeFeedWatcher:
        if self._drain_thread is not None:
            self._drain_thread.start()
        if self._ws_thread is not None:
            self._ws_thread.start()
        return self

    # -- live-feed connection state (observable) -----------------------------

    @property
    def connected(self) -> bool:
        """Whether the live feed is established RIGHT NOW.

        True from the moment a session's delivery model is settled — the hub's
        hello is processed (and any resync it demanded has already been handed
        to ``on_reset``), or, for a ledger-intent watcher, the subscribe frame
        is away and ledger delivery is pre-entered — until that session drops.
        False while reconnecting, and always False for a poll-only watcher with
        no ``ws_path`` (it has no feed to connect).

        The invalidation ordering is the point: because a connect's resync is
        applied BEFORE the flag flips, an owner that sees ``connected`` knows
        the feed is live *and* that the connect's cache invalidation has already
        happened — there is no later surprise reset from this connect. A
        consumer serving cached state can surface this in a health probe ("live
        invalidation is down, values may be up to cache_ttl stale") and can wait
        on the transition instead of guessing at a timeout.
        """
        with self._state_cond:
            return self._connected

    @property
    def keepalives(self) -> int:
        """Keepalive pings sent on the live feed, over this watcher's lifetime.

        The proof that an idle feed is being HELD UP rather than merely not
        dropped yet: a quiet hub produces no frames at all, so this counter is
        the only thing that moves while the session sits idle. Rising while
        ``connected`` is a healthy idle feed; rising while ``peer_timeouts``
        also rises is a peer that takes pings and never answers.
        """
        with self._ws_conn_lock:
            live = self._active_ws.pings_sent if self._active_ws is not None else 0
            return self._closed_keepalives + live

    @property
    def stall_seconds(self) -> float:
        """Seconds this host was provably not scheduled to watch the feed.

        Local blindness measured by the reader itself (a socket wait that
        overran the timeout the kernel was given, plus the gaps between waits),
        never charged to the peer — it instead widens the peer's liveness
        deadline interval-for-interval. Read it as a health signal about THIS
        process: a feed that looks flaky while this climbs is a starved client,
        not a flapping hub, and reconnecting harder would only add load.
        """
        with self._ws_conn_lock:
            live = self._active_ws.stall_seconds if self._active_ws is not None else 0.0
            return self._closed_stall + live

    def wait_connected(self, timeout: float = 5.0) -> bool:
        """Block until the feed is established. Returns the state on return.

        A LEVEL wait: it answers "is it up (yet)?", so use it when the state is
        expected to persist. To detect a transition that may not persist — a hub
        that drops and is reconnected in milliseconds — watch the monotonic
        ``connects`` / ``disconnects`` counters instead; a level can flip back
        before an observer looks, an edge cannot be un-counted.
        """
        with self._state_cond:
            return self._state_cond.wait_for(lambda: self._connected, timeout)

    def wait_disconnected(self, timeout: float = 5.0) -> bool:
        """Block until the feed is NOT established (never connected counts).

        The mirror of ``wait_connected`` for the hub-loss direction: a consumer
        (or a test) that must act only once the feed is genuinely down waits on
        the observed transition instead of assuming the drop propagated. When it
        returns True the ws session is fully torn down, so every frame that
        session had already received has been dispatched to ``on_event`` /
        ``on_reset`` — no delivery can still land from it.
        """
        with self._state_cond:
            return self._state_cond.wait_for(lambda: not self._connected, timeout)

    def _set_connected(self, value: bool) -> None:
        """Record a live-feed transition and wake anyone waiting on it."""
        with self._state_cond:
            if self._connected == value:
                return
            self._connected = value
            if value:
                self.connects += 1
            else:
                self.disconnects += 1
            self._state_cond.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        """Shut the watcher down and return only once its threads have exited.

        Raises ``ServiceError`` if a thread is still running when ``timeout``
        expires. Returning quietly with a live thread would be the worst of both
        outcomes — the caller believes the watcher is gone while its callbacks
        can still fire — so an un-stoppable thread is reported, not hidden.
        """
        self._stop.set()
        self._wake.set()
        # A wake connection blocks in recv (idle) or in its handshake reads
        # (connecting); shut the socket down so the ws thread unwinds at once
        # instead of waiting out a socket timeout. SHUT_RDWR only — the fd is
        # closed by the thread that owns it, in _ws_loop's finally. See
        # _WebSocketConnection.shutdown for why closing it here would race the
        # very wakeup it is trying to deliver.
        with self._ws_conn_lock:
            if self._active_ws is not None:
                self._active_ws.shutdown()
            elif self._active_sock is not None:
                # Mid-connect: no connection object yet, but the socket is
                # already published and already blocking. Reach it directly.
                with contextlib.suppress(OSError):
                    self._active_sock.shutdown(socket.SHUT_RDWR)
        deadline = time.monotonic() + timeout
        stuck: list[str] = []
        for thread in (self._ws_thread, self._drain_thread):
            if thread is None:
                continue
            # One shared deadline, not one per thread: stop(timeout=5) means the
            # call returns in ~5s, not in 5s per thread it happens to own.
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                stuck.append(thread.name)
        if stuck:
            raise ServiceError(
                f"ChangeFeedWatcher.stop({timeout}s) left {', '.join(stuck)} "
                "running: the thread is blocked somewhere the stop signal "
                "cannot reach. Its callbacks may still fire."
            )

    # -- replay drain (the ordered source of truth) --------------------------

    def _replay(self, after: int) -> dict:
        if self._replay_fn is not None:
            return self._replay_fn(after)
        params = {
            self._after_param: str(after),
            self._limit_param: str(self._limit),
            **self._extra_params,
        }
        resp = self._client.request("GET", self._replay_path, params=params)
        return resp or {}

    def _drain(self) -> None:
        """Pull contiguous replay pages until the ledger tail is reached.

        Serialized by ``_drain_lock`` so wake and poll ticks can never run two
        drains concurrently against one cursor.
        """
        with self._drain_lock:
            while not self._stop.is_set():
                start_cursor = self.cursor
                resp = self._replay(start_cursor)
                if not isinstance(resp, dict):
                    # A top-level JSON non-object (e.g. a list) is not a valid
                    # page: resp.get would raise. Count it as a bad page and
                    # stop this drain — the cursor is untouched, the next tick
                    # retries. No crash, no data delivered from a bad shape.
                    self.drain_errors += 1
                    break
                events = resp.get(self._events_field) or []
                page_cursor = resp.get(self._cursor_field)

                if (
                    resp.get(self._reset_field)
                    and start_cursor != self._last_reset_cursor
                ):
                    # The cursor fell below the retention floor: the gap of
                    # trimmed events is unrecoverable here. Signal a full
                    # resync; the page below (and page_cursor) carry the cursor
                    # forward past the floor. Fire only the first reset seen at
                    # this cursor value — a degenerate reset that never advances
                    # the cursor (empty page, no/non-int cursor_field) must not
                    # loop-fire on_reset (and climb `resets`) every poll.
                    self.resets += 1
                    self._safe_reset(resp)
                    self._last_reset_cursor = start_cursor

                for event in events:
                    self._safe_event(event)
                    event_id = event.get(self._event_id_field)
                    if event_id is not None:
                        self.cursor = event_id

                # Adopt the server's end-of-page cursor when it is ahead of the
                # last delivered id (the server may have scanned past events
                # filtered out for this subscriber, or floored a reset). Never
                # move the cursor backward.
                if isinstance(page_cursor, int) and page_cursor > self.cursor:
                    self.cursor = page_cursor

                # A short page means the tail is reached. A full page may have
                # more behind it, so keep draining.
                if len(events) < self._limit:
                    break

                # A full page that left the cursor exactly where it started
                # (mismatched id/cursor fields, or a stale/non-int echoed
                # cursor) would loop at full speed forever: infinite duplicate
                # delivery and a request per iteration. Count it and break so
                # the misconfiguration surfaces via drain_errors instead of
                # hammering the server.
                if self.cursor == start_cursor:
                    self.drain_errors += 1
                    break

    def _drain_loop(self) -> None:
        # Deliver on start, on every wake hint, and on every poll tick.
        redrain = _REDRAIN_MIN_BACKOFF
        # The _wake_target value the current re-drain sequence is chasing. The
        # backoff floor is restored only when a genuinely NEW hint raises the
        # target above this — never on mere cursor progress toward an unchanged
        # one — so a hint that can never be reached decays to the poll cadence.
        chasing_target = self._wake_target
        while not self._stop.is_set():
            # Clear the wake before draining so a wake raised while this drain
            # is in flight is preserved: it re-arms the event and the wait below
            # returns at once for a follow-up drain, rather than being wiped.
            self._wake.clear()
            # Replay pulls happen only while the feed is in ledger delivery.
            # mode is None for a poll-only ledger watcher (no hello) and until
            # a ws hello arrives; a hello advertising ephemeral/catchup moves
            # delivery to the ws thread's in-frame events and quiesces here.
            if self._mode in (None, "ledger"):
                try:
                    self._drain()
                except ServiceError:
                    # Replay unreachable, a non-JSON page, or an oversized body
                    # is fine right now: the next tick retries and the cursor is
                    # untouched, so nothing is skipped or misordered.
                    pass
                # blind-except: no unexpected error may kill the long-lived feed
                # thread; count it so a storm of failures stays observable.
                except Exception:
                    self.drain_errors += 1
            # Snapshot the target once so both the reset test and the lag test
            # below see one consistent value even as the ws thread advances it.
            target = self._wake_target
            if target > chasing_target:
                # A fresh wake frame raised the target: restart the re-drain at
                # the floor so a newly announced ceiling is chased snappily.
                chasing_target = target
                redrain = _REDRAIN_MIN_BACKOFF
            # A wake hinted a cursor the drain did not reach — the server's
            # replay ceiling lagged the wake (an unrelated long transaction
            # held the visibility horizon back). Re-drain on a short bounded
            # backoff to catch the event the instant it is revealed, rather than
            # sleeping a full poll_interval. Delivery still comes only from the
            # replay pages above; the hint is purely a liveness target — the hub
            # is not trusted to keep it sane, so a target that never rises again
            # keeps doubling the backoff up to poll_interval, so a bogus or
            # mis-mapped hint (or one chased while unrelated events trickle in)
            # degrades to the normal polling cadence instead of pinning the
            # watcher in amplified re-drains at the floor.
            if not self._stop.is_set() and self.cursor < target:
                self._wake.wait(redrain)
                redrain = min(
                    redrain * 2, max(_REDRAIN_MAX_BACKOFF, self._poll_interval)
                )
                continue
            redrain = _REDRAIN_MIN_BACKOFF
            self._wake.wait(self._poll_interval)

    def _safe_event(self, event: dict) -> None:
        try:
            self._on_event(event)
        # blind-except: a subscriber's on_event error must not kill the feed
        # thread; count it so a storm of failures is observable.
        except Exception:
            self.event_callback_errors += 1

    def _safe_reset(self, resp: dict) -> None:
        if self._on_reset is None:
            return
        try:
            self._on_reset(resp)
        # blind-except: a subscriber's on_reset error must not kill the feed
        # thread; count it so a storm of failures is observable.
        except Exception:
            self.reset_callback_errors += 1

    # -- feed WebSocket (mode-driven delivery) -------------------------------

    def _publish_ws_socket(self, sock: socket.socket) -> None:
        """Make a connecting wake socket reachable by ``stop`` immediately.

        Called by ``open_websocket`` as soon as the socket exists — before the
        TLS wrap and before the upgrade handshake, both of which park on it for
        up to the connect timeout. Without this the socket is invisible until
        the handshake finishes, so a ``stop`` arriving mid-handshake has nothing
        to interrupt and the thread outlives the call that asked it to end.

        A ``stop`` that already ran will never look again, so a socket published
        after it is shut down here instead — the handshake in flight fails at
        once rather than running to completion for a watcher that is gone.
        """
        with self._ws_conn_lock:
            if self._stop.is_set():
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                return
            self._active_sock = sock

    def _ws_loop(self) -> None:
        backoff = self._base_backoff
        while not self._stop.is_set():
            conn: _WebSocketConnection | None = None
            connected_at: float | None = None
            healthy = False
            try:
                conn = self._client.open_websocket(
                    self._ws_path,
                    on_socket=self._publish_ws_socket,
                    cancelled=self._stop.is_set,
                )
                connected_at = time.monotonic()
                with self._ws_conn_lock:
                    self._active_ws = conn
                # Publish-then-check, and stop() sets the flag before it takes
                # the lock: whichever order the two threads interleave in, the
                # session is ended by exactly one of them. If this read is
                # False, stop() had not yet set the flag, so its later
                # lock-and-shutdown is guaranteed to find `conn` published.
                if self._stop.is_set():
                    break
                # Announce the subscription so the hub can pick a delivery mode.
                self._send_subscribe(conn)
                if self._has_replay():
                    # Ledger intent: default to ledger delivery on connect so a
                    # hub that only sends wake hints (no hello) drains at once
                    # and catches up on anything missed while disconnected. A
                    # hello may downgrade this to in-frame delivery.
                    self._mode = "ledger"
                    # Delivery is settled for this session (ledger, pulled by the
                    # drain thread) even if the hub never sends a hello, so the
                    # feed is observably connected from here.
                    self._set_connected(True)
                    self._wake.set()
                while not self._stop.is_set():
                    msg = conn.recv_wake()
                    if msg is None:
                        break
                    # Any complete frame proves the session live; a non-JSON or
                    # fragmented frame is still a frame, not a reconnect —
                    # recv_wake tolerates it.
                    healthy = True
                    if isinstance(msg, dict) and msg.get("type") == "hello":
                        self._on_hello(msg)
                        continue
                    mode = self._mode
                    if mode is None:
                        # No hello and no replay source: infer a safe in-frame
                        # mode and resync before delivering this frame.
                        mode = self._infer_mode()
                    self._deliver_frame(mode, msg)
            except ServiceError, OSError, ValueError:
                pass
            finally:
                with self._ws_conn_lock:
                    if conn is not None:
                        # Fold this session's liveness accounting into the
                        # watcher's totals in the same critical section that
                        # retires the connection, so the properties above can
                        # never count a session twice or miss it entirely.
                        self._closed_keepalives += conn.pings_sent
                        self._closed_stall += conn.stall_seconds
                        if conn.peer_silence is not None:
                            self.last_peer_silence = conn.peer_silence
                            self.peer_timeouts += 1
                    self._active_ws = None
                    # Retired together: a stale socket here would have stop()
                    # shutting down an fd this thread is about to close (or, once
                    # the number is recycled, somebody else's).
                    self._active_sock = None
                if conn is not None:
                    # The owning thread, and the only place this fd is closed —
                    # a cross-thread close would race a parked reader's wakeup.
                    conn.close()
                # The session is over and every frame it delivered has already
                # been dispatched above, so the observable state can flip only
                # here — a waiter released by this transition can rely on no
                # further delivery arriving from the dead session.
                self._set_connected(False)
            if self._stop.is_set():
                break
            # Reset backoff after a healthy session — one that delivered a wake
            # or stayed connected past the stable period. A hub that accepts
            # then immediately drops (no wake, sub-stable) keeps backing off
            # instead of being hammered.
            stable = (
                connected_at is not None
                and time.monotonic() - connected_at >= self._stable_period
            )
            if healthy or stable:
                backoff = self._base_backoff
            delay = min(backoff, self._max_backoff) + random.uniform(0, backoff)
            self.reconnect_delay = delay
            self._stop.wait(delay)
            backoff = min(backoff * 2, self._max_backoff)

    def _send_subscribe(self, conn: _WebSocketConnection) -> None:
        """Announce the subscription so the hub can pick a delivery mode.

        Carries this watcher's state: the stable ``client_id``, the last
        delivered ``seq`` (the hub's in-memory catch-up key) and the ``epoch`` it
        was delivered under (so a hub restart is detected), and — when a durable
        replay endpoint is configured — the ledger ``cursor``. The hub replies
        with a hello declaring which model it will serve.
        """
        conn.send_json(
            {
                "type": "subscribe",
                "prefixes": self._prefixes,
                "client_id": self.client_id,
                "last_seq": self.last_seq,
                "epoch": self._epoch,
                "cursor": self.cursor if self._has_replay() else None,
            }
        )

    def _default_mode(self) -> str:
        """Delivery mode when the hub advertises nothing usable. Catchup is
        never inferred — only the hub knows whether it retained this client's
        buffer — so the non-ledger fallback always resyncs (ephemeral)."""
        return "ledger" if self._has_replay() else "ephemeral"

    def _on_hello(self, hello: dict) -> None:
        """Enter the delivery mode the hub advertised in its hello frame.

        The hub's ``mode`` wins, reconciled with what this watcher can do: a
        hub asking for ledger replay when no replay endpoint is configured
        falls back to in-frame delivery (a full resync is always safe), and an
        unknown mode falls back to the configured default.
        """
        mode = hello.get("mode")
        if mode == "ledger" and not self._has_replay():
            mode = "ephemeral"
        elif mode not in ("ledger", "ephemeral", "catchup"):
            mode = self._default_mode()
        self._mode = mode
        hub_epoch = hello.get("epoch")
        # A hello whose epoch differs from the one we were resuming under names a
        # NEW hub incarnation (a restart). Its seq space is unrelated to our
        # last_seq, so even if the hub did not itself flag resync we must not
        # resume — force a full resync and discard the stale resume key. None is a
        # first connect (no stored epoch), already handled by the resync flag.
        epoch_changed = hub_epoch is not None and hub_epoch != self._epoch
        if mode == "ledger":
            # Delivery is the drain thread's job; nudge it to pull to the head
            # the hub just advertised.
            self._wake.set()
        elif mode == "ephemeral":
            # The hub kept no per-client state: invalidate and lazily re-fetch
            # on every (re)connect.
            self._safe_reset(hello)
        elif mode == "catchup" and (hello.get("resync") or epoch_changed):
            # The hub evicted past our last_seq, never knew this client, or is a
            # new incarnation: the missed window is unrecoverable, so resync, then
            # take live frames. Discard the stale last_seq (it belongs to the old
            # incarnation's seq space) and adopt the advertised head so a later
            # reconnect resumes from this incarnation, not the dead one.
            self._safe_reset(hello)
            boundary = hello.get("seq")
            self.last_seq = boundary if isinstance(boundary, int) else None
        # catchup without resync (matching epoch): the hub replays the missed
        # event frames next.
        if hub_epoch is not None:
            self._epoch = hub_epoch
        # Delivery is negotiated and this connect's resync (if any) has already
        # been applied — only now is the feed observably connected. Flipping the
        # flag last is what lets an owner treat `connected` as "no pending
        # invalidation from this connect".
        self._set_connected(True)

    def _infer_mode(self) -> str:
        """Pick a mode when the hub sends no hello (a hub predating the hello
        handshake). Only reached by a ws-only watcher — a replay-configured one
        pre-enters ledger on connect — so this always resyncs before delivery.
        """
        mode = self._default_mode()
        self._mode = mode
        if mode != "ledger":
            self._safe_reset({"resync": True})
        # Same contract as the hello path: settled mode + resync applied, so the
        # feed is observably connected from here (a hello-less hub never reaches
        # the hello path, and would otherwise never report connected at all).
        self._set_connected(True)
        return mode

    def _deliver_frame(self, mode: str, msg: object) -> None:
        """Handle one post-hello frame under the negotiated ``mode``."""
        if mode == "ledger":
            # The in-frame payload is never delivered in ledger mode: the frame
            # is a liveness hint only. Record the highest hinted cursor as a
            # re-drain target and nudge the drain; replay delivers and advances
            # the cursor.
            if isinstance(msg, dict):
                hint = msg.get(self._wake_cursor_field)
                if isinstance(hint, int) and hint > self._wake_target:
                    self._wake_target = hint
            self._wake.set()
            return
        # ephemeral / catchup: the event is delivered in the frame itself.
        if isinstance(msg, dict) and msg.get("type") == "event":
            self._safe_event(msg)
            seq = msg.get("seq")
            if isinstance(seq, int):
                # Retained so a catchup reconnect resumes just past it; also the
                # observable high-water mark in ephemeral mode. Advance
                # MONOTONICALLY: concurrent publishers assign seq in order under
                # the hub lock but fan out over independent channel sends, so a
                # frame can arrive out of order (seq 2 before seq 1). Every frame
                # is still delivered to on_event (dedupe/refetch is the consumer's
                # job); only the RESUME cursor must never regress, or a reconnect
                # would re-replay an already-seen event.
                self.last_seq = max(self.last_seq or 0, seq)
            return
        # A ws-only watcher (no durable replay source) that fell back from a
        # ledger hub to in-frame delivery still receives the ledger's wake hints,
        # which are not event frames and carry no payload it can deliver. Without
        # a replay endpoint it cannot service the wake's cursor, so the only way
        # not to go stale is to treat every wake as a resync trigger: re-fetch
        # everything on each change. Degraded (a full resync per change instead of
        # a targeted replay) but never stale. A watcher WITH a replay source runs
        # in ledger mode and handles wakes via replay above.
        if (
            isinstance(msg, dict)
            and msg.get("type") == "wake"
            and not self._has_replay()
        ):
            self._safe_reset(msg)
