"""
Mutual-TLS client authentication.

Three pieces, usable together (self-contained) or with an external proxy:

1. **Certificate authority helpers** — ``create_ca()`` / ``issue_cert()``
   mint a private CA and per-service client certificates (CN = the service's
   identity name). Requires the ``cryptography`` package (``crypto`` extra).

2. **MTLSTerminator** — a TLS-terminating certificate-authentication gateway
   that runs inside the application process on its own thread + event loop. It
   terminates TLS, verifies the client certificate against the CA during the
   handshake, derives the identity (common name + SHA-256 fingerprint) with
   ``cryptography`` and enforces the certificate validity window and a
   clientAuth Extended Key Usage, then forwards the connection over loopback to
   the (plaintext) native server with verified-identity headers injected. It
   makes no HTTP framing decisions: it strips inbound spoofs of the identity
   headers and the client's inbound ``Connection`` header, appends the attested
   set plus exactly one ``Connection`` header (``Upgrade`` when the head carries
   an ``Upgrade`` line, otherwise ``close``), and splices the rest of the
   connection opaquely in both directions. The injected set carries
   a per-process random attestation secret, so nothing outside the process can
   forge a verified identity. The native server is the single HTTP framing
   authority; live feeds (WebSocket) work unchanged because everything after the
   request head is opaque bytes.

3. **resolve_client_cert()** — the single authority apps call to read the
   verified identity off a request. It honors the headers only when the
   attestation matches (constant-time), checking two sources in precedence
   order: the process-level registry of running in-process terminators (see
   below), then the ``MTLS_PROXY_SECRET`` setting shared with an external
   TLS-terminating proxy. Unset/absent attestation → ``None`` (fail closed).

   Every ``MTLSTerminator.start()`` registers its own attestation secret in the
   process-level registry and ``stop()`` deregisters it, so an in-process
   terminator+app deployment needs nothing hand-carried — the registry resolves
   it automatically; the ``MTLS_PROXY_SECRET`` setting covers the proxy case.

Injected headers (always stripped from inbound traffic first):

    X-Hyper-MTLS-Attest:      attestation secret (terminator or proxy)
    X-Hyper-MTLS-CN:          certificate subject common name
    X-Hyper-MTLS-Fingerprint: sha256 hex of the client certificate (DER)
    X-Real-IP:                original peer address (terminator topology)

External-proxy topology: configure the proxy to verify client certs and
send the same headers with ``MTLS_PROXY_SECRET`` as the attestation value.
See ``docs/mtls.md`` for a complete nginx configuration example.
"""

import asyncio
import contextlib
import datetime
import hmac
import secrets as _secrets
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path

from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.logging import logger
from hyperdjango.telemetry.metrics import Counter, Gauge

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

MTLS_ACTIVE = Gauge(
    "hyperdjango_mtls_active_connections",
    "Client connections currently open on the mTLS terminator.",
)
MTLS_HANDSHAKE_FAILURES = Counter(
    "hyperdjango_mtls_handshake_failures_total",
    "Completed TLS handshakes that carried no usable client-certificate "
    "identity (a certificate with a missing or malformed common name). "
    "Certificates rejected during the handshake itself — untrusted, expired, "
    "or absent — are refused by the TLS layer before reaching the terminator "
    "and are not counted here.",
)
MTLS_CONNECTIONS_SHED = Counter(
    "hyperdjango_mtls_connections_shed_total",
    "Connections rejected because the terminator was at its connection cap.",
)
MTLS_UPSTREAM_FAILURES = Counter(
    "hyperdjango_mtls_upstream_failures_total",
    "Failures connecting from the terminator to the plaintext upstream.",
)
MTLS_IDENTITY_POLICY_REJECTED = Counter(
    "hyperdjango_mtls_identity_policy_rejected_total",
    "Handshake-verified client certificates refused by the terminator's "
    "post-handshake identity policy: outside the certificate validity window "
    "(expired or not yet valid), or an Extended Key Usage extension that does "
    "not include clientAuth. Chain trust is enforced by the TLS handshake; "
    "these are the additional policy checks stdlib ssl does not express cleanly.",
)

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:  # pragma: no cover - exercised only without the extra
    x509 = None

ATTEST_HEADER = "x-hyper-mtls-attest"
CN_HEADER = "x-hyper-mtls-cn"
FINGERPRINT_HEADER = "x-hyper-mtls-fingerprint"

_STRIP_PREFIX = b"x-hyper-mtls-"
_MAX_HEAD_BYTES = 64 * 1024
# The resolver and the terminator must agree on the CN length ceiling: a CN
# longer than this is refused at BOTH ends (the terminator will not inject it,
# the resolver will not honor it) so a long-CN cert can never silently resolve
# to no identity on only one side.
_MAX_CN_LEN = 256
# Floor for the external-proxy shared secret (MTLS_PROXY_SECRET), matching the
# repo's signing-key minimum. A shorter value is a broken boundary and is
# refused (fail closed) rather than accepted as a trivially-guessable secret.
_MIN_PROXY_SECRET_LEN = 32


class MTLSError(Exception):
    """Configuration or certificate-issuance failure."""


def _require_cryptography() -> None:
    if x509 is None:
        raise MTLSError(
            "mTLS certificate issuance requires the 'cryptography' package "
            "(install the 'crypto' extra)"
        )


# ---------------------------------------------------------------------------
# Certificate authority
# ---------------------------------------------------------------------------


def create_ca(common_name: str, *, days: int = 3650) -> tuple[bytes, bytes]:
    """Create a private CA. Returns ``(key_pem, cert_pem)``."""
    _require_cryptography()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


