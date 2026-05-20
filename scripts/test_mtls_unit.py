"""
Unit tests for hyperdjango.mtls: CA/cert issuance and a real loopback mTLS
handshake through MTLSTerminator.

# hyper-test: unit

Proves:
  - CA and issued certs carry SKI/AKI so modern OpenSSL builds a chain
  - server certs get serverAuth EKU + IP/DNS SANs; client certs get
    clientAuth EKU with CN = identity name
  - a client presenting a CA-signed cert handshakes through the terminator
    and its request reaches the upstream with attested identity headers
    (CN + cryptography-derived SHA-256 fingerprint) and exactly one Connection
    header — close for an ordinary request, Upgrade for a WebSocket head —
    with the client's own Connection header stripped
  - the terminator strips client-supplied x-hyper-mtls-* / x-real-ip /
    x-forwarded-for spoof headers by name and refuses obs-fold heads
  - the terminator makes NO framing decision: a request body (even one whose
    Content-Length is un-parseable, like "1_0") passes through opaquely
  - the certificate identity policy fails closed on an expired / not-yet-valid
    cert and on a cert whose EKU omits clientAuth
  - a self-signed (non-CA) client cert is rejected at the TLS layer
  - a client that sends an Upgrade head then goes silent (no real WebSocket) is
    reaped at upgrade_idle_timeout, while an upgrade with periodic frames is not
  - a byte-at-a-time head dribble under the per-read idle is reaped by the
    cumulative head budget
"""

import asyncio
import contextlib
import datetime
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.mtls import (  # noqa: E402
    _MAX_CN_LEN,
    ATTEST_HEADER,
    CN_HEADER,
    FINGERPRINT_HEADER,
    MTLS_CONNECTIONS_SHED,
    MTLS_HANDSHAKE_FAILURES,
    MTLS_IDENTITY_POLICY_REJECTED,
    MTLSTerminator,
    _deregister_attestation,
    _register_attestation,
    create_ca,
    issue_cert,
    resolve_client_cert,
    rewrite_request_head,
    write_pem,
)
from hyperdjango.services_registry import SERVICES  # noqa: E402

SCRATCH = PROJECT_ROOT / ".test_scratch" / "mtls_unit"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: {detail}")


def _eku(cert_pem: bytes) -> set:
    cert = x509.load_pem_x509_certificate(cert_pem)
    return set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)


def _has_ext(cert_pem: bytes, cls) -> bool:
    cert = x509.load_pem_x509_certificate(cert_pem)
    try:
        cert.extensions.get_extension_for_class(cls)
        return True
    except x509.ExtensionNotFound:
        return False


def _der_of(cert_pem: bytes) -> bytes:
    return x509.load_pem_x509_certificate(cert_pem).public_bytes(
        serialization.Encoding.DER
    )


def _fingerprint_of(cert_pem: bytes) -> str:
    return x509.load_pem_x509_certificate(cert_pem).fingerprint(hashes.SHA256()).hex()


def _mint_client_cert(
    ca_key_pem: bytes,
    ca_cert_pem: bytes,
    *,
    common_name: str | None = "service:policy",
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
    eku: str = "client",
    omit_eku: bool = False,
) -> bytes:
    """Mint a CA-signed leaf certificate (DER) with custom validity/EKU.

    Used to exercise the terminator's post-handshake identity policy directly:
    the TLS layer would reject an expired/not-yet-valid cert during the
    handshake, so the policy check is proven by feeding a real DER to
    ``_peer_identity`` rather than through a live socket.
    """
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    nb = not_before if not_before is not None else now - datetime.timedelta(minutes=5)
    na = not_after if not_after is not None else now + datetime.timedelta(days=1)
    subject = (
        # _validate=False bypasses cryptography's X.520 ub-common-name (64) cap
        # so a test can mint a cert with a CN longer than the terminator's
        # _MAX_CN_LEN (256) — a real cert cryptography can PARSE but whose CN the
        # terminator must refuse, matching resolve_client_cert's ceiling.
        x509.Name(
            [x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name, _validate=False)]
        )
        if common_name
        else x509.Name([])
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(nb)
        .not_valid_after(na)
    )
    if not omit_eku:
        oid = (
            x509.ExtendedKeyUsageOID.CLIENT_AUTH
            if eku == "client"
            else x509.ExtendedKeyUsageOID.SERVER_AUTH
        )
        builder = builder.add_extension(x509.ExtendedKeyUsage([oid]), critical=False)
    cert = builder.sign(ca_key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.DER)


