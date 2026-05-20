# Mutual TLS (client-certificate authentication)

`hyperdjango.mtls` provides network-exposed mutual TLS: clients authenticate
with certificates issued by your own certificate authority, and the server
verifies them as the authentication leg of AAA. It works self-contained (an
in-process TLS terminator) or behind an external TLS-terminating proxy.

The native Zig HTTP server does not speak TLS, so mTLS is layered in front of
it. Two topologies, one identity API.

## Certificate authority

```python
from hyperdjango.mtls import create_ca, issue_cert, write_pem

ca_key, ca_cert = create_ca("my-ca")
write_pem("ca.key", ca_key, private=True)  # 0600
write_pem("ca.crt", ca_cert)

# Server certificate (serverAuth EKU + SANs)
srv_key, srv_cert = issue_cert(
    ca_key,
    ca_cert,
    "secrets.internal",
    server=True,
    san_dns=["secrets.internal", "10.0.0.5"],
)

# Client certificate — CN is the application identity name (clientAuth EKU)
cli_key, cli_cert = issue_cert(ca_key, ca_cert, "service:prod-api")
```

Certificates carry Subject/Authority Key Identifiers (required by modern
OpenSSL) and the appropriate Extended Key Usage; IP entries in `san_dns`
become IP-address SANs automatically.

## In-process terminator (self-contained)

`MTLSTerminator` is a certificate-authentication gateway, not a second HTTP
server. It runs a TLS listener on its own thread and event loop and does
exactly four things per connection, making **zero** HTTP framing decisions:

1. **Terminate TLS.** The handshake enforces chain trust against the CA
   (`CERT_REQUIRED`, TLS 1.2 floor).
2. **Derive and vet the identity** with `cryptography`: the certificate common
   name and its SHA-256 fingerprint. Two policy checks that stdlib `ssl` does
   not express cleanly are applied and fail closed — the certificate validity
   window (not-before / not-after, in UTC) and, when the certificate declares
   an Extended Key Usage, that it includes clientAuth.
3. **Rewrite only the request head.** The terminator reads up to the end of the
   head (bounded by a head-size cap and the idle timeout), strips any inbound
   reserved identity header by name (`x-hyper-mtls-*`, `x-real-ip`,
   `x-forwarded-for`) so a client cannot smuggle an attested identity, **also
   strips the client's inbound `Connection` header**, refuses a head that uses
   obs-fold, appends the attested identity headers plus **exactly one**
   `Connection` header, and writes that head to a fresh loopback connection to
   the plaintext native server. The single `Connection` header is chosen by a
   lexical header-name check, not by interpreting framing: if the surviving head
   carries an `Upgrade` line (a WebSocket upgrade) it injects `Connection:
Upgrade` so the native server performs the upgrade; otherwise `Connection:
close`, giving one request per upstream connection. Emitting exactly one
   `Connection` header keeps correctness from depending on how the native server
   resolves conflicting `Connection` headers. It never reads or interprets the
   request line, `Content-Length`, or `Transfer-Encoding`.
4. **Splice the rest opaquely.** After the head, bytes flow in both directions
   untouched until close, and an EOF from either side tears down the other so a
   completed (`Connection: close`) request never leaves a half-open socket
   pinning a connection slot. Both directions' bytes stamp a shared liveness
   clock, and the connection is reaped only on whole-connection idleness — so a
   peer that completes a valid head and then stalls mid-body is reaped, while a
   quiet client receiving a long streaming response is not. A non-upgrade
   connection uses `idle_timeout`; a WebSocket upgrade uses the larger
   `upgrade_idle_timeout`, so a genuine feed survives long idle gaps between
   events yet a client that sends an `Upgrade` head and then goes silent
   _without ever establishing a real WebSocket_ is still reaped rather than
   pinning a slot untimed. Head assembly is additionally bounded by a cumulative
   wall-clock budget (`idle_timeout` total), so a peer dribbling a byte at a time
   just under the per-read idle cannot stretch the head phase indefinitely.

The native server is the **single HTTP framing authority** — it owns keep-alive,
body length, and every request boundary. The two `Connection` cases keep the
splice opaque either way: an ordinary request gets `Connection: close`, so one
client connection maps to one upstream connection with no second boundary to
find; a WebSocket head instead gets `Connection: Upgrade`, and everything after
the head is the same opaque bidirectional splice, indefinitely. One TLS
connection per request suits this low-volume authentication path — stdlib
`ssl` session resumption (tickets) makes client reconnects cheap abbreviated
handshakes.