def issue_cert(
    ca_key_pem: bytes,
    ca_cert_pem: bytes,
    common_name: str,
    *,
    days: int = 365,
    server: bool = False,
    san_dns: list[str] | None = None,
) -> tuple[bytes, bytes]:
    """Issue a certificate signed by the CA. Returns ``(key_pem, cert_pem)``.

    Client certificates carry the service identity name as CN; server
    certificates additionally carry DNS SANs for hostname verification.
    """
    _require_cryptography()
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    eku = (
        x509.ExtendedKeyUsageOID.SERVER_AUTH
        if server
        else x509.ExtendedKeyUsageOID.CLIENT_AUTH
    )
    try:
        ca_ski = ca_cert.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value
        aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski)
    except x509.ExtensionNotFound:
        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name)])
        )
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(aki, critical=False)
    )
    if server:
        import ipaddress

        sans: list = []
        for entry in san_dns or ["localhost"]:
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(entry)))
            except ValueError:
                sans.append(x509.DNSName(entry))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(sans), critical=False
        )
    cert = builder.sign(ca_key, hashes.SHA256())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


def write_pem(path: str, data: bytes, *, private: bool = False) -> None:
    """Write PEM material; private keys are created 0600."""
    p = Path(path)
    if private:
        import os

        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    else:
        p.write_bytes(data)


# ---------------------------------------------------------------------------
# Identity authority
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ClientCertIdentity:
    """A network-verified client-certificate identity."""

    common_name: str
    fingerprint_sha256: str


def normalize_fingerprint(value: str) -> str:
    """Canonicalize a SHA-256 certificate fingerprint for comparison.

    The terminator emits lowercase hex with no separators, but an operator's
    allow-list is often pasted in the OpenSSL ``AB:CD:...`` uppercase/colon form
    (and may carry stray whitespace). Removing all whitespace and colons and
    lowercasing makes those forms compare equal, so a pin does not fail closed
    silently on a cosmetic format difference. A non-string is coerced to ``""``.
    """
    if not isinstance(value, str):
        return ""
    return "".join(value.split()).replace(":", "").lower()


_RESERVED_HEADERS = frozenset({b"x-real-ip", b"x-forwarded-for"})
_CONNECTION_HEADER = b"connection"
_UPGRADE_HEADER = b"upgrade"


def rewrite_request_head(head: bytes, injected: bytes) -> tuple[bytes, bool] | None:
    """Rewrite one HTTP/1.1 request head (bytes before the ``CRLFCRLF``).

    This is a lexical splice, not an HTTP parse. It never reads the request
    line, Content-Length, or Transfer-Encoding — the native server is the sole
    framing authority. It only:

    - keeps the request line verbatim as the first line;
    - drops, by case-insensitive header name, any inbound reserved identity
      header (``x-hyper-mtls-*``, ``x-real-ip``, ``x-forwarded-for``) so a
      client cannot smuggle an attested identity past the terminator, plus the
      client's inbound ``Connection`` header;
    - appends ``injected`` (the raw attested ``name: value\\r\\n`` identity
      lines) followed by exactly one ``Connection`` header.

    The single ``Connection`` header is chosen by a lexical header-name check,
    not by interpreting HTTP framing: if the surviving head carries an
    ``Upgrade`` header line (a WebSocket upgrade) the injected value is
    ``Connection: Upgrade`` so the native server performs the upgrade;
    otherwise it is ``Connection: close``, giving one request per upstream
    connection. Emitting exactly one ``Connection`` header keeps correctness
    from depending on how the native server resolves multiple conflicting
    ``Connection`` headers.

    A header line using obs-fold (beginning with SP or HTAB) is rejected rather
    than interpreted: returns ``None`` and the caller closes the connection.

    Returns ``(rewritten_head, has_upgrade)`` — the rewritten head (including
    the terminating ``CRLFCRLF``) and whether a surviving ``Upgrade`` line was
    seen. The caller needs ``has_upgrade`` to decide the splice policy: a
    non-upgrade connection is reaped on ``idle_timeout`` idleness, while an
    upgrade is reaped on the larger ``upgrade_idle_timeout`` — so a genuine
    WebSocket survives long idle gaps between events while a client that sends an
    ``Upgrade`` head and then goes silent is still reaped.
    """
    lines = head.split(b"\r\n")
    request_line, header_lines = lines[0], lines[1:]
    kept: list[bytes] = []
    has_upgrade = False
    for line in header_lines:
        if not line:
            continue
        # obs-fold (a continuation line) is ambiguous to a byte-lexer and a
        # classic desync vector; refuse it rather than guess its owner header.
        if line[:1] in (b" ", b"\t"):
            return None
        name = line.split(b":", 1)[0].strip().lower()
        # Lexical presence check only — a surviving Upgrade line means the
        # native server should upgrade; we do not parse its value.
        if name == _UPGRADE_HEADER:
            has_upgrade = True
        if (
            name.startswith(_STRIP_PREFIX)
            or name in _RESERVED_HEADERS
            or name == _CONNECTION_HEADER
        ):
            continue
        kept.append(line)
    connection = b"connection: upgrade" if has_upgrade else b"connection: close"
    rewritten = (
        b"\r\n".join([request_line, *kept])
        + b"\r\n"
        + injected
        + connection
        + b"\r\n\r\n"
    )
    return rewritten, has_upgrade


# Process-level registry of attestation secrets for the in-process terminators
# currently running in THIS process. `MTLSTerminator.start()` adds its secret and
# `stop()` removes it, so `resolve_client_cert` can recognize a terminator's
# injected identity without the app hand-threading the secret. Guarded by a lock
# for free-threading; reads snapshot under the lock, then compare outside it.
_ATTEST_REGISTRY: set[str] = set()
_ATTEST_REGISTRY_LOCK = threading.Lock()


def _register_attestation(secret: str) -> None:
    with _ATTEST_REGISTRY_LOCK:
        _ATTEST_REGISTRY.add(secret)