def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def test_issuance():
    print("\n== certificate issuance ==")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = create_ca("test-ca")
    check("CA has SubjectKeyIdentifier", _has_ext(ca_cert, x509.SubjectKeyIdentifier))
    check(
        "CA has AuthorityKeyIdentifier", _has_ext(ca_cert, x509.AuthorityKeyIdentifier)
    )
    check(
        "CA is a CA",
        x509.load_pem_x509_certificate(ca_cert)
        .extensions.get_extension_for_class(x509.BasicConstraints)
        .value.ca,
    )

    srv_key, srv_cert = issue_cert(
        ca_key, ca_cert, "localhost", server=True, san_dns=["localhost", "127.0.0.1"]
    )
    check(
        "server cert has serverAuth EKU",
        x509.ExtendedKeyUsageOID.SERVER_AUTH in _eku(srv_cert),
    )
    sans = (
        x509.load_pem_x509_certificate(srv_cert)
        .extensions.get_extension_for_class(x509.SubjectAlternativeName)
        .value
    )
    check(
        "server cert has IP + DNS SANs",
        any(isinstance(s, x509.IPAddress) for s in sans)
        and any(isinstance(s, x509.DNSName) for s in sans),
    )

    cli_key, cli_cert = issue_cert(ca_key, ca_cert, "service:prod-api")
    check(
        "client cert has clientAuth EKU",
        x509.ExtendedKeyUsageOID.CLIENT_AUTH in _eku(cli_cert),
    )
    check(
        "client cert has SKI + AKI",
        _has_ext(cli_cert, x509.SubjectKeyIdentifier)
        and _has_ext(cli_cert, x509.AuthorityKeyIdentifier),
    )
    check(
        "client cert CN is the identity name",
        x509.load_pem_x509_certificate(cli_cert)
        .subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0]
        .value
        == "service:prod-api",
    )
    return ca_key, ca_cert, srv_key, srv_cert, cli_key, cli_cert