`start()` blocks until the listener is bound and raises if it cannot bind (for
example, the port is in use), so a misconfigured front door fails fast at
startup instead of leaving a dead listener behind.

```python
from hyperdjango.mtls import MTLSTerminator

terminator = MTLSTerminator(
    listen_host="0.0.0.0",
    listen_port=8443,
    upstream_host="127.0.0.1",
    upstream_port=8960,  # bind app to loopback
    certfile="server.crt",
    keyfile="server.key",
    ca_file="ca.crt",
    max_connections=512,  # shed connections past this cap (backpressure)
    idle_timeout=60.0,  # reap a stalled non-upgrade peer (slow-loris)
    upgrade_idle_timeout=300.0,  # larger ceiling for an upgraded WebSocket
)
terminator.start()  # in @app.on_startup
```

### Wiring it into an app

`MTLSTerminator.install(app, ...)` is **the** way to wire the terminator into an
app. One call registers everything the lifecycle needs and returns a live
handle whose `.terminator` attribute is the running terminator once startup has
run (`None` before startup, when mTLS is disabled, or after shutdown):

```python
from hyperdjango.mtls import MTLSTerminator

mtls = MTLSTerminator.install(
    app,
    listen_port=config.mtls_listen_port,  # 0 (or no cert) disables mTLS
    cert_file=config.mtls_cert_file,
    key_file=config.mtls_key_file,
    ca_file=config.mtls_ca_file,
    listen_host="127.0.0.1" if debug else "0.0.0.0",
    max_connections=config.mtls_max_connections,
    idle_timeout=config.mtls_idle_timeout,
    upgrade_idle_timeout=config.mtls_upgrade_idle_timeout,
    # trust_upstream_ip=True (default) — see "Client IP / rate limiting" above
)
# mtls.terminator is the running MTLSTerminator after startup, else None
```

`install` registers three things on the app and references no application code:

- an **`on_startup`** hook that builds the terminator, passing the app's
  _actual_ bound plaintext port (`app.bound_port`, published before startup
  hooks run) as the upstream — so moving the app port can never desync the
  terminator. When mTLS is enabled the hook **fails loudly** if `app.bound_port`
  is `0`: an ephemeral bind (`PORT=0`) has no port the native server can report,
  so there is no known upstream to forward to — bind the app to a fixed
  plaintext port. When `trust_upstream_ip` is left `True`, the hook also makes
  the loopback upstream a trusted proxy so `request.client_ip` reflects the
  injected `X-Real-IP` (see **Client IP / rate limiting** above);
- a **readiness check** (named `mtls_terminator` by default) that reports
  healthy when mTLS is disabled _and_ while the terminator is alive, and
  unhealthy only when a configured front door has died — so `/ready` reflects a
  dead front door instead of reporting healthy while no mTLS traffic can land;
- an **`on_shutdown`** hook that stops the terminator (deregistering its
  attestation) on the way down.

mTLS is enabled iff both `listen_port` and `cert_file` are set; otherwise the
same hooks are wired but no-op and `mtls.terminator` stays `None`.

Under the hood `install` uses `MTLSTerminator.from_config`, which collapses the
enable-check-construct-start sequence and returns a started terminator or
`None`. Reach for `from_config` directly only when managing the lifecycle by
hand; `upstream_port` is **required** so the terminator can never silently
forward to the wrong port when the app moves:

```python
terminator = MTLSTerminator.from_config(
    upstream_port=app.bound_port,  # the app's real port — REQUIRED
    listen_port=config.mtls_listen_port,  # 0 disables
    cert_file=config.mtls_cert_file,
    key_file=config.mtls_key_file,
    ca_file=config.mtls_ca_file,
)  # started terminator, or None when disabled
```

**Hardening.** `max_connections` caps concurrent client sockets — excess
connections are closed immediately rather than queued.

Two idle ceilings bound a stalled peer, both measured on **whole-connection**
idleness (either direction's bytes reset the clock, so a live streaming response
or a WebSocket with periodic frames is never reaped mid-flight):

- `idle_timeout` (default `60s`) reaps a **non-upgrade** peer that opens a
  connection and then stalls — during the request head and mid-body — so a
  slow-loris cannot pin a slot by completing a valid head and then going silent.
  Head assembly also has a cumulative wall-clock budget of `idle_timeout` total,
  so a byte-at-a-time dribble that never trips the per-read idle is still reaped
  on total elapsed time.