def _deregister_attestation(secret: str) -> None:
    with _ATTEST_REGISTRY_LOCK:
        _ATTEST_REGISTRY.discard(secret)


def _attestation_registered(attest_bytes: bytes) -> bool:
    """Constant-time match of an inbound attestation against every registered
    terminator secret. The registry is tiny (one entry per in-process
    terminator), and each comparison is constant-time so a partial match leaks
    no timing signal."""
    with _ATTEST_REGISTRY_LOCK:
        snapshot = tuple(_ATTEST_REGISTRY)
    matched = False
    for secret in snapshot:
        # Keep comparing after a match so total work is independent of WHICH
        # entry matched (and never short-circuits on the first mismatch either).
        if hmac.compare_digest(attest_bytes, secret.encode("utf-8")):
            matched = True
    return matched


_short_proxy_secret_logged = False


def _configured_proxy_secret() -> str:
    """The external-proxy attestation secret from ``MTLS_PROXY_SECRET``, or ``""``.

    A configured secret shorter than :data:`_MIN_PROXY_SECRET_LEN` (the repo's
    signing-key floor) is refused: a trivially short shared secret is a broken
    security boundary, so it never attests an identity. Fail closed — the
    too-short value is treated as unset and the misconfiguration is logged once
    with a clear, actionable message rather than silently honored.
    """
    secret = get_setting("MTLS_PROXY_SECRET", "")
    if secret and len(secret) < _MIN_PROXY_SECRET_LEN:
        global _short_proxy_secret_logged
        if not _short_proxy_secret_logged:
            _short_proxy_secret_logged = True
            logger.error(
                "MTLS_PROXY_SECRET is {n} characters; the minimum is {m}. "
                "Ignoring it (fail closed) — configure a random secret of at "
                "least {m} characters.",
                n=len(secret),
                m=_MIN_PROXY_SECRET_LEN,
            )
        return ""
    return secret


def resolve_client_cert(request) -> ClientCertIdentity | None:
    """Return the verified certificate identity for a request, or ``None``.

    Honors the mTLS headers only when the attestation value matches
    (constant-time) one of two sources, in precedence order: the process-level
    registry of running in-process terminators (each ``MTLSTerminator.start()``
    self-registers its secret), then the configured ``MTLS_PROXY_SECRET``
    (external-proxy topology). Everything else — missing attestation, no secret
    configured, malformed CN — resolves to ``None``.
    """
    attest = request.headers.get(ATTEST_HEADER, "")
    if not attest:
        return None
    # Compare on bytes: a genuine attestation is ASCII hex and encodes
    # identically, while a non-ASCII header value simply fails to match
    # instead of raising TypeError inside compare_digest — fail closed.
    attest_bytes = attest.encode("utf-8", "replace")
    ok = _attestation_registered(attest_bytes)
    if not ok:
        proxy_secret = _configured_proxy_secret()
        if proxy_secret:
            ok = hmac.compare_digest(attest_bytes, proxy_secret.encode("utf-8"))
    if not ok:
        return None
    common_name = request.headers.get(CN_HEADER, "").strip()
    if not common_name or len(common_name) > _MAX_CN_LEN:
        return None
    return ClientCertIdentity(
        common_name=common_name,
        fingerprint_sha256=request.headers.get(FINGERPRINT_HEADER, ""),
    )


# Refcount of the loopback hosts mTLS installs have added to
# DEFAULTS["TRUSTED_PROXIES"]. A host is mTLS-managed iff it appears here with a
# positive count; a host present in TRUSTED_PROXIES but ABSENT here is operator-
# owned and is never touched. Refcounting is what keeps two in-process
# terminators sharing a loopback upstream correct: each install depends on (and
# increments) the hosts it needs, so the first to shut down decrements rather
# than removing a host the second is still serving on. Guarded by a lock for the
# free-threaded runtime, where installs/shutdowns can run concurrently.
_LOOPBACK_TRUST_REFCOUNT: dict[str, int] = {}
_LOOPBACK_TRUST_LOCK = threading.Lock()


def _loopback_trust_hosts(upstream_host: str) -> set[str]:
    hosts = {upstream_host}
    if upstream_host in _LOOPBACK_HOSTS:
        # "localhost" resolves to either loopback IP; trust the concrete
        # addresses the terminator->upstream connection can present as the peer.
        hosts.update({"127.0.0.1", "::1"})
    return hosts


def _trust_loopback_upstream(upstream_host: str) -> list[str]:
    """Trust the terminator's loopback upstream as a proxy for client-IP purposes.

    The terminator forwards over loopback and injects ``X-Real-IP`` with the real
    client address, but :func:`hyperdjango.client_ip.resolve_client_ip` honors a
    forwarding header only when the socket peer is a trusted proxy (or
    ``TRUSTED_PROXY_COUNT`` > 0). The app's socket peer is always the loopback
    upstream, so without trusting it every request's ``client_ip`` collapses to
    the loopback address — one rate-limit bucket for every caller.

    This adds the loopback upstream address to ``TRUSTED_PROXIES`` at the DEFAULTS
    layer, the same programmatic-config layer app wiring uses for
    ``DATABASE_URL`` / ``SECRET_KEY`` / ``ALLOWED_HOSTS``. It composes with the
    built-in default but is shadowed by an explicit env / Django
    ``TRUSTED_PROXIES`` (get_setting precedence), so an operator who configures
    proxy trust by hand owns that policy and must list the loopback upstream
    themselves.

    Additions are refcounted: this returns every host the install now DEPENDS on
    (whether or not this call is the one that first added it), so
    :func:`_untrust_loopback_upstream` decrements exactly those on shutdown and a
    host is removed only when the last install depending on it goes away. A host
    already present but NOT mTLS-managed is operator-owned and left untouched
    (never recorded as a dependency, so it is never later removed).
    """
    depends: list[str] = []
    with _LOOPBACK_TRUST_LOCK:
        current = list(DEFAULTS.get("TRUSTED_PROXIES") or [])
        for host in _loopback_trust_hosts(upstream_host):
            if host in _LOOPBACK_TRUST_REFCOUNT:
                # A sibling install already added this host — share it.
                _LOOPBACK_TRUST_REFCOUNT[host] += 1
                depends.append(host)
            elif host not in current:
                # Not trusted at all: mTLS adds and now owns this host.
                _LOOPBACK_TRUST_REFCOUNT[host] = 1
                current.append(host)
                depends.append(host)
            # else: present but operator-owned — do not manage or depend on it.
        DEFAULTS["TRUSTED_PROXIES"] = current
    return depends