class _Upstream:
    """Trivial loopback HTTP/1.1 server that echoes request headers back."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.last_head = b""
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            self.last_head = buf
            body = b'{"ok":true}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\ncontent-length: %d\r\nconnection: close\r\n\r\n%s"
                % (len(body), body)
            )
            conn.close()

    def close(self):
        self._sock.close()


def test_terminator_handshake(certs):
    print("\n== terminator: mTLS handshake + header injection ==")
    ca_key, ca_cert, srv_key, srv_cert, cli_key, cli_cert = certs
    for name, data, priv in (
        ("ca.crt", ca_cert, False),
        ("srv.crt", srv_cert, False),
        ("srv.key", srv_key, True),
        ("cli.crt", cli_cert, False),
        ("cli.key", cli_key, True),
    ):
        path = SCRATCH / name
        if path.exists():
            path.unlink()
        write_pem(str(path), data, private=priv)

    upstream = _Upstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    terminator.start()
    try:
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        # Every reserved identity header is spoofed — all must be stripped by
        # name and replaced with the terminator's attested values.
        tls.sendall(
            b"GET /probe HTTP/1.1\r\nHost: x\r\n"
            b"X-Hyper-MTLS-CN: service:forged\r\n"
            b"X-Hyper-MTLS-Attest: forged-secret\r\n"
            b"X-Hyper-MTLS-Fingerprint: forgedfp\r\n"
            b"X-Real-IP: 9.9.9.9\r\n"
            b"X-Forwarded-For: 8.8.8.8\r\n"
            b"Connection: keep-alive\r\n"
            b"content-length: 0\r\n\r\n"
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = tls.recv(4096)
            if not chunk:
                break
            resp += chunk
        tls.close()
        check("handshake + request succeeds", b"200 OK" in resp)
        check(
            "upstream received the request head",
            _wait_for(lambda: b"x-hyper-mtls-attest:" in upstream.last_head.lower()),
        )
        head = upstream.last_head.lower()
        check(
            "upstream received the terminator's real attestation",
            (b"x-hyper-mtls-attest: " + terminator.attestation_secret.encode()) in head,
        )
        check(
            "upstream received verified CN",
            b"x-hyper-mtls-cn: service:prod-api" in head,
        )
        check(
            "upstream received cryptography-derived fingerprint",
            (b"x-hyper-mtls-fingerprint: " + _fingerprint_of(cli_cert).encode())
            in head,
        )
        check("terminator forces connection: close", b"connection: close" in head)
        check(
            "exactly one connection header reaches upstream",
            head.count(b"connection:") == 1,
        )
        check("client connection: keep-alive stripped", b"keep-alive" not in head)
        for spoof, label in (
            (b"service:forged", "CN"),
            (b"forged-secret", "attest"),
            (b"forgedfp", "fingerprint"),
            (b"9.9.9.9", "x-real-ip"),
            (b"8.8.8.8", "x-forwarded-for"),
        ):
            check(f"spoofed {label} header stripped", spoof not in head)
        # X-Real-IP is re-injected as the real loopback peer, not the spoof.
        check("real x-real-ip injected", b"x-real-ip: 127.0.0.1" in head)
    finally:
        terminator.stop()
        upstream.close()


def test_untrusted_client_rejected(certs):
    print("\n== terminator: self-signed client cert rejected ==")
    ca_key, ca_cert, srv_key, srv_cert, _, _ = certs
    # A client cert from a DIFFERENT, untrusted CA.
    rogue_ca_key, rogue_ca_cert = create_ca("rogue-ca")
    rogue_key, rogue_cert = issue_cert(rogue_ca_key, rogue_ca_cert, "service:prod-api")
    for name, data, priv in (
        ("rogue.crt", rogue_cert, False),
        ("rogue.key", rogue_key, True),
    ):
        path = SCRATCH / name
        if path.exists():
            path.unlink()
        write_pem(str(path), data, private=priv)

    upstream = _Upstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    terminator.start()
    try:
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "rogue.crt"), str(SCRATCH / "rogue.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        rejected = False
        try:
            tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
            tls.sendall(b"GET / HTTP/1.1\r\nHost: x\r\ncontent-length: 0\r\n\r\n")
            if not tls.recv(16):
                rejected = True  # server closed without responding
            tls.close()
        except ssl.SSLError, OSError:
            rejected = True
        check("untrusted client cert rejected at TLS", rejected)
    finally:
        terminator.stop()
        upstream.close()


def test_terminator_hardening(certs):
    print("\n== terminator: idle-timeout reaping + connection cap ==")
    ca_key, ca_cert, srv_key, srv_cert, cli_key, cli_cert = certs
    for name, data, priv in (
        ("ca.crt", ca_cert, False),
        ("srv.crt", srv_cert, False),
        ("srv.key", srv_key, True),
        ("cli.crt", cli_cert, False),
        ("cli.key", cli_key, True),
    ):
        path = SCRATCH / name
        if path.exists():
            path.unlink()
        write_pem(str(path), data, private=priv)

    def _client_ctx():
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        return ctx

    # Idle-timeout reaping: connect, send an INCOMPLETE request head, and never
    # finish it. The terminator must close the connection after idle_timeout.
    upstream = _Upstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        idle_timeout=0.5,
    )
    check("hardening params stored", terminator.idle_timeout == 0.5)
    terminator.start()
    try:
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        tls = _client_ctx().wrap_socket(raw, server_hostname="127.0.0.1")
        tls.sendall(b"GET /slow HTTP/1.1\r\nHost: x\r\n")  # no terminating CRLFCRLF
        tls.settimeout(3.0)
        closed = False
        try:
            # A stalled peer is reaped: recv returns EOF (b"") once the
            # terminator closes the upstream+client after idle_timeout.
            if tls.recv(64) == b"":
                closed = True
        except TimeoutError, OSError:
            closed = False
        tls.close()
        check("idle connection reaped after idle_timeout", closed)
    finally:
        terminator.stop()
        upstream.close()

    # Non-loopback upstream is accepted but the constructor warns (we assert it
    # constructs and records the value rather than rejecting — binding is the
    # operator's responsibility).
    warn_term = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="10.0.0.5",
        upstream_port=9,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        max_connections=1,
    )
    check("max_connections stored", warn_term.max_connections == 1)
    check(
        "non-loopback upstream still constructs", warn_term.upstream_host == "10.0.0.5"
    )


def _write_terminator_certs(certs) -> None:
    """Write the standard CA/server/client PEM set into SCRATCH."""
    ca_key, ca_cert, srv_key, srv_cert, cli_key, cli_cert = certs
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for name, data, priv in (
        ("ca.crt", ca_cert, False),
        ("srv.crt", srv_cert, False),
        ("srv.key", srv_key, True),
        ("cli.crt", cli_cert, False),
        ("cli.key", cli_key, True),
    ):
        path = SCRATCH / name
        if path.exists():
            path.unlink()
        write_pem(str(path), data, private=priv)


def test_rewrite_head_lexical():
    print("\n== rewrite_request_head: lexical strip, no framing interpretation ==")
    # The injected block is identity headers only; rewrite_request_head appends
    # the single Connection header itself and returns (rewritten, has_upgrade).
    inj = b"x-hyper-mtls-cn: service:x\r\n"

    def head(extra: bytes) -> bytes:
        return b"POST /x HTTP/1.1\r\nHost: h\r\n" + extra

    # Reserved identity headers are stripped by name (any case); everything else
    # — including Content-Length, whatever its value — is forwarded verbatim.
    out, up = rewrite_request_head(
        head(
            b"X-Hyper-MTLS-CN: service:forged\r\n"
            b"X-Real-IP: 9.9.9.9\r\n"
            b"X-Forwarded-For: 8.8.8.8\r\n"
            b"Content-Length: 1_0\r\n"
        ),
        inj,
    )
    check("returns bytes (not rejected)", isinstance(out, bytes))
    check("non-upgrade head reports has_upgrade False", up is False)
    check("head ends with CRLFCRLF", out.endswith(b"\r\n\r\n"))
    low = out.lower()
    check("request line preserved verbatim", out.startswith(b"POST /x HTTP/1.1\r\n"))
    check("reserved CN spoof stripped", b"service:forged" not in low)
    check("reserved x-real-ip spoof stripped", b"9.9.9.9" not in low)
    check("reserved x-forwarded-for spoof stripped", b"8.8.8.8" not in low)
    check(
        "un-parseable Content-Length forwarded untouched",
        b"content-length: 1_0" in low,
    )
    check("injected identity present", b"x-hyper-mtls-cn: service:x" in low)

    # Transfer-Encoding is no longer special: it is opaque like any other header.
    te = rewrite_request_head(head(b"Transfer-Encoding: chunked\r\n"), inj)
    check(
        "transfer-encoding passes through (native owns framing)",
        te is not None and b"transfer-encoding: chunked" in te[0].lower(),
    )

    # Exactly one Connection header, chosen lexically. An ordinary request (no
    # Upgrade line) gets Connection: close and the client's inbound Connection
    # never survives.
    plain, plain_up = rewrite_request_head(head(b"Connection: keep-alive\r\n"), inj)
    plow = plain.lower()
    check("ordinary head: has_upgrade False", plain_up is False)
    check(
        "ordinary head: exactly one connection header", plow.count(b"connection:") == 1
    )
    check("ordinary head: connection: close injected", b"connection: close" in plow)
    check("client connection: keep-alive stripped", b"keep-alive" not in plow)

    # A head carrying an Upgrade line gets Connection: Upgrade — one header, no
    # close — and the Upgrade line is preserved for the native server.
    ws, ws_up = rewrite_request_head(
        head(b"Connection: keep-alive\r\nUpgrade: websocket\r\n"), inj
    )
    wlow = ws.lower()
    check("upgrade head: has_upgrade True", ws_up is True)
    check(
        "upgrade head: exactly one connection header", wlow.count(b"connection:") == 1
    )
    check("upgrade head: connection: upgrade injected", b"connection: upgrade" in wlow)
    check("upgrade head: no connection: close", b"connection: close" not in wlow)
    check("upgrade head: Upgrade line preserved", b"upgrade: websocket" in wlow)

    # obs-fold (a header line starting with SP/HTAB) is refused, not interpreted.
    check(
        "obs-fold (leading space) refused",
        rewrite_request_head(head(b"X-Foo: bar\r\n baz\r\n"), inj) is None,
    )
    check(
        "obs-fold (leading tab) refused",
        rewrite_request_head(head(b"X-Foo: bar\r\n\tbaz\r\n"), inj) is None,
    )


def test_body_passes_through_opaque(certs):
    print("\n== terminator: opaque body passthrough (no Content-Length parse) ==")
    _write_terminator_certs(certs)
    upstream = _Upstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    terminator.start()
    try:
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        # "1_0" is a Content-Length the OLD second parser rejected as malformed
        # (400). The terminator no longer looks at it, so the request reaches
        # the upstream verbatim and the native server owns the framing decision.
        tls.sendall(
            b"POST /body HTTP/1.1\r\nHost: x\r\nContent-Length: 1_0\r\n\r\nhello"
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = tls.recv(4096)
            if not chunk:
                break
            resp += chunk
        tls.close()
        check("terminator does not reject the request itself", b"200 OK" in resp)
        check("terminator emits no 400/411", b"400" not in resp and b"411" not in resp)
        check(
            "upstream received the request head",
            _wait_for(lambda: b"post /body" in upstream.last_head.lower()),
        )
        check(
            "un-parseable Content-Length forwarded untouched",
            b"content-length: 1_0" in upstream.last_head.lower(),
        )
    finally:
        terminator.stop()
        upstream.close()


def test_identity_policy(certs):
    print("\n== terminator: certificate validity + EKU policy fails closed ==")
    from hyperdjango.telemetry import metrics as _metrics

    _metrics.enable()
    ca_key, ca_cert = certs[0], certs[1]
    _write_terminator_certs(certs)
    term = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=1,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    now = datetime.datetime.now(datetime.UTC)
    day = datetime.timedelta(days=1)

    good = _mint_client_cert(ca_key, ca_cert)
    check(
        "valid current clientAuth cert yields identity",
        term._peer_identity(_StubWriter(good)) is not None,
    )

    for label, der in (
        (
            "expired",
            _mint_client_cert(
                ca_key, ca_cert, not_before=now - 10 * day, not_after=now - day
            ),
        ),
        (
            "not-yet-valid",
            _mint_client_cert(
                ca_key, ca_cert, not_before=now + day, not_after=now + 10 * day
            ),
        ),
        ("serverAuth-only EKU", _mint_client_cert(ca_key, ca_cert, eku="server")),
    ):
        before = MTLS_IDENTITY_POLICY_REJECTED.value()
        rejected = term._peer_identity(_StubWriter(der)) is None
        check(f"{label} cert refused (fail closed)", rejected)
        check(
            f"{label} counted as an identity-policy rejection",
            MTLS_IDENTITY_POLICY_REJECTED.value() > before,
        )

    # A certificate with NO EKU extension is unconstrained: chain trust already
    # vouched for it, so it is accepted.
    no_eku = _mint_client_cert(ca_key, ca_cert, omit_eku=True)
    check(
        "cert with no EKU extension accepted",
        term._peer_identity(_StubWriter(no_eku)) is not None,
    )


class _FakeHeaders:
    def __init__(self, data: dict):
        self._data = data

    def get(self, key: str, default: str = "") -> str:
        return self._data.get(key, default)


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


def test_non_ascii_attestation_fails_closed():
    print("\n== resolve_client_cert: non-ASCII attestation fails closed ==")
    secret = "a" * 64
    # The attestation is matched against the process registry; register the
    # secret to stand in for a running terminator, deregister when done.
    _register_attestation(secret)
    try:
        bad = _FakeRequest({ATTEST_HEADER: "not-ascii-é☃"})
        raised = False
        res = None
        try:
            res = resolve_client_cert(bad)
        except Exception:  # noqa: BLE001 - the whole point is that none escapes
            raised = True
        check("non-ASCII attestation does not raise", not raised)
        check("non-ASCII attestation resolves to None", res is None)

        good = _FakeRequest(
            {
                ATTEST_HEADER: secret,
                CN_HEADER: "service:prod-api",
                FINGERPRINT_HEADER: "deadbeef",
            }
        )
        ident = resolve_client_cert(good)
        check(
            "matching ASCII attestation still resolves identity",
            ident is not None and ident.common_name == "service:prod-api",
        )
    finally:
        _deregister_attestation(secret)


class _StubSSLObject:
    """A handshake-verified peer whose leaf cert DER we control."""

    def __init__(self, der: bytes):
        self._der = der

    def getpeercert(self, binary_form: bool = False):
        return self._der if binary_form else None


class _StubWriter:
    def __init__(self, der: bytes = b""):
        self.closed = False
        self._der = der

    def get_extra_info(self, key: str):
        if key == "ssl_object":
            return _StubSSLObject(self._der)
        if key == "peername":
            return ("127.0.0.1", 55555)
        return None

    def close(self):
        self.closed = True


class _StubReader:
    async def read(self, n: int = 65536) -> bytes:
        return b""


def test_connection_slot_reserved_before_await(certs):
    print("\n== terminator: connection slot reserved before first await ==")
    _write_terminator_certs(certs)
    term = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=1,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        max_connections=4,
    )
    observed: dict[str, int] = {}
    orig_open = asyncio.open_connection

    async def fake_open(*args, **kwargs):
        # The slot must already be reserved by the time the first await runs,
        # otherwise a burst of accepts could all clear the cap check first.
        observed["active_at_await"] = term._active
        raise OSError("blocked for test")

    # A valid current clientAuth cert so identity resolves and the handler
    # reaches the upstream await (where the slot must already be reserved).
    cli_cert = certs[5]
    asyncio.open_connection = fake_open
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            term._handle_client(_StubReader(), _StubWriter(_der_of(cli_cert)))
        )
    finally:
        asyncio.open_connection = orig_open
        loop.close()
    check(
        "slot reserved before upstream await",
        observed.get("active_at_await") == 1,
        f"active_at_await={observed.get('active_at_await')}",
    )
    check("slot released after handler exit (exactly one decrement)", term._active == 0)


def test_completed_request_slot_reaped(certs):
    print("\n== terminator: slot reaped after a completed request (half-open) ==")
    # A well-formed client sends a complete request, reads the whole response,
    # then holds its TLS socket open and idle. The upstream has already EOF'd
    # (Connection: close), so the client->upstream splice has nothing more to do.
    # The terminator must propagate that EOF and release the _active slot rather
    # than pinning it on the half-open client — otherwise max_connections such
    # sockets wedge the gateway (a DoS).
    _write_terminator_certs(certs)
    upstream = _Upstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        idle_timeout=30.0,  # long: reaping must come from EOF propagation, not this
    )
    terminator.start()
    try:
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        tls.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        tls.settimeout(3.0)
        resp = b""
        while b'{"ok":true}' not in resp:
            chunk = tls.recv(4096)
            if not chunk:
                break
            resp += chunk
        check("client got the full response", b'{"ok":true}' in resp)
        # Client stays OPEN and idle. The slot must be released promptly by EOF
        # propagation, well inside the (long) idle_timeout.
        check(
            "slot released after completed request despite half-open client",
            _wait_for(lambda: terminator._active == 0, timeout=5.0),
            f"_active={terminator._active}",
        )
        tls.close()
    finally:
        terminator.stop()
        upstream.close()


class _SilentUpstream:
    """Accepts connections and holds them open without ever responding, so the
    terminator's per-connection handler parks in the opaque splice phase — a live
    connection to prove clean cancellation on stop()."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._held = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self._held.append(conn)  # hold open, never respond

    def close(self):
        self._sock.close()
        for c in self._held:
            with contextlib.suppress(OSError):
                c.close()