- `upgrade_idle_timeout` (default `300s`) is the larger ceiling used **after a
  WebSocket upgrade**. `Upgrade` presence is a lexical header check on the head,
  set _before_ any real `101` is confirmed, so the splice cannot be left untimed:
  a client that sends `Upgrade: websocket` and then goes silent without ever
  establishing a feed is reaped at `upgrade_idle_timeout`. A genuine WebSocket
  survives because its frames (or the app's keepalive pings) keep resetting the
  clock; set `upgrade_idle_timeout` comfortably above your keepalive interval.

`upgrade_idle_timeout` is meant to be the **larger** of the two. Setting it below
`idle_timeout` reaps an upgraded WebSocket sooner than a plain connection — the
opposite of intent (it still fails safe; nothing is under-reaped). The terminator
**warns** and **preserves** the value you configured rather than silently
clamping it, so your explicit numbers are always honored; set
`upgrade_idle_timeout >= idle_timeout` to clear the warning.

The terminator warns if `upstream_host` is not loopback, since a client that can
reach the plaintext upstream directly bypasses TLS and can forge the identity
headers — bind that port to loopback or firewall it to the terminator host.

**Client IP / rate limiting.** The terminator forwards over loopback and injects
`X-Real-IP` with the real client address, but the app's socket peer is then
always the loopback upstream. `request.client_ip` honors `X-Real-IP` only when
that peer is a trusted proxy, so without extra wiring `client_ip` would collapse
to `127.0.0.1` for every caller — one rate-limit bucket for all of them.
`MTLSTerminator.install(app, ...)` fixes this automatically: `trust_upstream_ip`
(default `True`) adds the loopback upstream address to the `TRUSTED_PROXIES`
authority, so `request.client_ip` reflects the injected `X-Real-IP` and per-IP
rate limiting keys on the real client. It is added at the DEFAULTS layer, so it
composes with the built-in default but does **not** override an explicit
`TRUSTED_PROXIES` (env / Django) — if you set that list yourself, include the
loopback upstream (`127.0.0.1`). Pass `trust_upstream_ip=False` to leave global
proxy trust untouched (e.g. you resolve client IP some other way). See
[`docs/settings.md`](settings.md) `TRUSTED_PROXY_COUNT` / `TRUSTED_PROXIES`.

**Metrics** (exposed on `/metrics` when telemetry is enabled):
`hyperdjango_mtls_active_connections` (gauge — client connections currently
open), `hyperdjango_mtls_handshake_failures_total` (completed handshakes that
carried no usable certificate identity — a missing or malformed common name;
certificates rejected during the handshake itself are refused by the TLS layer
before the terminator sees them), `hyperdjango_mtls_identity_policy_rejected_total`
(handshake-verified certificates refused by the post-handshake identity policy —
outside the validity window, or an EKU that omits clientAuth),
`hyperdjango_mtls_connections_shed_total` (connections closed immediately at the
connection cap), and `hyperdjango_mtls_upstream_failures_total` (failures
connecting to the plaintext upstream).

Injected headers (client-supplied copies are always stripped first):

| Header                     | Meaning                                                         |
| -------------------------- | --------------------------------------------------------------- |
| `X-Hyper-MTLS-Attest`      | per-process random secret proving the terminator injected these |
| `X-Hyper-MTLS-CN`          | verified certificate common name (the identity)                 |
| `X-Hyper-MTLS-Fingerprint` | SHA-256 of the client certificate                               |
| `X-Real-IP`                | original peer address                                           |

## Reading the verified identity

`resolve_client_cert` returns the raw verified certificate identity:

```python
from hyperdjango.mtls import resolve_client_cert

cert = resolve_client_cert(request)  # attestation resolved automatically — see below
if cert is not None:
    identity = lookup_identity(cert.common_name)  # your identity table
```

Most services want the higher-level `hyperdjango.identity.resolve_identity`,
which authenticates a caller by **either** a signed bearer token
(`SignedAPIKeyMixin`) **or** a client certificate, against your identity
model, and reports how they authenticated (for the audit trail) — with
optional per-identity certificate fingerprint pinning:

```python
from hyperdjango.identity import resolve_identity

resolved = await resolve_identity(
    request,
    ServiceIdentity,
    fingerprint_field="cert_fingerprint",  # per-identity pin (optional)
)
# resolved.identity, resolved.method ("token"|"cert"), resolved.fingerprint
audit(
    actor=resolved.identity.name, auth=resolved.method, fingerprint=resolved.fingerprint
)
```

Pinning: give an identity a non-empty `cert_fingerprint` (a comma-separated
SHA-256 allow-list) and only those certificates authenticate as it — revoke a
single leaked cert by removing its fingerprint, without disabling the
identity. An empty value accepts any CA-issued cert with the right CN. Both
services record `auth_method` and `fingerprint` on every audit row.

**Fingerprint format.** Entries are matched case-insensitively and separators
are ignored, so either form works: the terminator's canonical lowercase hex
with no separators (`a1b2c3…`, the shape stored in `X-Hyper-MTLS-Fingerprint`)
or the OpenSSL `openssl x509 -fingerprint -sha256` style
(`A1:B2:C3:…`, uppercase with colons). Both sides are normalized — colons and
whitespace stripped, lowercased — before comparison, so pasting a fingerprint
in either format pins correctly rather than failing closed on a cosmetic
difference.

`resolve_client_cert` honors the identity headers **only** when
`X-Hyper-MTLS-Attest` matches (constant-time) one of two sources, in
precedence order:

1. the **process-level registry** of running in-process terminators — every
   `MTLSTerminator.start()` registers its own secret and `stop()` deregisters
   it, so an in-process terminator+app deployment needs nothing hand-carried;
2. the configured `MTLS_PROXY_SECRET` (external-proxy topology).

A missing attestation, or one matching no source, returns `None` — fail
closed. Because the registry resolves an in-process terminator automatically,
`resolve_client_cert(request)` and `resolve_identity(request, Model)` "just
work" with no secret threaded through your call sites. This is the same shape
as the client-IP and CORS authorities: one decision point both serving paths
share.

## Authorizing by scope

Authentication answers _who_; a `SignedAPIKeyMixin` identity carries a
comma-separated `scopes` string (default `"*"`) that answers _what they may do_.
`hyperdjango.identity` exposes the three shared scope primitives so every
service checks capabilities the same way instead of re-splitting the CSV and
re-writing the wildcard rule:

```python
from hyperdjango.identity import parse_scopes, has_scope, require_scope

scopes = parse_scopes(resolved.identity.scopes)  # frozenset[str]; "*" stays a member

if has_scope(scopes, "secrets:write"):  # True when granted or "*" present
    ...

require_scope(scopes, "secrets:write")  # raises HTTPException(403) unless granted
```

- `parse_scopes(raw: str) -> frozenset[str]` — split on commas, strip
  whitespace, drop empties (`" read , , write "` → `{"read", "write"}`); an
  empty/blank input yields the empty set.
- `has_scope(scopes, required) -> bool` — `required` is granted when it is in
  `scopes` **or** the wildcard `"*"` is. `scopes` may be an already-parsed
  `frozenset` or a raw string (parsed on the fly), so either form works.
- `require_scope(scopes, required) -> None` — the 403-raising gate: returns
  `None` when granted, else raises `HTTPException(403, "Scope '<required>'
required")`. Fail closed.

A caller keeps only its own identity-to-scopes extraction and funnels every
capability check through `require_scope`.

## External-proxy topology

Set `MTLS_PROXY_SECRET` (a random value) and configure a TLS-terminating
proxy to verify client certificates and forward the same headers with that
secret as the attestation. The app's plaintext port must be reachable only
from the proxy. A complete nginx example ships at
`services/hypersecret/deploy/nginx-mtls.conf`.

`MTLS_PROXY_SECRET` must be at least **32 characters** (the repo's signing-key
floor). A shorter value is a broken security boundary — it is refused (never
honored to attest an identity) and the misconfiguration is logged. Generate one
with `python -c "import secrets; print(secrets.token_hex(32))"`.

## Revocation

Bind certificate CNs to rows in your own identity table (as the HyperSecret
and HyperManager services do) and gate on an `is_active` flag. Revoking the
identity disables both its bearer token and its certificate immediately — no
CRL or OCSP machinery required for a single-boundary deployment.

See `services/hypersecret` and `services/hypermanager` for end-to-end use,
and the provisioning CLI's `ca` / `cert` commands for issuance.