def _untrust_loopback_upstream(hosts: list[str]) -> None:
    """Release the loopback hosts a prior :func:`_trust_loopback_upstream` recorded.

    Only the hosts this install actually depended on are passed in. Each is
    decremented, and removed from ``TRUSTED_PROXIES`` only when its refcount
    reaches zero — so a sibling install still serving the same loopback upstream
    keeps it trusted, and an operator's own entries (never refcounted) are never
    removed. The mTLS front door cleans up exactly what it installed and nothing
    else, regardless of shutdown order.
    """
    if not hosts:
        return
    with _LOOPBACK_TRUST_LOCK:
        current = list(DEFAULTS.get("TRUSTED_PROXIES") or [])
        changed = False
        for host in hosts:
            count = _LOOPBACK_TRUST_REFCOUNT.get(host, 0)
            if count <= 0:
                continue
            if count == 1:
                del _LOOPBACK_TRUST_REFCOUNT[host]
                if host in current:
                    current.remove(host)
                    changed = True
            else:
                _LOOPBACK_TRUST_REFCOUNT[host] = count - 1
        if changed:
            DEFAULTS["TRUSTED_PROXIES"] = current


# ---------------------------------------------------------------------------
# In-process TLS terminator
# ---------------------------------------------------------------------------


class MTLSTerminator:
    """TLS-terminating client-certificate authentication gateway.

    Runs on a dedicated thread + event loop (independent of the serving
    loops). Per client connection it does exactly four things and makes zero
    HTTP framing decisions:

    1. terminate TLS and let the handshake enforce chain trust
       (``CERT_REQUIRED`` against the CA);
    2. derive the identity from the peer certificate with ``cryptography`` —
       common name and SHA-256 fingerprint — and fail closed if the
       certificate is outside its validity window or carries an Extended Key
       Usage that omits clientAuth;
    3. read only up to the end of the request head, lexically strip inbound
       reserved identity headers and the client's ``Connection`` header, append
       the attested identity plus exactly one ``Connection`` header (``Upgrade``
       for a WebSocket head, otherwise ``close``), and write that head to a
       fresh loopback connection to the plaintext native server;
    4. splice the remaining bytes opaquely in both directions until close.

    The native server owns all framing, keep-alive, and body-length decisions.
    ``Connection: close`` means one request per upstream connection, so there
    is never a second request boundary to find; a WebSocket head instead gets a
    single ``Connection: Upgrade`` so the native server upgrades — everything
    after the head is opaque either way. One TLS connection per
    request is appropriate for this low-volume authentication path: stdlib
    ``ssl`` session resumption (tickets) makes client reconnects cheap
    abbreviated handshakes.
    """

    def __init__(
        self,
        *,
        listen_host: str,
        listen_port: int,
        upstream_host: str,
        upstream_port: int,
        certfile: str,
        keyfile: str,
        ca_file: str,
        max_connections: int = 512,
        idle_timeout: float = 60.0,
        upgrade_idle_timeout: float = 300.0,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        # Backpressure + slow-loris limits. max_connections caps concurrent
        # client sockets (excess are shed immediately); idle_timeout reaps a
        # peer that stalls mid-request. An upgraded connection uses the larger
        # upgrade_idle_timeout ceiling instead — a genuine WebSocket may idle
        # legitimately between events, but a client that sends an Upgrade head
        # and then goes silent WITHOUT ever establishing a real WebSocket must
        # still be reaped so it cannot pin a slot untimed. Both directions'
        # bytes stamp the same liveness clock, so a live feed keeps resetting it.
        self.max_connections = max_connections
        self.idle_timeout = idle_timeout
        self.upgrade_idle_timeout = upgrade_idle_timeout
        # upgrade_idle_timeout is meant to be the LARGER ceiling — a genuine
        # WebSocket may idle between events far longer than a plain connection
        # should. If an operator inverts them, an upgraded feed is reaped SOONER
        # than a plain connection: it still fails safe (nothing is under-reaped),
        # but violates intent. Warn and preserve the configured value rather than
        # silently clamping — an operator's explicit numbers are honored, and a
        # deliberately small upgrade ceiling (e.g. a test) is not rewritten.
        if upgrade_idle_timeout < idle_timeout:
            logger.warning(
                "mTLS upgrade_idle_timeout ({u}s) is below idle_timeout ({i}s): "
                "an upgraded WebSocket will be reaped sooner than a plain "
                "connection, the opposite of intent. The configured value is "
                "preserved — set upgrade_idle_timeout >= idle_timeout (it is the "
                "larger ceiling).",
                u=upgrade_idle_timeout,
                i=idle_timeout,
            )
        self._active = 0
        # The plaintext upstream must be unreachable except from this
        # terminator: a client that can reach it directly bypasses TLS and can
        # forge the identity headers. We can't bind it for the app, but we warn
        # loudly when it isn't loopback.
        if upstream_host not in _LOOPBACK_HOSTS:
            logger.warning(
                "mTLS terminator upstream {h} is not loopback — bind the "
                "plaintext port to loopback (or firewall it to this host), or "
                "callers can skip TLS and present forged identity headers.",
                h=upstream_host,
            )
        self.attestation_secret = _secrets.token_hex(32)
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ctx.load_cert_chain(certfile, keyfile)
        self._ctx.load_verify_locations(cafile=ca_file)
        self._ctx.verify_mode = ssl.CERT_REQUIRED
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self.bound_port: int | None = None

    @classmethod
    def from_config(
        cls,
        *,
        upstream_port: int,
        listen_port: int,
        cert_file: str,
        key_file: str,
        ca_file: str,
        listen_host: str = "0.0.0.0",
        upstream_host: str = "127.0.0.1",
        max_connections: int = 512,
        idle_timeout: float = 60.0,
        upgrade_idle_timeout: float = 300.0,
        start: bool = True,
    ) -> MTLSTerminator | None:
        """Build (and by default start) a terminator from an app's ``mtls_*``
        settings, or return ``None`` when mTLS is disabled.

        mTLS is enabled iff both ``listen_port`` and ``cert_file`` are set —
        the same condition both services hand-rolled around the constructor.
        This helper collapses that ``if enabled: MTLSTerminator(...); .start()``
        block into one call and makes the port-desync bug impossible:
        ``upstream_port`` is REQUIRED, so a caller must pass the app's ACTUAL
        bound plaintext port. The old inline wiring defaulted it to a constant
        that silently pointed the terminator at the wrong port whenever the app
        moved — this signature has no such default to drift from.

        The ``cert_file`` / ``key_file`` / ``ca_file`` names mirror the config
        bundle; they map onto the constructor's ``certfile`` / ``keyfile`` /
        ``ca_file``. When started, the returned terminator has already
        registered its attestation secret (see ``start()``), so
        ``resolve_client_cert`` recognizes its identity headers without any
        further wiring.
        """
        if not (listen_port and cert_file):
            return None
        terminator = cls(
            listen_host=listen_host,
            listen_port=listen_port,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
            certfile=cert_file,
            keyfile=key_file,
            ca_file=ca_file,
            max_connections=max_connections,
            idle_timeout=idle_timeout,
            upgrade_idle_timeout=upgrade_idle_timeout,
        )
        if start:
            terminator.start()
        return terminator

    @classmethod
    def install(
        cls,
        app,
        *,
        listen_port: int,
        cert_file: str,
        key_file: str,
        ca_file: str,
        listen_host: str = "0.0.0.0",
        upstream_host: str = "127.0.0.1",
        max_connections: int = 512,
        idle_timeout: float = 60.0,
        upgrade_idle_timeout: float = 300.0,
        trust_upstream_ip: bool = True,
        health_check_name: str = "mtls_terminator",
    ) -> InstalledMTLS:
        """Wire an mTLS terminator into ``app``'s lifecycle in one call.

        This is the one-liner both serving topologies use to adopt the
        terminator. It registers three things on the framework ``app`` and
        returns a live :class:`InstalledMTLS` handle whose ``terminator``
        attribute is the running terminator once startup has run (``None`` before
        startup, when mTLS is disabled, or after shutdown):

        - an ``on_startup`` hook that builds the terminator with
          :meth:`from_config`, passing the app's ACTUAL bound plaintext port
          (``app.bound_port``, published before startup hooks run) as the
          upstream — so moving the app port can never desync the terminator, and
          mTLS stays disabled (the handle's ``terminator`` is ``None``) whenever
          ``listen_port`` or ``cert_file`` is unset. When mTLS is enabled the hook
          fails loudly if ``app.bound_port`` is falsy: the app was bound on an
          ephemeral port (``PORT=0``) whose real value the native server does not
          expose, so the terminator has no known upstream to forward to;
        - a readiness check named ``health_check_name`` that fails when the
          terminator front door has died, so ``/ready`` reflects a dead front
          door instead of reporting healthy while no mTLS traffic can land;
        - an ``on_shutdown`` hook that stops the terminator (deregistering its
          attestation) on the way down.

        ``trust_upstream_ip`` (default ``True``) makes the terminator's loopback
        upstream a trusted proxy so ``request.client_ip`` reflects the
        ``X-Real-IP`` the terminator injects. The terminator forwards over
        loopback, so without this every request's socket peer is the loopback
        upstream and ``client_ip`` collapses to ``127.0.0.1`` — one bucket for all
        callers, defeating per-IP rate limiting. It adds the loopback upstream
        address to the ``TRUSTED_PROXIES`` authority at the DEFAULTS layer (the
        same programmatic-config layer as ``DATABASE_URL`` / ``SECRET_KEY``);
        it therefore composes with the default but does NOT override an operator
        who has set ``TRUSTED_PROXIES`` explicitly (env / Django) — that operator
        owns the proxy-trust policy and must list the loopback upstream
        themselves. Set it ``False`` to leave global proxy trust untouched.

        The framework stays app-agnostic: it takes the app instance and plain
        parameters and references no application code. The app object must expose
        ``on_startup`` / ``on_shutdown`` decorators, ``add_health_check(name,
        check)``, and a ``bound_port`` attribute. See ``docs/mtls.md``.
        """
        handle = InstalledMTLS()

        @app.on_startup
        async def _mtls_startup() -> None:
            if listen_port and cert_file and not app.bound_port:
                raise MTLSError(
                    "mTLS terminator cannot forward to app.bound_port="
                    f"{app.bound_port!r}: the app is bound on an ephemeral port "
                    "(PORT=0) whose real value the native server does not "
                    "expose. Bind the app to a fixed plaintext port so the "
                    "terminator upstream is known."
                )
            handle.terminator = cls.from_config(
                upstream_port=app.bound_port,
                listen_port=listen_port,
                cert_file=cert_file,
                key_file=key_file,
                ca_file=ca_file,
                listen_host=listen_host,
                upstream_host=upstream_host,
                max_connections=max_connections,
                idle_timeout=idle_timeout,
                upgrade_idle_timeout=upgrade_idle_timeout,
            )
            if handle.terminator is not None and trust_upstream_ip:
                handle.trusted_hosts = _trust_loopback_upstream(upstream_host)

        @app.on_shutdown
        async def _mtls_shutdown() -> None:
            if handle.terminator is not None:
                handle.terminator.stop()
            _untrust_loopback_upstream(handle.trusted_hosts)
            handle.trusted_hosts = []

        app.add_health_check(
            health_check_name,
            lambda: handle.terminator is None or handle.terminator.is_alive(),
        )
        return handle

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start listening on a dedicated thread. Blocks until bound.

        On a successful bind the listener thread registers the terminator's
        attestation secret in the process-level registry BEFORE it begins
        serving, so ``resolve_client_cert`` recognizes the identity headers this
        terminator injects from the very first request — with no window in which
        an early request resolves to a spurious 401 — and without the app
        threading the secret through by hand.

        Starting a terminator that is already running raises: a second ``start()``
        would overwrite the thread/loop/server references and orphan the first
        thread (and its still-registered attestation). A ``stop()`` then
        ``start()`` restart IS supported — the ready gate and any prior start
        error are reset here so the fresh listener thread is awaited cleanly.
        """
        if self._thread is not None and self._thread.is_alive():
            raise MTLSError(
                "MTLSTerminator is already running; call stop() before start()"
            )
        # Reset the one-shot start state so a restart (after stop()) or a retry
        # (after a failed bind) awaits the NEW listener thread rather than
        # returning instantly on a stale set gate or re-raising a stale error.
        self._ready.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run_thread, name="mtls-terminator", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise MTLSError("MTLS terminator failed to start within 10s")
        # A bind/startup crash on the listener thread records the exception and
        # releases the ready gate with no listener bound; surface it here so
        # callers fail fast at startup rather than discovering a dead front
        # door at runtime.
        if self._start_error is not None:
            raise self._start_error

    def is_alive(self) -> bool:
        """True when the listener thread is running and bound — use it in a
        readiness check so a dead terminator front-door is visible rather than
        the app reporting healthy while no mTLS traffic can land."""
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self.bound_port is not None
        )

    def stop(self) -> None:
        # Deregister first so no request can resolve this terminator's identity
        # once we begin tearing it down.
        _deregister_attestation(self.attestation_secret)
        loop = self._loop
        if loop is not None and loop.is_running():
            # Close the listener and cancel in-flight handler tasks on the loop
            # BEFORE stopping it, so no connection is abandoned mid-relay — which
            # would surface as "Task was destroyed" / "Event loop is closed"
            # noise. Bounded so a wedged handler cannot hang shutdown; the loop is
            # stopped regardless once the drain returns or times out.
            fut = asyncio.run_coroutine_threadsafe(self._drain(), loop)
            with contextlib.suppress(Exception):
                fut.result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    async def _drain(self) -> None:
        """Close the listener and cancel every outstanding handler task.

        Runs on the terminator's own loop from ``stop()``. Cancelling the
        per-connection handlers lets each run its ``finally`` (releasing its
        ``_active`` slot and closing both sockets) instead of being torn down
        with the loop, so shutdown is clean even with live connections.
        """
        # Stop accepting first, then cancel the in-flight handlers and drain
        # them. Cancelling BEFORE waiting is essential: Server.wait_closed()
        # blocks on active connections, so waiting on it while handlers are still
        # parked in the splice would deadlock the shutdown.
        if self._server is not None:
            self._server.close()
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Yield once so the transport-close callbacks the handlers scheduled in
        # their finally run on this loop, rather than being left for a __del__ on
        # the already-closed loop.
        await asyncio.sleep(0)

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            server = loop.run_until_complete(
                asyncio.start_server(
                    self._handle_client,
                    self.listen_host,
                    self.listen_port,
                    ssl=self._ctx,
                )
            )
            self._server = server
            self.bound_port = server.sockets[0].getsockname()[1]
            logger.info(
                "mTLS terminator listening on {h}:{p} → {uh}:{up}",
                h=self.listen_host,
                p=self.bound_port,
                uh=self.upstream_host,
                up=self.upstream_port,
            )
            # Register the attestation secret BEFORE serving begins. The listener
            # is bound but run_forever has not started dispatching handlers, so no
            # request can be processed until the secret is resolvable. This closes
            # the window (secret registered only after start() returned) in which
            # an early request resolved to a spurious 401.
            _register_attestation(self.attestation_secret)
            self._ready.set()
            loop.run_forever()
        # blind-except: a listener-thread crash must release start(), be
        # visible in logs, and be re-raised from start() rather than hanging
        # the process or silently reporting a bound listener that isn't there
        except Exception as exc:
            self._start_error = exc
            logger.error("mTLS terminator failed: {err}", err=exc)
            self._ready.set()
        finally:
            # Deregister here too, not only in stop(): if run_forever() raises
            # after _ready.set(), stop() may never run, and a lingering secret in
            # the registry would keep resolve_client_cert trusting a dead front
            # door for the life of the process. Idempotent with stop()'s
            # deregister (discard is safe to call twice).
            _deregister_attestation(self.attestation_secret)
            if self._server is not None:
                self._server.close()
            loop.close()

    # -- per-connection relay -------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Shed load past the cap before doing any per-connection work. The
        # terminator loop is single-threaded, so this counter needs no lock.
        if self._active >= self.max_connections:
            MTLS_CONNECTIONS_SHED.inc()
            writer.close()
            return

        # Reserve the slot immediately — before the first await — so a burst of
        # simultaneous accepts cannot all clear the cap check before any of them
        # increments. The try/finally below guarantees exactly one decrement on
        # every exit path from here on.
        self._active += 1
        MTLS_ACTIVE.set(self._active)
        up_writer: asyncio.StreamWriter | None = None
        try:
            peer = writer.get_extra_info("peername")
            peer_ip = peer[0] if peer else ""
            # _peer_identity counts the outcome (handshake-with-no-identity or
            # an identity-policy rejection) itself, so a None here is already
            # accounted for — just drop the connection.
            identity = self._peer_identity(writer)
            if identity is None:
                return
            common_name, fingerprint = identity

            try:
                up_reader, up_writer = await asyncio.open_connection(
                    self.upstream_host, self.upstream_port
                )
            except OSError as exc:
                MTLS_UPSTREAM_FAILURES.inc()
                logger.error("mTLS terminator: upstream connect failed: {err}", err=exc)
                return

            # Attested identity headers only; rewrite_request_head appends the
            # single Connection header (close, or Upgrade for a WebSocket) after
            # stripping the client's inbound Connection so exactly one reaches
            # the native server (the sole framing authority).
            injected = (
                f"{ATTEST_HEADER}: {self.attestation_secret}\r\n"
                f"{CN_HEADER}: {common_name}\r\n"
                f"{FINGERPRINT_HEADER}: {fingerprint}\r\n"
                f"x-real-ip: {peer_ip}\r\n"
            ).encode()

            # Relay both directions concurrently and tear the connection down as
            # soon as EITHER direction ends. When the upstream EOFs (the injected
            # Connection: close makes it EOF right after the response) _pump
            # returns; the client half then splices to nothing, so without this
            # coupling the client->upstream relay would block forever on a peer
            # that holds its socket open — pinning the _active slot and, at the
            # cap, wedging the gateway. Cancelling the sibling on first-completion
            # (plus the writer close in the outer finally) releases the slot.
            # Shared liveness stamp: either direction's bytes count as
            # activity, so the idle reaper measures whole-connection idleness.
            activity = [asyncio.get_running_loop().time()]
            relay = asyncio.ensure_future(
                self._relay(reader, up_writer, injected, activity)
            )
            pump = asyncio.ensure_future(self._pump(up_reader, writer, activity))
            try:
                await asyncio.wait({relay, pump}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (relay, pump):
                    task.cancel()
                # Drain the cancellations so neither task is destroyed while
                # pending; per-connection errors are absorbed here (they end this
                # connection only — the listener keeps serving).
                await asyncio.gather(relay, pump, return_exceptions=True)
        finally:
            self._active -= 1
            MTLS_ACTIVE.set(self._active)
            for w in (up_writer, writer):
                if w is not None:
                    with contextlib.suppress(OSError):
                        w.close()

    def _peer_identity(self, writer: asyncio.StreamWriter) -> tuple[str, str] | None:
        """Return ``(common_name, fingerprint)`` for the handshake-verified peer.

        The TLS handshake has already enforced chain trust. Here ``cryptography``
        loads the peer's leaf certificate to derive the identity and apply the
        two policy checks stdlib ``ssl`` does not express cleanly: the validity
        window (not-before/not-after in UTC) and, when an Extended Key Usage
        extension is present, that it includes clientAuth. Returns ``None`` and
        counts the reason (a handshake carrying no usable identity, or an
        identity-policy rejection) so the caller can simply close the socket.
        """
        ssl_obj = writer.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True) if ssl_obj is not None else None
        if not der:
            MTLS_HANDSHAKE_FAILURES.inc()
            return None
        try:
            cert = x509.load_der_x509_certificate(der)
        except ValueError:
            MTLS_HANDSHAKE_FAILURES.inc()
            return None

        # Validity window: fail closed outside [not_before, not_after]. Compared
        # in UTC so a naive-vs-aware mismatch can never silently pass.
        now = datetime.datetime.now(datetime.UTC)
        if now < cert.not_valid_before_utc or now > cert.not_valid_after_utc:
            MTLS_IDENTITY_POLICY_REJECTED.inc()
            return None

        # Extended Key Usage: when the certificate declares an EKU, it must name
        # clientAuth. A certificate with no EKU extension is unconstrained and
        # accepted (chain trust already vouched for it).
        try:
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            pass
        else:
            if x509.ExtendedKeyUsageOID.CLIENT_AUTH not in eku:
                MTLS_IDENTITY_POLICY_REJECTED.inc()
                return None

        cn_attrs = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        common_name = cn_attrs[0].value if cn_attrs else ""
        # The CN is injected into an HTTP header: refuse anything that could
        # split headers even though our own CA should never issue such a CN. The
        # length ceiling matches the resolver (_MAX_CN_LEN): a CN this side would
        # inject but resolve_client_cert would reject is worse than no identity —
        # refuse it here too so both ends agree and the connection fails closed.
        if (
            not isinstance(common_name, str)
            or not common_name
            or len(common_name) > _MAX_CN_LEN
            or any(c in common_name for c in "\r\n\x00")
        ):
            MTLS_HANDSHAKE_FAILURES.inc()
            return None
        return common_name, cert.fingerprint(hashes.SHA256()).hex()

    async def _pump(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        activity: list[float],
    ) -> None:
        """Stream upstream responses back to the client untouched.

        Every chunk stamps ``activity`` so the client-side idle reaper in
        ``_relay`` counts response bytes as liveness: a quiet client receiving
        a long streaming response (SSE, long-poll, slow upstream) must never
        be reaped while the response is still flowing.
        """
        try:
            loop = asyncio.get_running_loop()
            while True:
                data = await src.read(65536)
                if not data:
                    break
                activity[0] = loop.time()
                dst.write(data)
                await dst.drain()
        except ConnectionError, asyncio.CancelledError, OSError:
            pass

    async def _read(
        self, reader: asyncio.StreamReader, size: int = 65536, timeout: float = 0.0
    ) -> bytes:
        """Read a chunk bounded by a per-read timeout — a peer that stalls
        mid-request is reaped rather than parking a connection forever. A
        ``timeout`` of 0 uses ``idle_timeout``. Returns ``b""`` on EOF or
        timeout."""
        try:
            return await asyncio.wait_for(
                reader.read(size), timeout or self.idle_timeout
            )
        except TimeoutError:
            return b""

    async def _read_idle(
        self, reader: asyncio.StreamReader, activity: list[float], timeout: float
    ) -> bytes:
        """Read a client chunk, reaping only on WHOLE-CONNECTION idleness.

        Each wait is bounded by ``timeout``, but a timeout alone is not idleness:
        ``activity`` is stamped by both directions (client bytes here, response
        bytes in ``_pump``), and the connection is reaped only when neither side
        has moved for a full ``timeout``. This keeps the body-phase slow-loris
        reap while never truncating a live streaming response to a legitimately
        quiet client. The caller passes ``idle_timeout`` for a non-upgrade
        connection and the larger ``upgrade_idle_timeout`` for an upgraded one, so
        a genuine WebSocket survives long idle gaps between events while a never-
        upgraded silent peer is still reaped. Returns ``b""`` on EOF or on
        bidirectional idle expiry.
        """
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout)
            except TimeoutError:
                if loop.time() - activity[0] >= timeout:
                    return b""
                continue
            if data:
                activity[0] = loop.time()
            return data

    async def _relay(
        self,
        reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
        injected: bytes,
        activity: list[float],
    ) -> None:
        """Rewrite the request head, then splice the rest of the connection.

        Reads only to the end of the head (the first ``CRLFCRLF``) under the
        idle timeout — a peer that stalls mid-head is reaped — and the head cap.
        After forwarding the rewritten head, everything is opaque bytes: the
        client half of the connection is spliced straight to the upstream while
        ``_pump`` splices the upstream half back. No framing is interpreted; the
        native server decides where the body ends, and ``Connection: close``
        guarantees a single request on this connection.

        The splice policy depends on whether the head was a WebSocket upgrade.
        Either way the client half is reaped when the WHOLE connection has been
        idle — client bytes and response bytes (stamped by ``_pump``) both count
        as liveness — so a completed-head-then-stall slow-loris is reaped and its
        slot released, while a quiet client receiving a long streaming response is
        not. A non-upgrade connection uses ``idle_timeout``; an upgrade uses the
        larger ``upgrade_idle_timeout`` so a genuine WebSocket survives long idle
        gaps between events, yet a client that sends an ``Upgrade`` head and then
        goes silent without ever establishing a real feed is still reaped rather
        than pinning a slot untimed.

        Head assembly is bounded twice: by each read's idle timeout AND by a
        cumulative wall-clock budget (``idle_timeout`` total), so a peer dribbling
        a byte at a time just under the per-read idle can no longer stretch the
        head phase indefinitely.
        """
        loop = asyncio.get_running_loop()
        head_deadline = loop.time() + self.idle_timeout
        buf = b""
        head_end = buf.find(b"\r\n\r\n")
        while head_end < 0:
            if len(buf) > _MAX_HEAD_BYTES:
                return
            remaining = head_deadline - loop.time()
            if remaining <= 0:
                # Cumulative head budget exhausted: a slow dribble that never
                # trips the per-read idle is reaped on total elapsed time.
                return
            chunk = await self._read(reader, timeout=min(self.idle_timeout, remaining))
            if not chunk:
                return
            buf += chunk
            head_end = buf.find(b"\r\n\r\n")
        head, rest = buf[:head_end], buf[head_end + 4 :]

        result = rewrite_request_head(head, injected)
        if result is None:
            # obs-fold or otherwise un-splice-able head — close, do not guess.
            return
        rewritten, has_upgrade = result
        up_writer.write(rewritten)
        if rest:
            up_writer.write(rest)
        await up_writer.drain()

        # Opaque bidirectional splice of the client half, reaped on whole-
        # connection idleness (_read_idle returns b"" once BOTH directions have
        # been quiet for the applicable ceiling), so a completed-head-then-stall
        # peer frees its slot while a live streaming response keeps the connection
        # alive. A WebSocket upgrade uses the larger upgrade_idle_timeout so it
        # may idle legitimately between events, while a client that sent an
        # Upgrade head but never established a real feed is still reaped.
        splice_timeout = self.upgrade_idle_timeout if has_upgrade else self.idle_timeout
        while True:
            data = await self._read_idle(reader, activity, splice_timeout)
            if not data:
                return
            up_writer.write(data)
            await up_writer.drain()


@dataclass(slots=True)
class InstalledMTLS:
    """Live handle returned by :meth:`MTLSTerminator.install`.

    ``terminator`` is the running :class:`MTLSTerminator` once the app's startup
    hook has run — ``None`` before startup, when mTLS is disabled, or after
    shutdown. The readiness check ``install`` registers reads it, so a dead
    front door surfaces on ``/ready`` rather than the app reporting healthy while
    no mTLS traffic can land.
    """

    terminator: MTLSTerminator | None = None
    # Loopback hosts this install depends on for TRUSTED_PROXIES (refcounted), so
    # shutdown decrements exactly its own dependencies — never a host an operator
    # owns, and never one a sibling install is still serving on.
    trusted_hosts: list[str] = field(default_factory=list)