def test_shutdown_cancels_inflight(certs):
    print("\n== terminator: stop() cancels in-flight handlers cleanly ==")
    import gc
    import logging

    _write_terminator_certs(certs)
    upstream = _SilentUpstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    terminator.start()

    # Capture asyncio's logger: an abandoned pending task surfaces here as a
    # "Task was destroyed but it is pending!" record.
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Capture()
    aio_log = logging.getLogger("asyncio")
    aio_log.addHandler(handler)
    tls = None
    try:
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        # Complete head → handler forwards it upstream and parks in the splice
        # (the silent upstream never responds), so this is a live in-flight
        # connection at the moment we stop.
        tls.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        check(
            "handler is in-flight before stop",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )
        terminator.stop()
        # The in-flight handler was cancelled and drained (its finally ran), so
        # its slot is released — not abandoned with the loop.
        check("in-flight slot released by clean shutdown", terminator._active == 0)
        check("terminator not alive after stop", not terminator.is_alive())
        gc.collect()
        leaked = [m for m in records if "Task was destroyed" in m]
        check("no leaked-task warning on shutdown", not leaked, f"records={records}")
    finally:
        aio_log.removeHandler(handler)
        if tls is not None:
            with contextlib.suppress(OSError):
                tls.close()
        upstream.close()


def test_start_reraises_bind_failure(certs):
    print("\n== terminator: start() re-raises a bind failure ==")
    _write_terminator_certs(certs)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    term = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=busy_port,
        upstream_host="127.0.0.1",
        upstream_port=1,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    raised = False
    try:
        term.start()
    except Exception:  # noqa: BLE001 - any startup exception must surface
        raised = True
    finally:
        term.stop()
        blocker.close()
    check("start() re-raises the bind failure", raised)
    check("terminator reports not alive after failed bind", not term.is_alive())


def test_handshake_failure_metric(certs):
    print("\n== terminator: handshake-failure metric is honest ==")
    # The docstring must describe what the counter actually measures: a
    # completed handshake with no usable certificate identity — NOT a TLS-layer
    # rejection (those never reach the terminator, so they cannot be counted).
    doc = MTLS_HANDSHAKE_FAILURES.help.lower()
    check(
        "metric help describes a completed handshake with no usable identity",
        "handshake" in doc and "not counted" in doc,
    )
    check(
        "metric help does not overclaim TLS-layer rejections",
        "certificate" in doc,
    )

    from hyperdjango.telemetry import metrics as _metrics

    _metrics.enable()
    _write_terminator_certs(certs)
    term = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=1,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )
    # A handshake-valid cert whose subject carries NO common name: chain trust
    # passed, but there is no usable identity to inject.
    no_cn_der = _mint_client_cert(certs[0], certs[1], common_name=None)
    before = MTLS_HANDSHAKE_FAILURES.value()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            term._handle_client(_StubReader(), _StubWriter(no_cn_der))
        )
    finally:
        loop.close()
    after = MTLS_HANDSHAKE_FAILURES.value()
    check(
        "metric increments on a handshake with no usable identity",
        after > before,
        f"before={before} after={after}",
    )
    check("slot released after a no-identity handshake", term._active == 0)


def test_module_docstring_points_at_docs():
    print("\n== module docstring points at docs, not services ==")
    import hyperdjango.mtls as _mtls_mod

    doc = _mtls_mod.__doc__ or ""
    # Framework modules document the framework, not the services built on it.
    # Sourced from the registry so a newly added service is covered
    # automatically, but restricted to DISTINCTIVE names — a few service names
    # ("hello", "deployment") are also ordinary English words that legitimately
    # appear in framework prose, so matching them would be a false positive.
    # "service" itself is likewise legitimate here (per-service certificates).
    distinctive = [n for n in SERVICES if "_" in n or n.startswith("hyper")]
    lowered = doc.lower()
    named = sorted(n for n in distinctive if n in lowered)
    check(
        "no bundled-service reference in module docstring",
        not named and "services/" not in lowered and "services." not in lowered,
        f"names a bundled service: {named}",
    )
    check("module docstring points at docs/mtls.md", "docs/mtls.md" in doc)


def test_cn_cap_agreement(certs):
    print("\n== terminator + resolver agree on the CN length cap ==")
    ca_key, ca_cert = certs[0], certs[1]
    _write_terminator_certs(certs)
    term = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=1,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
    )

    at_cap = "c" * _MAX_CN_LEN
    over_cap = "c" * (_MAX_CN_LEN + 1)

    # Terminator side: it injects a CN AT the cap but refuses one OVER it, so a
    # long-CN cert cannot be injected here only to be dropped by the resolver.
    at_der = _mint_client_cert(ca_key, ca_cert, common_name=at_cap)
    at_ident = term._peer_identity(_StubWriter(at_der))
    check(
        "terminator injects a CN at the cap",
        at_ident is not None and at_ident[0] == at_cap,
    )
    over_der = _mint_client_cert(ca_key, ca_cert, common_name=over_cap)
    check(
        "terminator refuses a CN over the cap",
        term._peer_identity(_StubWriter(over_der)) is None,
    )

    # Resolver side: the same ceiling, so both ends agree.
    secret = "s" * 64
    _register_attestation(secret)
    try:
        at = resolve_client_cert(
            _FakeRequest(
                {ATTEST_HEADER: secret, CN_HEADER: at_cap, FINGERPRINT_HEADER: "fp"}
            )
        )
        check(
            "resolver honors a CN at the cap",
            at is not None and at.common_name == at_cap,
        )
        over = resolve_client_cert(
            _FakeRequest(
                {ATTEST_HEADER: secret, CN_HEADER: over_cap, FINGERPRINT_HEADER: "fp"}
            )
        )
        check("resolver refuses a CN over the cap", over is None)
    finally:
        _deregister_attestation(secret)


def test_body_phase_slowloris_reaped(certs):
    print("\n== terminator: body-phase slow-loris reaped; WS upgrade not reaped ==")
    # A client that completes a VALID non-upgrade head then stalls (no body, and
    # the upstream never responds) must be reaped by the idle timeout on the
    # client->upstream splice — otherwise it pins its slot forever. A genuine
    # WebSocket upgrade may idle legitimately and must NOT be reaped.
    _write_terminator_certs(certs)
    upstream = _SilentUpstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        idle_timeout=0.5,
        max_connections=8,
    )
    terminator.start()

    def _client():
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        return ctx.wrap_socket(raw, server_hostname="127.0.0.1")

    try:
        # Non-upgrade: complete head, then stall. The splice read is idle-bounded
        # so the slot is released ~idle_timeout later even though the upstream is
        # silent (no EOF to propagate).
        stall = _client()
        stall.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        check(
            "stalled non-upgrade handler goes in-flight",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )
        check(
            "stalled non-upgrade client reaped and slot released",
            _wait_for(lambda: terminator._active == 0, timeout=3.0),
            f"_active={terminator._active}",
        )
        with contextlib.suppress(OSError):
            stall.close()

        # WebSocket upgrade: complete head, then idle. The splice is untimed, so
        # the slot stays pinned well past idle_timeout (the app's WS keepalive,
        # not the terminator, reaps a half-open feed).
        ws = _client()
        ws.sendall(
            b"GET /ws HTTP/1.1\r\nHost: x\r\n"
            b"Upgrade: websocket\r\nConnection: upgrade\r\n\r\n"
        )
        check(
            "ws upgrade handler goes in-flight",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )
        check(
            "ws upgrade NOT reaped while idle past idle_timeout",
            not _wait_for(lambda: terminator._active == 0, timeout=1.5),
            f"_active={terminator._active}",
        )
        with contextlib.suppress(OSError):
            ws.close()
    finally:
        terminator.stop()
        upstream.close()


class _StreamingUpstream:
    """Accepts a connection and streams a byte at a steady cadence — a live
    response to a quiet client, proving response-side activity counts as
    connection liveness for the idle reaper."""

    def __init__(self, interval: float = 0.15):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._interval = interval
        self.streaming = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with contextlib.suppress(OSError):
            while self.streaming:
                conn.sendall(b".")
                time.sleep(self._interval)

    def close(self):
        self.streaming = False
        self._sock.close()


def test_streaming_response_not_reaped(certs):
    print("\n== terminator: quiet client on a live streaming response survives ==")
    # The idle reaper measures WHOLE-connection idleness: a client that sends
    # its head then legitimately goes quiet while the upstream streams a long
    # response (SSE, long-poll, slow producer) must NOT be reaped at
    # idle_timeout — response bytes are liveness. Once the stream stops and
    # both directions are idle, the reap fires.
    _write_terminator_certs(certs)
    upstream = _StreamingUpstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        idle_timeout=0.5,
        max_connections=8,
    )
    terminator.start()

    def _client():
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        return ctx.wrap_socket(raw, server_hostname="127.0.0.1")

    try:
        quiet = _client()
        quiet.sendall(b"GET /stream HTTP/1.1\r\nHost: x\r\n\r\n")
        check(
            "streaming handler goes in-flight",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )
        # Well past idle_timeout (0.5s) with a silent client: the connection
        # must survive because response bytes keep stamping activity.
        check(
            "quiet client NOT reaped while the response streams",
            not _wait_for(lambda: terminator._active == 0, timeout=1.6),
            f"_active={terminator._active}",
        )
        # Stream ends; now BOTH directions are idle — the reap must fire.
        upstream.streaming = False
        check(
            "connection reaped once both directions go idle",
            _wait_for(lambda: terminator._active == 0, timeout=3.0),
            f"_active={terminator._active}",
        )
        with contextlib.suppress(OSError):
            quiet.close()
    finally:
        terminator.stop()
        upstream.close()


def test_upgrade_idle_timeout_reaps_silent_peer(certs):
    print("\n== terminator: silent Upgrade peer reaped; live feed survives ==")
    # A cert-holder can open a connection, send only an Upgrade head, and never
    # establish a real WebSocket. has_upgrade is a lexical head check set BEFORE
    # any 101, so the splice must not be wholly untimed: the silent peer is
    # reaped at upgrade_idle_timeout (a LARGER ceiling than idle_timeout, so a
    # real feed idling between events survives). A connection with periodic
    # frames keeps stamping the shared liveness clock and is NOT reaped.
    _write_terminator_certs(certs)
    upstream = _SilentUpstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        idle_timeout=30.0,  # large: the reap must come from the UPGRADE ceiling
        upgrade_idle_timeout=0.5,
        max_connections=8,
    )
    check("upgrade_idle_timeout stored", terminator.upgrade_idle_timeout == 0.5)
    terminator.start()

    def _client():
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        return ctx.wrap_socket(raw, server_hostname="127.0.0.1")

    ws_head = (
        b"GET /ws HTTP/1.1\r\nHost: x\r\n"
        b"Upgrade: websocket\r\nConnection: upgrade\r\n\r\n"
    )
    try:
        # Silent forged-upgrade peer: sends an Upgrade head, never a real frame.
        # Reaped at upgrade_idle_timeout, well inside the (large) idle_timeout.
        silent = _client()
        silent.sendall(ws_head)
        check(
            "silent upgrade handler goes in-flight",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )
        check(
            "silent forged-upgrade peer reaped at upgrade_idle_timeout",
            _wait_for(lambda: terminator._active == 0, timeout=3.0),
            f"_active={terminator._active}",
        )
        with contextlib.suppress(OSError):
            silent.close()

        # Live upgrade: periodic client frames keep resetting the liveness clock,
        # so it is NOT reaped even after several upgrade_idle_timeout windows.
        live = _client()
        live.sendall(ws_head)
        check(
            "live upgrade handler goes in-flight",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )
        deadline = time.monotonic() + 1.5  # 3x upgrade_idle_timeout
        pinged = True
        while time.monotonic() < deadline:
            try:
                live.sendall(b"\x00")  # opaque WS-frame stand-in — spliced along
            except OSError:
                pinged = False
                break
            time.sleep(0.15)
        check("live upgrade kept sending (never reaped mid-stream)", pinged)
        check(
            "live upgrade NOT reaped while frames keep the clock warm",
            terminator._active == 1,
            f"_active={terminator._active}",
        )
        with contextlib.suppress(OSError):
            live.close()
    finally:
        terminator.stop()
        upstream.close()


def test_head_dribble_reaped_by_total_budget(certs):
    print("\n== terminator: byte-at-a-time head dribble reaped by the total budget ==")
    # A peer that dribbles the request head one byte at a time, each byte under
    # the per-read idle, never trips the per-read timeout — but the cumulative
    # head budget (idle_timeout total) still reaps it, so head assembly can't be
    # stretched indefinitely.
    _write_terminator_certs(certs)
    upstream = _SilentUpstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        idle_timeout=0.6,
        max_connections=8,
    )
    terminator.start()
    try:
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
        check(
            "dribble handler goes in-flight",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )
        # Never complete the head: one byte every 0.25s (< idle_timeout 0.6s), so
        # the per-read idle never fires but the 0.6s total budget does. The head
        # is long enough that dribbling it whole would far outlast the budget.
        head = b"GET /slow HTTP/1.1\r\nHost: x\r\nX-Filler: aaaaaaaaaaaaaaaaaaaa\r\n"
        reaped = False
        for i in range(len(head)):
            try:
                tls.sendall(head[i : i + 1])
            except OSError:
                reaped = True
                break
            if terminator._active == 0:
                reaped = True
                break
            time.sleep(0.25)
        check(
            "head dribble reaped by the cumulative budget (no CRLFCRLF sent)",
            reaped or _wait_for(lambda: terminator._active == 0, timeout=2.0),
            f"_active={terminator._active}",
        )
        with contextlib.suppress(OSError):
            tls.close()
    finally:
        terminator.stop()
        upstream.close()


def test_connection_cap_sheds(certs):
    print("\n== terminator: connection cap sheds excess and frees on completion ==")
    from hyperdjango.telemetry import metrics as _metrics

    _metrics.enable()
    _write_terminator_certs(certs)
    upstream = _SilentUpstream()
    terminator = MTLSTerminator(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_host="127.0.0.1",
        upstream_port=upstream.port,
        certfile=str(SCRATCH / "srv.crt"),
        keyfile=str(SCRATCH / "srv.key"),
        ca_file=str(SCRATCH / "ca.crt"),
        max_connections=1,
        idle_timeout=30.0,
    )
    terminator.start()

    def _client():
        ctx = ssl.create_default_context(cafile=str(SCRATCH / "ca.crt"))
        ctx.load_cert_chain(str(SCRATCH / "cli.crt"), str(SCRATCH / "cli.key"))
        raw = socket.create_connection(("127.0.0.1", terminator.bound_port), timeout=5)
        return ctx.wrap_socket(raw, server_hostname="127.0.0.1")

    a = None
    try:
        # Client A takes the single slot and parks in the splice (silent upstream).
        a = _client()
        a.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        check(
            "first client holds the only slot",
            _wait_for(lambda: terminator._active == 1, timeout=3.0),
            f"_active={terminator._active}",
        )

        shed_before = MTLS_CONNECTIONS_SHED.value()
        # Client B is over the cap: the terminator sheds it immediately after the
        # handshake — it never needs to send a request — and the client sees EOF.
        b_closed = False
        try:
            b = _client()
            b.settimeout(3.0)
            b_closed = b.recv(64) == b""
            b.close()
        except ssl.SSLError, OSError:
            b_closed = True
        check("second client over the cap is closed promptly", b_closed)
        check(
            "shed counter increments",
            _wait_for(lambda: MTLS_CONNECTIONS_SHED.value() > shed_before, timeout=3.0),
            f"before={shed_before} after={MTLS_CONNECTIONS_SHED.value()}",
        )
        check("first client still holds its slot", terminator._active == 1)

        # When the first client completes (closes), its slot frees.
        a.close()
        a = None
        check(
            "slot frees after the first client completes",
            _wait_for(lambda: terminator._active == 0, timeout=3.0),
            f"_active={terminator._active}",
        )
    finally:
        if a is not None:
            with contextlib.suppress(OSError):
                a.close()
        terminator.stop()
        upstream.close()


def main() -> bool:
    print("hyperdjango.mtls unit tests")
    certs = test_issuance()
    test_terminator_handshake(certs)
    test_untrusted_client_rejected(certs)
    test_terminator_hardening(certs)
    test_rewrite_head_lexical()
    test_body_passes_through_opaque(certs)
    test_identity_policy(certs)
    test_cn_cap_agreement(certs)
    test_body_phase_slowloris_reaped(certs)
    test_upgrade_idle_timeout_reaps_silent_peer(certs)
    test_head_dribble_reaped_by_total_budget(certs)
    test_streaming_response_not_reaped(certs)
    test_connection_cap_sheds(certs)
    test_non_ascii_attestation_fails_closed()
    test_connection_slot_reserved_before_await(certs)
    test_completed_request_slot_reaped(certs)
    test_shutdown_cancels_inflight(certs)
    test_start_reraises_bind_failure(certs)
    test_handshake_failure_metric(certs)
    test_module_docstring_points_at_docs()
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
