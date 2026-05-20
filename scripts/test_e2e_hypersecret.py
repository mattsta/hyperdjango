"""
E2E: HyperSecret secret manager (services/hypersecret).

# hyper-test: e2e

Boots the real native server and exercises the full secret lifecycle:

  - seed bootstrap (namespaces, identities, grants, sealed secrets)
  - fetch + client-side decrypt (SDK), batch fetch, conditional 304
  - fail-closed authz: cross-namespace denial, forged/revoked tokens
  - crypto isolation: right grant + wrong KEK still cannot decrypt
  - provisioning over HTTP: put/rotate/version-pin/409 conflict
  - blob-substitution tamper detection via AAD binding
  - soft delete → 404, revive, admin-only purge
  - audit rows for reads, writes, and denials
  - identity lifecycle over the admin API (create/grant/revoke)
  - secrets_run injection wrapper (exec mode + env-file mode)
  - native-vs-ASGI parity spot checks (dual-dispatch drift guard)
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from e2e_helper import (  # noqa: E402
    TEST_PORTS,
    AppRunner,
    http_delete,
    http_get,
    http_post,
)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.hypersecret.client import (  # noqa: E402
    AuthError,
    DecryptError,
    SecretNotFound,
    SecretsClient,
    SecretsError,
    VersionConflict,
)
from services.hypersecret.envelope import load_kek_file  # noqa: E402

PORT = TEST_PORTS["hypersecret"]
BASE = f"http://127.0.0.1:{PORT}"
DEMO_DIR = PROJECT_ROOT / ".test_scratch" / "hypersecret_demo"

# The token-signing key (framework SESSION_SIGNING_KEY) must be STABLE across
# the seed and server subprocesses, else seed-minted tokens fail to verify.
# hyper-test injects it; pin it here too for standalone runs.
os.environ.setdefault(
    "HYPER_SESSION_SIGNING_KEY", "test-session-signing-key-for-tests-only"
)
# The app resolves SECRET_KEY / ADMIN_SECRET with require_setting(min_length=32),
# so it refuses to start on a missing or too-short signing secret. Provide stable
# >=32-char values for the seed and server subprocesses. The shared runner injects
# a shorter ADMIN_SECRET for other apps; override it here to satisfy the length
# gate without weakening the app's requirement.
os.environ.setdefault(
    "HYPER_SECRET_KEY", "test-secret-key-for-hypersecret-tests-only-0123456789"
)
if len(os.environ.get("HYPER_ADMIN_SECRET", "")) < 32:
    os.environ["HYPER_ADMIN_SECRET"] = (
        "test-admin-secret-for-hypersecret-tests-only-0123456789"
    )

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        ERRORS.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")
    return cond


def expect_raises(name: str, exc_type, fn) -> None:
    try:
        fn()
        check(name, False, f"expected {exc_type.__name__}, no error raised")
    except exc_type:
        check(name, True)
    except Exception as exc:  # noqa: BLE001 - report wrong-type failures
        check(name, False, f"expected {exc_type.__name__}, got {exc!r}")


def _valid_dek_b64() -> str:
    """A base64 blob of the exact wrapped-DEK wire size the server enforces
    (nonce + DEK + GCM tag) — a dummy that passes the length check without being
    a real envelope (used where the request is expected to fail past that gate)."""
    from services.hypersecret.envelope import ENCRYPTED_DEK_BYTES

    return base64.b64encode(b"\x00" * ENCRYPTED_DEK_BYTES).decode()


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
    # The ceiling only bounds the CPU-starved worst case under the full
    # parallel suite; polls return the moment the predicate holds, so normal
    # runs never feel it. A genuinely unmet condition still fails once the
    # bound elapses.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _backdate_deleted_at(key: str, when: datetime) -> None:
    """Age a soft-deleted secret's ``deleted_at`` in the shared DB so the
    server's retention sweep (fast cadence under test) hard-purges it — the
    honest way to exercise the time-based purge without real elapsed days."""

    async def _run():
        from hyperdjango.database import Database

        db = Database(
            os.environ.get("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
        )
        await db.connect()
        try:
            await db.execute(
                "UPDATE hs_secrets SET deleted_at = $1 "
                "WHERE key = $2 AND deleted_at IS NOT NULL",
                when,
                key,
            )
        finally:
            await db.disconnect()

    asyncio.run(_run())


def _insert_audit_row(identity: str, when: datetime) -> None:
    """Insert an AccessLog row directly with a chosen created_at, to exercise
    the audit-retention sweep without waiting real days."""

    async def _run():
        from hyperdjango.database import Database

        db = Database(
            os.environ.get("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
        )
        await db.connect()
        try:
            await db.execute(
                "INSERT INTO hs_access_log "
                "(identity, namespace, key, version, action, outcome, "
                " created_at, client_ip, auth_method, fingerprint) "
                "VALUES ($1, '', '', 0, 'read', 'ok', $2, '', '', '')",
                identity,
                when,
            )
        finally:
            await db.disconnect()

    asyncio.run(_run())


def setup_db() -> None:
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    env = dict(os.environ)
    env["HYPERSECRET_DEMO_DIR"] = str(DEMO_DIR)
    result = subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.hypersecret.app:app",
            "--drop",
            "--seed",
            "services.hypersecret.seed:run",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError("hyper setup failed")


def load_demo_state() -> tuple[dict, dict]:
    tokens = json.loads((DEMO_DIR / "tokens.json").read_text())
    keks = {}
    for ns in ("prod/api", "prod/frontend", "staging/api"):
        kek_id, kek = load_kek_file(str(DEMO_DIR / (ns.replace("/", "-") + ".kek")))
        keks[ns] = (kek_id, kek)
    return tokens, keks


def client_for(tokens, keks, identity: str, namespace: str, **kw) -> SecretsClient:
    kek_id, kek = keks.get(namespace, ("", None))
    return SecretsClient(
        BASE,
        token=tokens[identity],
        namespace=namespace,
        kek=kek,
        kek_id=kek_id,
        **kw,
    )


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------


def test_read_path(tokens, keks):
    print("\n== read path: fetch, decrypt, batch, 304 ==")
    api = client_for(tokens, keks, "service:prod-api", "prod/api")

    check(
        "stripe_key decrypts",
        api.get_secret("stripe_key") == "sk_live_demo_4242424242424242",
    )
    check(
        "secret() returns plaintext str",
        api.secret("db_password") == "prod-api-db-pw-3f9c2e",
    )
    with api.secret_bytes("jwt_secret") as buf:
        check(
            "secret_bytes yields wipeable bytearray",
            bytes(buf) == b"jwt-signing-demo-77aa1b",
        )
    check("secret_bytes zeroized after block", bytes(buf) == b"\x00" * len(buf))

    batch = api.get_secrets(["stripe_key", "db_password", "jwt_secret"])
    check(
        "batch fetch decrypts all three",
        len(batch) == 3 and batch["stripe_key"].startswith("sk_live"),
    )

    keys = {k["key"] for k in api.list_keys()}
    check("list_keys", keys == {"stripe_key", "db_password", "jwt_secret"})

    namespaces = api.list_namespaces()
    check(
        "namespaces reflect grants",
        [n["name"] for n in namespaces] == ["prod/api"]
        and namespaces[0]["write"] is False,
    )

    status, _ = api._fetch("stripe_key", query={"known_version": "1"})
    check("conditional fetch returns 304", status == 304)

    expect_raises(
        "missing key → SecretNotFound", SecretNotFound, lambda: api.get_secret("nope")
    )


def test_fail_closed(tokens, keks):
    print("\n== authz: fail closed ==")
    frontend = client_for(tokens, keks, "service:prod-frontend", "prod/api")
    expect_raises(
        "frontend token denied on prod/api",
        AuthError,
        lambda: frontend.get_secret("stripe_key"),
    )
    staging = client_for(tokens, keks, "service:staging-api", "prod/api")
    expect_raises(
        "staging token denied on prod/api",
        AuthError,
        lambda: staging.get_secret("stripe_key"),
    )

    forged = SecretsClient(BASE, token="hsk_forged_garbage", namespace="prod/api")
    expect_raises("forged token → 401", AuthError, lambda: forged.list_keys())

    noscope = client_for(tokens, keks, "service:prod-api", "prod/api")
    expect_raises(
        "read-only identity denied write",
        AuthError,
        lambda: noscope.put_secret("newkey", b"x"),
    )
    expect_raises(
        "read-only identity denied audit query",
        AuthError,
        lambda: noscope._request("GET", "/v1/audit"),
    )
    expect_raises(
        "read-only identity denied admin API",
        AuthError,
        lambda: noscope._request(
            "POST", "/v1/admin/identities", body={"name": "x:y", "scopes": "read"}
        ),
    )


def test_crypto_isolation(tokens, keks):
    print("\n== crypto: wrong KEK cannot decrypt despite valid grant ==")
    # operator:admin holds a read grant on prod/api but we hand it the
    # frontend KEK — authorization passes, decryption must fail.
    wrong_kek = SecretsClient(
        BASE,
        token=tokens["operator:admin"],
        namespace="prod/api",
        kek=keks["prod/frontend"][1],
        kek_id=keks["prod/frontend"][0],
    )
    expect_raises(
        "grant without KEK still cannot decrypt",
        DecryptError,
        lambda: wrong_kek.get_secret("stripe_key"),
    )


def test_provisioning(tokens, keks):
    print("\n== provisioning: put / rotate / pin / conflict ==")
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)

    v1 = ops.put_secret("webhook_secret", b"whsec_first", metadata={"owner": "qa"})
    check("initial put → version 1", v1 == 1)
    v2 = ops.put_secret("webhook_secret", b"whsec_rotated")
    check("rotation put → version 2", v2 == 2)
    check(
        "fetch returns rotated value",
        ops.get_secret("webhook_secret") == "whsec_rotated",
    )
    check(
        "version pin returns original",
        ops.get_secret("webhook_secret", version=1) == "whsec_first",
    )

    history = ops.versions("webhook_secret")
    check(
        "history shows both versions with provenance",
        history["current_version"] == 2
        and len(history["versions"]) == 2
        and all(v["created_by"] == "operator:admin" for v in history["versions"]),
    )

    # Optimistic concurrency: hand-build a POST with a stale version number.
    from services.hypersecret.envelope import seal

    kek_id, kek = keks["prod/api"]
    stale = seal(
        b"nope",
        kek=kek,
        kek_id=kek_id,
        namespace="prod/api",
        key="webhook_secret",
        version=2,
    )
    body = stale.to_dict()
    body["version"] = 2  # server expects 3
    expect_raises(
        "stale version POST → 409",
        VersionConflict,
        lambda: ops._request("POST", "/v1/secrets/prod/api/webhook_secret", body=body),
    )


def test_concurrent_write_conflict(tokens, keks):
    """Regression: concurrent writes to the same new key must never 500 on the
    unique constraint. Every attempt resolves to a clean success or an audited
    409, and the resulting version history is contiguous with no duplicates
    (the pre-fix code let the losing racer surface a raw IntegrityError 500)."""
    print("\n== concurrent write: no 500s, contiguous versions ==")
    import concurrent.futures

    def attempt(value: bytes):
        # Each thread its own client (own cache) racing the same fresh key.
        c = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
        try:
            c.put_secret("race_key", value)
            return "ok"
        except VersionConflict:
            return "conflict"
        except Exception as exc:  # noqa: BLE001
            return f"error:{exc!r}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, [f"v{i}".encode() for i in range(6)]))
    check(
        "no attempt 500s (all ok or clean 409)",
        all(r in ("ok", "conflict") for r in results),
        f"results={results}",
    )
    check("at least one writer succeeds", "ok" in results)

    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
    versions = sorted(v["version"] for v in ops.versions("race_key")["versions"])
    check(
        "version history is contiguous 1..N with no duplicates",
        versions == list(range(1, len(versions) + 1)),
        f"versions={versions}",
    )
    check(
        "stored versions match successful writes",
        len(versions) == results.count("ok"),
        f"versions={versions} oks={results.count('ok')}",
    )


def test_metadata_segregation(tokens, keks):
    """Regression: a write-scoped rotation must not clear server-managed flags
    (exposed/exposed_at), and a client cannot inject them."""
    print("\n== metadata: rotation preserves server flags, ignores client spoof ==")
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)

    ops.put_secret("seg_key", b"v1")
    http_post(
        f"{BASE}/v1/secrets/prod/api/seg_key/expose",
        body={},
        headers={"Authorization": f"Bearer {tokens['operator:admin']}"},
    )
    _, after_expose = ops._fetch("seg_key")
    check(
        "exposed flag set by /expose", after_expose["metadata"].get("exposed") is True
    )

    # A client tries to clear `exposed` and forge `expiry_notified` via rotation.
    ops.put_secret(
        "seg_key",
        b"v2",
        metadata={"exposed": False, "expiry_notified": True, "owner": "x"},
    )
    _, after_rotate = ops._fetch("seg_key")
    meta = after_rotate["metadata"]
    check("rotation preserves server-set exposed flag", meta.get("exposed") is True)
    check("client cannot forge expiry_notified", "expiry_notified" not in meta)
    check("client's own metadata keys still applied", meta.get("owner") == "x")


def test_tamper_detection(tokens, keks):
    print("\n== tamper: blob substitution fails AAD authentication ==")
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)

    # Steal stripe_key's (perfectly valid) envelope and store it under a
    # different key. The server cannot tell — but the client's AAD check can.
    _, stolen = ops._fetch("stripe_key")
    body = {
        "format": stolen["format"],
        "alg": stolen["alg"],
        "kek_id": stolen["kek_id"],
        "ciphertext": stolen["ciphertext"],
        "encrypted_dek": stolen["encrypted_dek"],
        "version": 1,
    }
    ops._request("POST", "/v1/secrets/prod/api/substituted_blob", body=body)
    expect_raises(
        "substituted blob → DecryptError",
        DecryptError,
        lambda: ops.get_secret("substituted_blob"),
    )
    ops.delete_secret("substituted_blob", purge=True)


def test_delete_lifecycle(tokens, keks):
    print("\n== delete: soft, revive, admin-only purge ==")
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
    deployer = client_for(tokens, keks, "ci:deployer", "prod/api", cache_ttl=0)

    v = deployer.put_secret("ephemeral", b"short-lived")
    check("deployer (write grant) can provision", v == 1)

    deployer.delete_secret("ephemeral")
    expect_raises(
        "soft-deleted secret unreadable",
        SecretNotFound,
        lambda: deployer.get_secret("ephemeral"),
    )
    history = deployer.versions("ephemeral")
    check("history survives soft delete", history["deleted_at"] is not None)

    v = deployer.put_secret("ephemeral", b"revived")
    check("re-put revives at next version", v == 2)
    check("revived value fetches", deployer.get_secret("ephemeral") == "revived")

    expect_raises(
        "purge without admin scope denied",
        AuthError,
        lambda: deployer.delete_secret("ephemeral", purge=True),
    )
    ops.delete_secret("ephemeral", purge=True)
    expect_raises(
        "purged secret gone",
        SecretNotFound,
        lambda: ops.versions("ephemeral"),
    )


def test_retention_time_sweep(tokens, keks):
    """The time-based retention sweep purges soft-deleted secrets once they age
    past the window — the machinery behind soft delete, distinct from admin
    purge. Soft-delete two secrets; backdate only one's ``deleted_at`` past the
    window; the server sweep (fast cadence under test) hard-purges the aged one
    while the fresh soft-delete survives with its history intact."""
    print("\n== retention: time-based sweep purges aged soft-deletes ==")
    deployer = client_for(tokens, keks, "ci:deployer", "prod/api", cache_ttl=0)
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)

    deployer.put_secret("retain_fresh", b"keep-me")
    deployer.delete_secret("retain_fresh")  # soft delete, deleted_at ~ now
    deployer.put_secret("retain_aged", b"purge-me")
    deployer.delete_secret("retain_aged")

    # Age only the victim's soft-delete far past the window; the sweep purges it.
    _backdate_deleted_at("retain_aged", datetime.now(UTC) - timedelta(days=3650))

    def purged() -> bool:
        try:
            ops.versions("retain_aged")
            return False
        except SecretNotFound:
            return True

    check(
        "aged soft-delete is hard-purged by the time-based sweep",
        wait_for(purged, timeout=15),
    )
    # The un-aged soft-delete survives: still present, still soft-deleted.
    history = ops.versions("retain_fresh")
    check(
        "un-aged soft-delete survives the sweep (history intact)",
        history["deleted_at"] is not None,
    )


def test_identity_lifecycle(tokens, keks):
    print("\n== admin API: identity create → grant → use → revoke ==")
    ops = client_for(tokens, keks, "operator:admin", "")

    _, created = ops._request(
        "POST", "/v1/admin/identities", body={"name": "temp:reader", "scopes": "read"}
    )
    check("identity minted with hsk_ token", created["token"].startswith("hsk_"))
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={"identity": "temp:reader", "namespace": "staging/api", "read": True},
    )

    kek_id, kek = keks["staging/api"]
    temp = SecretsClient(
        BASE, token=created["token"], namespace="staging/api", kek=kek, kek_id=kek_id
    )
    check(
        "new identity reads its namespace",
        temp.get_secret("db_password") == "staging-db-pw-not-prod",
    )

    _, review = ops._request(
        "GET", "/v1/admin/grants", query={"namespace": "staging/api"}
    )
    reviewed = {g["identity"] for g in review["grants"]}
    check("grant review lists new identity", "temp:reader" in reviewed)

    ops._request("DELETE", "/v1/admin/identities/temp:reader")
    temp.invalidate()
    expect_raises(
        "revoked token → 401 immediately",
        AuthError,
        lambda: temp.list_keys(),
    )


def test_wildcard_admin_include_deleted(tokens, keks):
    """Regression: the include_deleted/purge admin gate must honor the "*"
    wildcard scope like every other admin site. A "*"-scoped superuser operator
    (SignedAPIKeyMixin's default scope) is granted every capability, so it must
    pass the gate — a raw ``SCOPE_ADMIN in scopes`` membership test wrongly
    denied it, stranding soft-deleted-but-retained versions during a
    "*"-operator KEK rotation. A caller holding neither admin nor "*" stays
    denied 403 on the same gate."""
    print("\n== authz: '*' wildcard scope satisfies the include_deleted admin gate ==")
    ops = client_for(tokens, keks, "operator:admin", "")

    _, created = ops._request(
        "POST", "/v1/admin/identities", body={"name": "star:super", "scopes": "*"}
    )
    ops._request(
        "POST",
        "/v1/admin/grants",
        body={
            "identity": "star:super",
            "namespace": "staging/api",
            "read": True,
            "write": True,
        },
    )
    star_hdr = {"Authorization": f"Bearer {created['token']}"}
    kek_id, kek = keks["staging/api"]
    star = SecretsClient(
        BASE, token=created["token"], namespace="staging/api", kek=kek, kek_id=kek_id
    )
    star.put_secret("wild_deleted", b"retain-me")
    star.delete_secret("wild_deleted")  # soft delete, envelope retained

    # The soft-deleted key is hidden by default; it surfaces only under
    # include_deleted=1 — so seeing it proves the "*" caller cleared the admin
    # gate (and that the deleted filter is what otherwise hides it).
    live = http_get(f"{BASE}/v1/secrets/staging/api", headers=star_hdr)
    live_keys = {k["key"] for k in json.loads(live.body)["keys"]}
    check(
        "'*' caller default list omits the soft-deleted key",
        live.status == 200 and "wild_deleted" not in live_keys,
        f"status={live.status} keys={live_keys}",
    )
    incl = http_get(
        f"{BASE}/v1/secrets/staging/api?include_deleted=1", headers=star_hdr
    )
    incl_keys = {k["key"] for k in json.loads(incl.body)["keys"]}
    check(
        "'*' scope passes the include_deleted admin gate (deleted key visible)",
        incl.status == 200 and "wild_deleted" in incl_keys,
        f"status={incl.status} keys={incl_keys}",
    )

    # A caller with neither admin nor "*" (service:staging-api holds "read" and a
    # read grant on this namespace) is denied 403 on the same gate.
    denied = http_get(
        f"{BASE}/v1/secrets/staging/api?include_deleted=1",
        headers={"Authorization": f"Bearer {tokens['service:staging-api']}"},
    )
    check(
        "read-only caller denied include_deleted → 403",
        denied.status == 403,
        f"status={denied.status}",
    )

    # Cleanup: the '*' caller also purges (admin+write gate) — then revoke it.
    star.delete_secret("wild_deleted", purge=True)
    ops._request("DELETE", "/v1/admin/identities/star:super")


def test_audit(tokens, keks):
    print("\n== audit: reads, writes, denials all recorded ==")
    # No sleep: the audit query endpoint calls flush_pending() before selecting,
    # so buffered rows are drained on the read path — timing crutches are false.
    ops = client_for(tokens, keks, "operator:admin", "")

    _, unfiltered = ops._request("GET", "/v1/audit", query={"limit": "500"})
    check(
        "audit trail non-empty",
        len(unfiltered["entries"]) > 0,
        "unfiltered audit query returned nothing",
    )

    _, ok_reads = ops._request(
        "GET",
        "/v1/audit",
        query={
            "namespace": "prod/api",
            "action": "read",
            "outcome": "ok",
            "limit": "200",
        },
    )
    check(
        "successful reads audited",
        len(ok_reads["entries"]) > 0,
        f"filtered empty; unfiltered sample: {unfiltered['entries'][:3]!r}",
    )
    identities = {e["identity"] for e in ok_reads["entries"]}
    check("audit rows carry identity", "service:prod-api" in identities)

    _, denials = ops._request(
        "GET", "/v1/audit", query={"outcome": "denied", "limit": "200"}
    )
    denied_identities = {e["identity"] for e in denials["entries"]}
    check(
        "denials audited with identity",
        "service:prod-frontend" in denied_identities,
        f"got {denied_identities}",
    )
    check(
        "unauthenticated denials audited",
        "" in denied_identities or any(e["identity"] == "" for e in denials["entries"]),
    )

    _, writes = ops._request(
        "GET", "/v1/audit", query={"action": "write", "outcome": "ok", "limit": "50"}
    )
    check(
        "writes audited", any(e["key"] == "webhook_secret" for e in writes["entries"])
    )


def test_secrets_run(tokens, keks):
    print("\n== secrets_run: exec + env-file injection ==")
    env = dict(os.environ)
    env["HYPERSECRET_URL"] = BASE
    env["HYPERSECRET_TOKEN"] = tokens["service:prod-api"]
    env["HYPERSECRET_NAMESPACE"] = "prod/api"
    env["HYPERSECRET_KEK_FILE"] = str(DEMO_DIR / "prod-api.kek")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.hypersecret.secrets_run",
            "--keys",
            "stripe_key,db_password",
            "--",
            "/usr/bin/env",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(
        "exec mode injects env vars",
        result.returncode == 0
        and "STRIPE_KEY=sk_live_demo_4242424242424242" in result.stdout
        and "DB_PASSWORD=prod-api-db-pw-3f9c2e" in result.stdout,
        result.stderr[-300:],
    )

    out_file = DEMO_DIR / "injected.env"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.hypersecret.secrets_run",
            "--all",
            "--output",
            str(out_file),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    content = out_file.read_text() if out_file.exists() else ""
    check(
        "env-file mode (--all) writes all namespace secrets",
        result.returncode == 0
        and "STRIPE_KEY=" in content
        and "JWT_SECRET=" in content,
        result.stderr[-300:],
    )
    check(
        "env file is 0600",
        out_file.exists() and (out_file.stat().st_mode & 0o777) == 0o600,
    )

    # Strict mode: unknown secret aborts the launch.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.hypersecret.secrets_run",
            "--keys",
            "does_not_exist",
            "--",
            "/usr/bin/env",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check("strict mode refuses to exec on missing secret", result.returncode == 1)

    # No selector (--map/--keys/--all) refuses rather than injecting everything.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "services.hypersecret.secrets_run",
            "--",
            "/usr/bin/env",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(
        "no selector refuses (no inject-all default)",
        result.returncode == 1 and "specify which secrets" in result.stderr,
        result.stderr[-200:],
    )


def _provision_cli(env, *args):
    return subprocess.run(
        [sys.executable, "-m", "services.hypersecret.provision", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_provision_cli(tokens, keks):
    print("\n== provision CLI: keygen / namespace / grant / put / get / rewrap ==")
    env = dict(os.environ)
    env["HYPERSECRET_URL"] = BASE
    env["HYPERSECRET_TOKEN"] = tokens["operator:admin"]

    kek_v1 = DEMO_DIR / "cli-payments-v1.kek"
    kek_v2 = DEMO_DIR / "cli-payments-v2.kek"
    result = _provision_cli(
        env, "keygen", "--out", str(kek_v1), "--kek-id", "payments-v1"
    )
    check(
        "cli keygen writes 0600 KEK file",
        result.returncode == 0 and (kek_v1.stat().st_mode & 0o777) == 0o600,
        result.stderr[-300:],
    )

    steps = [
        (
            "namespace create",
            ["namespace", "create", "prod/payments", "--kek-id", "payments-v1"],
        ),
        (
            "grant operator write",
            ["grant", "operator:admin", "prod/payments", "--read", "--write"],
        ),
    ]
    for name, argv in steps:
        result = _provision_cli(env, *argv)
        check(f"cli {name}", result.returncode == 0, result.stderr[-300:])

    result = _provision_cli(
        env,
        "put",
        "prod/payments",
        "api_key",
        "--kek-file",
        str(kek_v1),
        "--value",
        "cli-provisioned-secret",
    )
    check(
        "cli put",
        result.returncode == 0 and "version 1" in result.stdout,
        result.stderr[-300:],
    )

    result = _provision_cli(
        env, "get", "prod/payments", "api_key", "--kek-file", str(kek_v1)
    )
    check(
        "cli get roundtrip",
        result.returncode == 0 and result.stdout.strip() == "cli-provisioned-secret",
        result.stderr[-300:],
    )

    result = _provision_cli(
        env, "keygen", "--out", str(kek_v2), "--kek-id", "payments-v2"
    )
    check("cli keygen v2", result.returncode == 0, result.stderr[-300:])
    result = _provision_cli(
        env,
        "rewrap",
        "prod/payments",
        "--old-kek-file",
        str(kek_v1),
        "--new-kek-file",
        str(kek_v2),
    )
    check(
        "cli rewrap whole namespace",
        result.returncode == 0 and "declares payments-v2" in result.stdout,
        result.stderr[-300:],
    )
    result = _provision_cli(
        env, "get", "prod/payments", "api_key", "--kek-file", str(kek_v2)
    )
    check(
        "cli get decrypts under rotated KEK",
        result.returncode == 0 and result.stdout.strip() == "cli-provisioned-secret",
        result.stderr[-300:],
    )
    result = _provision_cli(
        env, "get", "prod/payments", "api_key", "--kek-file", str(kek_v1)
    )
    check("cli get with retired KEK fails", result.returncode == 1)


def test_provision_help(tokens, keks):
    """`provision --help` exits 0 and renders the epilog's operational
    walkthrough (the module guidance), not merely a bare subcommand list."""
    print("\n== provision --help renders operational guidance ==")
    result = _provision_cli(dict(os.environ), "--help")
    check(
        "provision --help exits 0 with epilog guidance",
        result.returncode == 0
        and "trusted operator path" in result.stdout
        and "keygen" in result.stdout
        and "rewrap" in result.stdout,
        result.stdout[-300:],
    )


def test_health_and_parity(tokens, keks):
    print("\n== health + native-vs-ASGI parity spot checks ==")
    resp = http_get(f"{BASE}/health")
    check("liveness", resp.status == 200)
    resp = http_get(f"{BASE}/ready")
    check("readiness (DB check)", resp.status == 200 and resp.json["status"] == "ok")
    checks = resp.json.get("checks", {})
    check(
        "readiness includes background-machinery checks",
        checks.get("scheduler") == "ok" and checks.get("mtls_terminator") == "ok",
        f"checks={checks}",
    )

    # Dual-dispatch drift guard: the same requests through the in-process
    # ASGI TestClient must match the native server byte-for-byte on
    # status + body.
    from hyperdjango.testing import TestClient
    from services.hypersecret.app import app

    tc = TestClient(app)
    token = tokens["service:prod-api"]
    cases = [
        ("GET", "/", None),
        ("GET", "/v1/secrets/prod/api/stripe_key", token),
        ("GET", "/v1/secrets/prod/api/missing_key", token),
        ("GET", "/v1/namespaces", token),
        ("GET", "/v1/secrets/prod/frontend/cdn_purge_token", token),  # 403
        ("GET", "/v1/audit", token),  # 403 (no audit scope)
    ]
    for method, path, tok in cases:
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        asgi = tc.request(method, path, headers=headers)
        native = http_get(f"{BASE}{path}", headers=headers)
        same_status = asgi.status == native.status
        same_body = asgi.json() == native.json
        check(
            f"parity {method} {path}",
            same_status and same_body,
            f"asgi={asgi.status} native={native.status}",
        )


def test_validation_caps(tokens, keks):
    """Metadata is annotation, not payload: its serialized JSON is capped at
    8KB (both sides of the boundary). And a naive rotation_due is normalized to
    UTC so it compares against the tz-aware sweep clock rather than firing off
    by the writer's session offset."""
    print("\n== validation: 8KB metadata cap + naive rotation_due → UTC ==")
    from services.hypersecret.app import _MAX_METADATA_BYTES, _parse_iso

    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)

    # Serialized overhead of {"pad": "…"} is 11 bytes; land just under / over.
    under = {"pad": "x" * (_MAX_METADATA_BYTES - 11 - 200)}
    over = {"pad": "x" * _MAX_METADATA_BYTES}
    check(
        "metadata just under 8KB accepted",
        ops.put_secret("meta_cap_ok", b"v1", metadata=under) == 1,
    )
    expect_raises(
        "metadata over 8KB → 400",
        SecretsError,
        lambda: ops.put_secret("meta_cap_big", b"v1", metadata=over),
    )

    naive = _parse_iso("2026-01-15T12:00:00")
    check("naive rotation_due parsed", naive is not None)
    check(
        "naive rotation_due normalized to UTC (compares against aware now)",
        naive == datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )
    aware = _parse_iso("2026-01-15T12:00:00+05:00")
    check(
        "aware rotation_due offset preserved",
        aware.utcoffset() == timedelta(hours=5),
    )
    # A write carrying a naive rotation_due must round-trip cleanly.
    check(
        "write with naive rotation_due succeeds",
        ops.put_secret(
            "rotation_naive", b"v1", metadata={"rotation_due": "2026-01-15T12:00:00"}
        )
        == 1,
    )


def test_audit_conflict_durability(tokens, keks):
    """Regression: a version conflict must audit on a CLEAN connection after the
    write transaction rolls back — never inside it, where a tripped batch flush
    would discard the conflict row and every unrelated request's buffered audit
    row along with the rollback. Drive a conflict and assert the conflict row
    AND a previously-buffered write row both survive."""
    print("\n== audit durability: conflict row + buffered rows survive rollback ==")
    from services.hypersecret.envelope import seal

    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)

    # Buffer a uniquely-identifiable audit row (a successful write) that must
    # still be present after the conflict path's rollback.
    marker_key = "audit_durability_marker"
    ops.put_secret(marker_key, b"marker-v1")

    # Drive a version conflict on a fresh key: the write transaction rolls back
    # (dropping the just-created empty Secret) and audits the conflict.
    conflict_key = "audit_durability_conflict"
    kek_id, kek = keks["prod/api"]
    stale = seal(
        b"nope",
        kek=kek,
        kek_id=kek_id,
        namespace="prod/api",
        key=conflict_key,
        version=5,  # server expects 1 for a brand-new key
    )
    body = stale.to_dict()
    body["version"] = 5
    expect_raises(
        "stale version on fresh key → 409",
        VersionConflict,
        lambda: ops._request("POST", f"/v1/secrets/prod/api/{conflict_key}", body=body),
    )

    # The /v1/audit read flushes pending rows first, but audit is a BATCHED
    # subsystem written on a clean connection after the conflict rollback, so
    # under peak parallel load the row can land just after the first read.
    # Poll the flush-and-read: this still proves DURABILITY (the row must
    # eventually appear on a fresh connection), it just tolerates the batch's
    # eventual-consistency rather than racing a single point-in-time query — a
    # never-durable row would never appear and still fail.
    auditor = client_for(tokens, keks, "operator:admin", "")

    def _audit_has(query: dict, key: str) -> bool:
        _, page = auditor._request("GET", "/v1/audit", query=query)
        return any(e["key"] == key for e in page["entries"])

    check(
        "conflict row survives (audited after rollback)",
        wait_for(
            lambda: _audit_has(
                {"action": "write", "outcome": "conflict", "limit": "200"},
                conflict_key,
            ),
            timeout=10,
        ),
    )
    check(
        "previously-buffered write row survives the conflict rollback",
        wait_for(
            lambda: _audit_has(
                {"action": "write", "key": marker_key, "outcome": "ok", "limit": "50"},
                marker_key,
            ),
            timeout=10,
        ),
    )


def test_metrics_auth(tokens, keks):
    """Regression: /metrics exposes namespace names + per-namespace access
    volume, so it must require a resolved identity — any valid token, no grant
    needed — and reject anonymous and forged scrapes."""
    print("\n== metrics: authenticated scrape only ==")
    anon = http_get(f"{BASE}/metrics")
    check(
        "anonymous /metrics denied",
        anon.status in (401, 403),
        f"status={anon.status}",
    )
    forged = http_get(
        f"{BASE}/metrics", headers={"Authorization": "Bearer hsk_forged_garbage"}
    )
    check(
        "forged token /metrics denied",
        forged.status in (401, 403),
        f"status={forged.status}",
    )
    # service:prod-api holds only a read scope + one namespace grant: scraping
    # needs neither, just a resolved identity.
    authed = http_get(
        f"{BASE}/metrics",
        headers={"Authorization": f"Bearer {tokens['service:prod-api']}"},
    )
    check(
        "authenticated identity may scrape (no grant needed)",
        authed.status == 200 and "hypersecret_requests_total" in authed.body,
        f"status={authed.status}",
    )


def _audit_count(auditor, **query) -> int:
    query.setdefault("limit", "500")
    _, payload = auditor._request("GET", "/v1/audit", query=query)
    return len(payload["entries"])


def test_batch_retained_version(tokens, keks):
    """Regression: batch fetch must resolve a secret that carries a RETAINED
    older version whose number collides with another key's current_version. The
    version__in cross-product returns the stale row too; a last-write-wins map
    (pre-fix) could report the existing multi-version secret as null."""
    print("\n== batch fetch: retained old version does not mask a live secret ==")
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)

    # multi: current_version=2 with a retained v1; single: current_version=1.
    # The version__in filter for the batch is {2, 1}, so multi's own v1 row is
    # also returned and could clobber its v2 entry under the old bug.
    ops.put_secret("batch_multi", b"multi-v1")
    ops.put_secret("batch_multi", b"multi-v2")
    ops.put_secret("batch_single", b"single-v1")

    got = ops.get_secrets(["batch_multi", "batch_single"])
    check(
        "both keys resolve (retained-version collision handled)",
        got.get("batch_multi") == "multi-v2" and got.get("batch_single") == "single-v1",
        f"got={got}",
    )

    # Raw batch response: neither entry is null, and multi reports its current.
    _, raw = ops._request(
        "POST", "/v1/batch/prod/api", body={"keys": ["batch_multi", "batch_single"]}
    )
    secrets = raw["secrets"]
    check(
        "raw batch: existing secret never null, current version returned",
        secrets["batch_multi"] is not None
        and secrets["batch_multi"]["version"] == 2
        and secrets["batch_single"] is not None,
        f"secrets={secrets}",
    )


def test_client_idempotent_retry(tokens, keks):
    """Regression: the SDK must NOT replay a non-idempotent verb on a transport
    failure — a lost put/delete response that the server DID apply would, on
    retry, 409 and (via the conflict-retry) append a DUPLICATE version. Idempotent
    GETs still retry. Proven by counting transport attempts under a forced fault."""
    print("\n== client: non-idempotent verbs are not replayed on transport fault ==")
    import urllib.error

    calls = {"n": 0}

    api = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
    # The transport lives in ServiceClient.request_raw, which drives requests
    # through this client's own opener (self._opener) — not the module-level
    # urllib.request.urlopen. Fault the opener to count transport attempts:
    # every retried request re-enters _opener.open once.
    real_open = api._opener.open

    def fake_open(*_a, **_kw):
        calls["n"] += 1
        raise urllib.error.URLError("forced transport failure")

    # retries default 2 → max_attempts 3 for idempotent verbs.
    api._opener.open = fake_open
    try:
        calls["n"] = 0
        expect_raises(
            "POST fails closed (ServerUnavailable)",
            SecretsError,
            lambda: api._request(
                "POST", "/v1/secrets/prod/api/idem_probe", body={"x": 1}
            ),
        )
        check(
            "non-idempotent POST tried exactly once", calls["n"] == 1, f"n={calls['n']}"
        )

        calls["n"] = 0
        expect_raises(
            "DELETE fails closed (ServerUnavailable)",
            SecretsError,
            lambda: api._request("DELETE", "/v1/secrets/prod/api/idem_probe"),
        )
        check(
            "non-idempotent DELETE tried exactly once",
            calls["n"] == 1,
            f"n={calls['n']}",
        )

        calls["n"] = 0
        expect_raises(
            "GET fails closed after retries",
            SecretsError,
            lambda: api._request("GET", "/v1/secrets/prod/api/idem_probe"),
        )
        check(
            "idempotent GET retried up to the policy",
            calls["n"] == 3,
            f"n={calls['n']}",
        )
    finally:
        api._opener.open = real_open

    # put_secret's own 409-retry loop only catches VersionConflict, so a
    # ServerUnavailable from a non-retried POST propagates — never a re-seal.
    api._opener.open = fake_open
    try:
        calls["n"] = 0
        expect_raises(
            "put_secret propagates transport failure without re-sealing",
            SecretsError,
            lambda: api.put_secret("idem_probe2", b"v"),
        )
        # The version lookup (idempotent GET, 3 tries) fails closed first, so
        # no seal+POST ever runs — and the conflict-retry loop only catches
        # VersionConflict, so it can never loop into a duplicate write.
        check(
            "put_secret fails closed before any write (no duplicate)",
            calls["n"] == 3,
            f"n={calls['n']}",
        )
    finally:
        api._opener.open = real_open


def test_scheduler_skip_if_running(tokens, keks):
    """Regression: the outbox drain and the sweeps must register with
    skip_if_running=True so a slow pass (a down hub blocks the drain ~15s)
    cannot stack backlogged instances that starve the other jobs."""
    print("\n== scheduler: sweeps + drain register skip_if_running ==")
    from services.hypersecret.app import _scheduler

    flags = {}
    for sid, job in _scheduler._jobs.items():
        entry = _scheduler._entries[sid]
        fire = job._BaseJob__handle
        freevars = fire.__code__.co_freevars
        cells = {
            name: fire.__closure__[i].cell_contents for i, name in enumerate(freevars)
        }
        task = cells.get("task_decorator")
        flags[getattr(task, "__name__", str(entry.schedule_id))] = cells.get(
            "skip_if_running"
        )

    for name in (
        "retention_sweep",
        "rotation_due_sweep",
        "outbox_drain",
        "audit_retention_sweep",
    ):
        check(
            f"{name} registered skip_if_running=True",
            flags.get(name) is True,
            f"flags={flags}",
        )


def test_denial_audit_coverage(tokens, keks):
    """Regression: post-gate denials, not-founds, and invalid input each write
    exactly one audit row with the right outcome (the audit contract: every
    access, including a rejected one, produces a row)."""
    print("\n== audit: post-gate denials / not-found / invalid all recorded ==")
    auditor = client_for(tokens, keks, "operator:admin", "")
    deployer = client_for(tokens, keks, "ci:deployer", "prod/api", cache_ttl=0)
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
    ns = "prod/api"

    # (a) purge without admin scope (deployer has write, not admin) → denied.
    expect_raises(
        "purge without admin denied",
        AuthError,
        lambda: deployer.delete_secret("denial_purge_key", purge=True),
    )
    # (b) versions on a missing secret → not_found.
    expect_raises(
        "versions on missing secret 404",
        SecretNotFound,
        lambda: ops.versions("denial_versions_missing"),
    )
    # (c) rewrap a missing secret → not_found. The encrypted_dek must be a
    # valid-length wrapped DEK so it clears the exact-size check and reaches the
    # not_found branch (a bad length would 400 as invalid instead).
    rewrap_body = {
        "version": 1,
        "encrypted_dek": _valid_dek_b64(),
        "kek_id": keks[ns][0],
    }
    expect_raises(
        "rewrap missing secret 404",
        SecretNotFound,
        lambda: ops._request(
            "POST", f"/v1/secrets/{ns}/denial_rewrap_missing/rewrap", body=rewrap_body
        ),
    )
    # (d) expose a missing secret → not_found (admin scope, no such key).
    expect_raises(
        "expose missing secret 404",
        SecretNotFound,
        lambda: ops._request(
            "POST", f"/v1/secrets/{ns}/denial_expose_missing/expose", body={}
        ),
    )
    # (e) a malformed write (bad envelope) → invalid.
    expect_raises(
        "malformed write 400",
        SecretsError,
        lambda: ops._request(
            "POST", f"/v1/secrets/{ns}/denial_invalid_key", body={"format": "wrong"}
        ),
    )

    checks = [
        ("purge", "denied", "denial_purge_key", None),
        ("versions", "not_found", "denial_versions_missing", ns),
        ("rewrap", "not_found", "denial_rewrap_missing", ns),
        ("expose", "not_found", "denial_expose_missing", ns),
        ("write", "invalid", "denial_invalid_key", ns),
    ]
    for action, outcome, key, namespace in checks:
        query = {"action": action, "outcome": outcome, "key": key}
        if namespace:
            query["namespace"] = namespace
        n = _audit_count(auditor, **query)
        check(
            f"{action}/{outcome} writes exactly one audit row ({key})",
            n == 1,
            f"count={n}",
        )


def test_rewrap_covers_soft_deleted(tokens, keks):
    """Regression: KEK rotation must rewrap the retained versions of
    soft-deleted secrets too — otherwise a later revive is undecryptable once
    the old KEK is retired. Rewrap a namespace holding a soft-deleted secret,
    then revive it and read the pre-rewrap version under the NEW KEK."""
    print("\n== rewrap: soft-deleted retained secrets are covered ==")
    env = dict(os.environ)
    env["HYPERSECRET_URL"] = BASE
    env["HYPERSECRET_TOKEN"] = tokens["operator:admin"]

    kek_v1 = DEMO_DIR / "rwdel-v1.kek"
    kek_v2 = DEMO_DIR / "rwdel-v2.kek"
    for out, kid in ((kek_v1, "rwdel-v1"), (kek_v2, "rwdel-v2")):
        r = _provision_cli(env, "keygen", "--out", str(out), "--kek-id", kid)
        check(f"keygen {kid}", r.returncode == 0, r.stderr[-200:])

    for name, argv in [
        (
            "namespace create",
            ["namespace", "create", "prod/rwdel", "--kek-id", "rwdel-v1"],
        ),
        (
            "grant operator",
            ["grant", "operator:admin", "prod/rwdel", "--read", "--write"],
        ),
    ]:
        r = _provision_cli(env, *argv)
        check(f"rwdel {name}", r.returncode == 0, r.stderr[-200:])

    r = _provision_cli(
        env,
        "put",
        "prod/rwdel",
        "revivable",
        "--kek-file",
        str(kek_v1),
        "--value",
        "revive-me",
    )
    check("put revivable v1", r.returncode == 0, r.stderr[-200:])
    r = _provision_cli(env, "delete", "prod/rwdel", "revivable")
    check("soft-delete revivable", r.returncode == 0, r.stderr[-200:])

    # Rewrap the whole namespace — must include the soft-deleted secret.
    r = _provision_cli(
        env,
        "rewrap",
        "prod/rwdel",
        "--old-kek-file",
        str(kek_v1),
        "--new-kek-file",
        str(kek_v2),
    )
    check(
        "rewrap reports the soft-deleted version was covered",
        r.returncode == 0 and "Rewrapped 1 versions" in r.stdout,
        f"stdout={r.stdout[-200:]} stderr={r.stderr[-200:]}",
    )

    # Revive (seals a v2 under the new KEK) then read the PRE-rewrap v1 under
    # the new KEK: it decrypts only if rewrap covered the soft-deleted version.
    r = _provision_cli(
        env,
        "put",
        "prod/rwdel",
        "revivable",
        "--kek-file",
        str(kek_v2),
        "--value",
        "revived-v2",
    )
    check("revive revivable under new KEK", r.returncode == 0, r.stderr[-200:])
    r = _provision_cli(
        env,
        "get",
        "prod/rwdel",
        "revivable",
        "--kek-file",
        str(kek_v2),
        "--version",
        "1",
    )
    check(
        "pre-rewrap version decrypts under the new KEK (was rewrapped)",
        r.returncode == 0 and r.stdout.strip() == "revive-me",
        f"stdout={r.stdout[-200:]} stderr={r.stderr[-200:]}",
    )


def test_strict_int_params(tokens, keks):
    """Regression: version/known_version parse ASCII-strict — a Unicode-digit
    like ?version=² must 400, not 500 on int(), and a non-numeric ?version=abc
    must 400 rather than silently serving current."""
    print("\n== validation: strict integer query params ==")
    auth = {"Authorization": f"Bearer {tokens['service:prod-api']}"}
    base = f"{BASE}/v1/secrets/prod/api/stripe_key"

    r = http_get(base + "?version=%C2%B2", headers=auth)  # "²"
    check(
        "unicode-digit ?version=² → 400 (no 500)", r.status == 400, f"status={r.status}"
    )
    r = http_get(base + "?version=abc", headers=auth)
    check("non-numeric ?version=abc → 400", r.status == 400, f"status={r.status}")
    r = http_get(base + "?known_version=%C2%B2", headers=auth)
    check(
        "unicode-digit ?known_version=² → 400 (no 500)",
        r.status == 400,
        f"status={r.status}",
    )
    # A valid pin still works, and no-version still serves current.
    r = http_get(base + "?version=1", headers=auth)
    check("valid ?version=1 still 200", r.status == 200, f"status={r.status}")
    r = http_get(base, headers=auth)
    check("no version serves current (200)", r.status == 200, f"status={r.status}")


def test_audit_retention_sweep(tokens, keks):
    """Regression: the audit-retention sweep trims rows past the window so the
    append-only access log stays bounded, while fresh rows survive."""
    print("\n== audit retention: aged rows swept, fresh rows kept ==")
    auditor = client_for(tokens, keks, "operator:admin", "")

    _insert_audit_row("sweep:victim", datetime.now(UTC) - timedelta(days=3650))
    _insert_audit_row("sweep:survivor", datetime.now(UTC))

    def victim_gone() -> bool:
        return _audit_count(auditor, identity="sweep:victim") == 0

    check(
        "aged audit row is trimmed by the retention sweep",
        wait_for(victim_gone, timeout=15),
    )
    check(
        "fresh audit row survives the sweep",
        _audit_count(auditor, identity="sweep:survivor") >= 1,
    )


def test_retention_revive_race(tokens, keks):
    """Regression: the retention sweep must not purge a secret revived between
    its snapshot and the per-secret transaction. Soft-delete + backdate a
    secret, capture the sweep's stale snapshot row, revive it (a concurrent put
    clears deleted_at), then drive the sweep's per-secret purge directly with a
    cutoff the backdated deleted_at matched — the re-select FOR UPDATE must skip
    the revived row (no purge, secret intact)."""
    print("\n== retention: revive between snapshot and tx is not purged ==")
    from services.hypersecret.app import _purge_if_still_expired
    from services.hypersecret.models import Secret

    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
    ops.put_secret("revive_race", b"v1")
    ops.delete_secret("revive_race")  # soft delete
    # deleted_at stays FRESH so the app's own periodic sweep (real 14-day
    # cutoff) never selects this secret; the race is driven below by handing
    # the purge helper a future cutoff that the fresh deleted_at matches.

    async def _load_stale():
        return await Secret.objects.filter(key="revive_race").first()

    stale = asyncio.run(_load_stale())
    check(
        "stale snapshot captured while soft-deleted",
        stale is not None and stale.deleted_at is not None,
    )

    # A concurrent put revives it AFTER the snapshot (clears deleted_at).
    check("secret revived at next version", ops.put_secret("revive_race", b"v2") == 2)

    # Drive the per-secret purge with the stale snapshot and a cutoff ahead of
    # the fresh deleted_at; the guard's re-select must skip the revived row.
    cutoff = datetime.now(UTC) + timedelta(days=1)
    purged = asyncio.run(_purge_if_still_expired(stale, "prod/api", cutoff))
    check("revived secret NOT purged (re-select FOR UPDATE guard)", purged is False)
    check("revived secret intact after sweep", ops.get_secret("revive_race") == "v2")


def _namespace_metric_labels(auth) -> set[str]:
    resp = http_get(f"{BASE}/metrics", headers=auth)
    labels: set[str] = set()
    for line in resp.body.splitlines():
        if line.startswith("hypersecret_namespace_access_total{"):
            head, _, tail = line.partition('namespace="')
            if tail:
                labels.add(tail.split('"', 1)[0])
    return labels


def test_unauth_metric_cardinality(tokens, keks):
    """Regression: unauthenticated grammar-valid spam of DISTINCT namespaces
    must not mint per-namespace metric labels (the label set is bounded to
    namespaces a caller actually holds a grant on). The denial audit rows still
    flow to AccessLog — only the metric label set is bounded."""
    print("\n== metrics: unauthenticated spam does not grow namespace labels ==")
    auth = {"Authorization": f"Bearer {tokens['operator:admin']}"}
    before = _namespace_metric_labels(auth)
    for i in range(25):
        http_get(f"{BASE}/v1/secrets/spam{i}/svc{i}/key{i}")  # unauthenticated
    after = _namespace_metric_labels(auth)
    check(
        "no spam namespace minted a metric label",
        not any(lbl.startswith("spam") for lbl in after),
        f"labels={sorted(after)}",
    )
    check(
        "unauthenticated spam added no new namespace labels",
        not any(lbl.startswith("spam") for lbl in after - before),
        f"new={sorted(after - before)}",
    )


def test_rewrap_dek_integrity(tokens, keks):
    """Regression (item 5): encrypted_dek must decode to the exact wrapped-DEK
    wire size on BOTH put and rewrap (a short/long blob → 400, never an in-place
    overwrite that bricks a version); and a valid rewrap retains the prior
    (encrypted_dek, kek_id) as a one-deep undo."""
    print("\n== rewrap integrity: exact encrypted_dek size + prior-pair undo ==")
    from services.hypersecret.envelope import (
        SealedEnvelope,
        generate_kek,
        rewrap_dek,
        seal,
    )
    from services.hypersecret.models import Secret, SecretVersion

    ns = "prod/api"
    kek_id, kek = keks[ns]
    hdr = {"Authorization": f"Bearer {tokens['operator:admin']}"}
    ops = client_for(tokens, keks, "operator:admin", ns, cache_ttl=0)

    short = base64.b64encode(b"\x00" * 40).decode()
    long = base64.b64encode(b"\x00" * 80).decode()

    def put_body(dek_b64: str) -> dict:
        env = seal(
            b"x", kek=kek, kek_id=kek_id, namespace=ns, key="dek_len_bad", version=1
        )
        body = env.to_dict()
        body["encrypted_dek"] = dek_b64
        body["version"] = 1
        return body

    for label, dek in (("undersized", short), ("oversized", long)):
        r = http_post(
            f"{BASE}/v1/secrets/{ns}/dek_len_bad_{label}",
            body=put_body(dek),
            headers=hdr,
        )
        check(
            f"{label} encrypted_dek on put → 400", r.status == 400, f"status={r.status}"
        )

    # A real version to rewrap, then bad-length rewrap → 400 (no overwrite).
    ops.put_secret("dek_len", b"v1")
    for label, dek in (("undersized", short), ("oversized", long)):
        r = http_post(
            f"{BASE}/v1/secrets/{ns}/dek_len/rewrap",
            body={"version": 1, "encrypted_dek": dek, "kek_id": kek_id},
            headers=hdr,
        )
        check(
            f"{label} encrypted_dek on rewrap → 400",
            r.status == 400,
            f"status={r.status}",
        )

    # A valid rewrap retains the prior (encrypted_dek, kek_id).
    _, payload = ops._request(
        "GET", f"/v1/secrets/{ns}/dek_len", query={"version": "1"}
    )
    old_dek, old_kek_id = payload["encrypted_dek"], payload["kek_id"]
    env = SealedEnvelope.from_dict(payload)
    new_kek = generate_kek()
    new_dek = rewrap_dek(
        env,
        old_kek=kek,
        new_kek=new_kek,
        new_kek_id="dek-len-v2",
        namespace=ns,
        key="dek_len",
        version=1,
    )
    ops.rewrap_version("dek_len", 1, new_dek, "dek-len-v2")

    async def _load_version():
        secret = await Secret.objects.filter(key="dek_len").first()
        return await SecretVersion.objects.filter(
            secret_id=secret.id, version=1
        ).first()

    sv = asyncio.run(_load_version())
    check(
        "rewrap updated the current wrapped DEK",
        sv.encrypted_dek == new_dek and sv.kek_id == "dek-len-v2",
    )
    check(
        "prev_encrypted_dek retained the pre-rewrap wrapped DEK",
        sv.prev_encrypted_dek == old_dek,
    )
    check("prev_kek_id retained the pre-rewrap kek_id", sv.prev_kek_id == old_kek_id)

    # undo_rewrap rolls the wrapped DEK back to the retained pair (recovering a
    # version bricked by a bad rewrap) and spends the one-deep undo slot.
    undone = ops.undo_rewrap("dek_len", 1)
    check("undo reports the restored kek_id", undone["kek_id"] == old_kek_id)
    sv2 = asyncio.run(_load_version())
    check(
        "undo restored the pre-rewrap wrapped DEK + kek_id",
        sv2.encrypted_dek == old_dek and sv2.kek_id == old_kek_id,
    )
    check(
        "undo cleared the one-deep undo slot",
        sv2.prev_encrypted_dek == "" and sv2.prev_kek_id == "",
    )
    # One-shot: a second undo has nothing retained → 409 (audited invalid).
    r = http_post(
        f"{BASE}/v1/secrets/{ns}/dek_len/rewrap/undo",
        body={"version": 1},
        headers=hdr,
    )
    check("second undo (nothing retained) → 409", r.status == 409, f"status={r.status}")
    # An unknown version → 404 (audited not_found).
    r = http_post(
        f"{BASE}/v1/secrets/{ns}/dek_len/rewrap/undo",
        body={"version": 999},
        headers=hdr,
    )
    check("undo of unknown version → 404", r.status == 404, f"status={r.status}")

    # The undo endpoint is admin-gated: a write-scoped caller (ci:deployer holds
    # write on prod/api but not admin) is denied 403 before any undo runs — the
    # admin gate is not implied by the write grant the rewrap itself needs.
    r = http_post(
        f"{BASE}/v1/secrets/{ns}/dek_len/rewrap/undo",
        body={"version": 1},
        headers={"Authorization": f"Bearer {tokens['ci:deployer']}"},
    )
    check(
        "undo by write-scope (non-admin) caller → 403",
        r.status == 403,
        f"status={r.status}",
    )

    # Purge the scratch key so it never joins prod/api's real key set that the
    # later `--all` injection test enumerates.
    ops.delete_secret("dek_len", purge=True)


def test_audit_contract_gaps(tokens, keks):
    """Regression (item 7): post-gate 400s that previously wrote NO audit row now
    audit under outcome=invalid; the batch gate + per-key rows share the
    batch_read action; admin input rejections audit; /metrics auth denials
    audit."""
    print("\n== audit contract: post-gate 400s / batch taxonomy / metrics denial ==")
    auditor = client_for(tokens, keks, "operator:admin", "")
    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
    ns = "prod/api"
    hdr = {"Authorization": f"Bearer {tokens['operator:admin']}"}

    # (a) fetch with a non-numeric known_version → read/invalid.
    r = http_get(f"{BASE}/v1/secrets/{ns}/stripe_key?known_version=abc", headers=hdr)
    check("invalid known_version → 400", r.status == 400, f"status={r.status}")
    # (b) batch with a malformed keys field → batch_read/invalid.
    r = http_post(f"{BASE}/v1/batch/{ns}", body={"keys": "notalist"}, headers=hdr)
    check("batch malformed keys → 400", r.status == 400, f"status={r.status}")
    # (c) rewrap with non-base64 encrypted_dek → rewrap/invalid (existing secret).
    ops.put_secret("audit_gap_rewrap", b"v1")
    r = http_post(
        f"{BASE}/v1/secrets/{ns}/audit_gap_rewrap/rewrap",
        body={"version": 1, "encrypted_dek": "!!not-base64!!", "kek_id": keks[ns][0]},
        headers=hdr,
    )
    check("rewrap bad base64 → 400", r.status == 400, f"status={r.status}")
    # (d) admin identity with too-short name → admin_identity/invalid.
    r = http_post(
        f"{BASE}/v1/admin/identities", body={"name": "x", "scopes": "read"}, headers=hdr
    )
    check("admin identity bad name → 400", r.status == 400, f"status={r.status}")
    # (e) unauthenticated /metrics scrape → metrics/denied.
    r = http_get(f"{BASE}/metrics")
    check("anon /metrics → 401", r.status in (401, 403), f"status={r.status}")
    # (f) revoke a non-existent identity → admin_revoke/not_found (was 404 with
    # no audit row).
    r = http_delete(f"{BASE}/v1/admin/identities/service:does-not-exist", headers=hdr)
    check("revoke unknown identity → 404", r.status == 404, f"status={r.status}")
    # (g) a non-object JSON body (valid JSON, wrong shape) on an admin grant →
    # admin_grant/invalid (was an AttributeError → unaudited 500).
    r = http_post(
        f"{BASE}/v1/admin/grants",
        body="[1, 2, 3]",
        headers={**hdr, "Content-Type": "application/json"},
    )
    check("non-object grant body → 400", r.status == 400, f"status={r.status}")

    for action, outcome, namespace in [
        ("read", "invalid", ns),
        ("batch_read", "invalid", ns),
        ("rewrap", "invalid", ns),
        ("admin_identity", "invalid", None),
        ("metrics", "denied", None),
        ("admin_revoke", "not_found", None),
        ("admin_grant", "invalid", None),
    ]:
        query = {"action": action, "outcome": outcome}
        if namespace:
            query["namespace"] = namespace
        n = _audit_count(auditor, **query)
        check(f"{action}/{outcome} now audited (>=1 row)", n >= 1, f"count={n}")

    # Taxonomy: a batch GATE denial audits action=batch_read (not "read"), same
    # as the per-key rows — no split vocabulary.
    frontend = client_for(tokens, keks, "service:prod-frontend", ns, cache_ttl=0)
    expect_raises(
        "frontend batch on prod/api denied",
        AuthError,
        lambda: frontend.get_secrets(["stripe_key"]),
    )

    # /v1/audit is read-your-writes (flush_pending drains the buffer AND waits
    # out in-flight concurrent flushes), so the denial row is queryable the
    # moment the denied request returns — no polling, by API guarantee.
    _, denials = auditor._request(
        "GET",
        "/v1/audit",
        query={"action": "batch_read", "outcome": "denied", "limit": "200"},
    )
    ids = {e["identity"] for e in denials["entries"]}
    check(
        "batch gate denial audits action=batch_read (taxonomy aligned)",
        "service:prod-frontend" in ids,
        f"ids={ids}",
    )


def test_rotation_due_clobber(tokens, keks):
    """Regression (item 8): a put that re-arms rotation_due to a FUTURE date
    between the sweep's snapshot and its per-secret update must not be clobbered.
    Drive the sweep's per-secret notify directly with a stale (past-due) snapshot
    after the row has been re-armed — the conditional update matches nothing, so
    no stale event fires and the future deadline stays armed."""
    print("\n== rotation-due sweep: re-armed deadline not clobbered ==")
    from services.hypersecret.app import _notify_if_still_due
    from services.hypersecret.models import Secret

    ops = client_for(tokens, keks, "operator:admin", "prod/api", cache_ttl=0)
    # Arm in the past → the sweep would notify it.
    ops.put_secret(
        "rotdue_clobber", b"v1", metadata={"rotation_due": "2020-01-01T00:00:00+00:00"}
    )

    async def _load():
        return await Secret.objects.filter(key="rotdue_clobber").first()

    stale = asyncio.run(_load())
    check(
        "stale snapshot armed past-due",
        stale is not None and stale.expiry_notified is False,
    )

    # A concurrent put re-arms rotation_due to the future (clears expiry_notified).
    ops.put_secret(
        "rotdue_clobber", b"v2", metadata={"rotation_due": "2099-01-01T00:00:00+00:00"}
    )

    fired = asyncio.run(_notify_if_still_due(stale, "prod/api", datetime.now(UTC)))
    check(
        "stale snapshot did NOT fire an expired event (conditional guard)",
        fired is False,
    )

    after = asyncio.run(_load())
    check(
        "future deadline stays armed (expiry_notified not clobbered to True)",
        after.expiry_notified is False,
    )


def test_admin_duplicate_conflict(tokens, keks):
    """Regression (item 9): a concurrent duplicate admin create must resolve to a
    clean 409, never a raw 500 from the unique constraint."""
    print("\n== admin: concurrent duplicate create → 409 (no 500) ==")
    import concurrent.futures

    hdr = {"Authorization": f"Bearer {tokens['operator:admin']}"}
    name = "dupe:identity"

    def attempt(_n):
        r = http_post(
            f"{BASE}/v1/admin/identities",
            body={"name": name, "scopes": "read"},
            headers=hdr,
        )
        return r.status

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(attempt, range(6)))
    check(
        "no attempt 500s",
        all(s in (201, 409) for s in statuses),
        f"statuses={statuses}",
    )
    check(
        "exactly one create succeeds", statuses.count(201) == 1, f"statuses={statuses}"
    )
    check(
        "the rest are clean 409s",
        statuses.count(409) == len(statuses) - 1,
        f"statuses={statuses}",
    )


def test_env_file_hygiene(tokens, keks):
    """Regression (item 10): write_env_file rejects newline/CR and systemd-quotes
    values with whitespace/quotes/backslash so they round-trip; plain values stay
    bare."""
    print("\n== secrets_run: env-file value hygiene (quote/reject) ==")
    from services.hypersecret.secrets_run import _format_env_value, write_env_file

    check(
        "plain value emitted bare",
        _format_env_value("K", "sk_live_abc") == "sk_live_abc",
    )
    check(
        "leading/trailing space is quoted (round-trips)",
        _format_env_value("K", "  padded  ") == '"  padded  "',
    )
    check(
        "embedded quote is escaped inside quotes",
        _format_env_value("K", 'a"b') == '"a\\"b"',
    )
    check(
        "backslash is escaped inside quotes",
        _format_env_value("K", "a\\b") == '"a\\\\b"',
    )
    for bad in ("line\nbreak", "carriage\rreturn"):
        raised = False
        try:
            _format_env_value("K", bad)
        except ValueError:
            raised = True
        check(f"newline/CR value rejected ({bad!r})", raised)

    out = DEMO_DIR / "hygiene.env"
    write_env_file(str(out), {"A": "plain", "B": "  spaced  "})
    content = out.read_text()
    check(
        "env file writes bare + quoted forms",
        "A=plain\n" in content and 'B="  spaced  "\n' in content,
        content,
    )


def test_provision_rewrap_resume(tokens, keks):
    """Regression (item 4): a rewrap rerun after a partial rotation must complete
    cleanly. Rewrap some versions to the new KEK by hand (simulating a crash
    mid-rotation), then run `provision rewrap` — it skips versions already on the
    new KEK instead of failing to unwrap them with the old KEK, and finishes."""
    print("\n== provision rewrap: resumes after a partial rotation ==")
    from services.hypersecret.envelope import (
        SealedEnvelope,
        load_kek_file,
        rewrap_dek,
    )

    env = dict(os.environ)
    env["HYPERSECRET_URL"] = BASE
    env["HYPERSECRET_TOKEN"] = tokens["operator:admin"]

    kek_v1 = DEMO_DIR / "resume-v1.kek"
    kek_v2 = DEMO_DIR / "resume-v2.kek"
    for out, kid in ((kek_v1, "resume-v1"), (kek_v2, "resume-v2")):
        r = _provision_cli(env, "keygen", "--out", str(out), "--kek-id", kid)
        check(f"keygen {kid}", r.returncode == 0, r.stderr[-200:])
    for name, argv in [
        (
            "namespace create",
            ["namespace", "create", "prod/resume", "--kek-id", "resume-v1"],
        ),
        (
            "grant operator",
            ["grant", "operator:admin", "prod/resume", "--read", "--write"],
        ),
    ]:
        r = _provision_cli(env, *argv)
        check(f"resume {name}", r.returncode == 0, r.stderr[-200:])

    # Two keys, each two versions, sealed under v1.
    v1_id, v1_kek = load_kek_file(str(kek_v1))
    v2_id, v2_kek = load_kek_file(str(kek_v2))
    ops = SecretsClient(
        BASE,
        token=tokens["operator:admin"],
        namespace="prod/resume",
        kek=v1_kek,
        kek_id=v1_id,
        cache_ttl=0,
    )
    for key in ("alpha", "beta"):
        ops.put_secret(key, b"one")
        ops.put_secret(key, b"two")

    # Simulate a crash mid-rotation: rewrap ONLY alpha v1 to the new KEK by hand.
    _, payload = ops._request(
        "GET", "/v1/secrets/prod/resume/alpha", query={"version": "1"}
    )
    partial = rewrap_dek(
        SealedEnvelope.from_dict(payload),
        old_kek=v1_kek,
        new_kek=v2_kek,
        new_kek_id=v2_id,
        namespace="prod/resume",
        key="alpha",
        version=1,
    )
    ops.rewrap_version("alpha", 1, partial, v2_id)

    # Rerun the whole rotation: it must skip alpha v1 (already on the new KEK) and
    # finish rather than DecryptError-ing trying to unwrap it with the old KEK.
    r = _provision_cli(
        env,
        "rewrap",
        "prod/resume",
        "--old-kek-file",
        str(kek_v1),
        "--new-kek-file",
        str(kek_v2),
    )
    check(
        "rewrap rerun completes cleanly after a partial rotation",
        r.returncode == 0 and "declares resume-v2" in r.stdout,
        f"stdout={r.stdout[-200:]} stderr={r.stderr[-200:]}",
    )
    # Every version now decrypts under the new KEK.
    for key, ver in (("alpha", 1), ("alpha", 2), ("beta", 1), ("beta", 2)):
        r = _provision_cli(
            env,
            "get",
            "prod/resume",
            key,
            "--kek-file",
            str(kek_v2),
            "--version",
            str(ver),
        )
        check(
            f"{key} v{ver} decrypts under new KEK after resume",
            r.returncode == 0,
            r.stderr[-200:],
        )


def test_mtls_install_handle(tokens, keks):
    """Item 12a: the app adopts the framework terminator via
    MTLSTerminator.install — the handle is present and its readiness check is
    wired (mTLS is disabled in the plain e2e, so the handle's terminator is
    None and the check reports healthy)."""
    print("\n== mTLS: install() handle + readiness wiring ==")
    from hyperdjango.mtls import InstalledMTLS
    from services.hypersecret.app import _mtls

    check("app holds an InstalledMTLS handle", isinstance(_mtls, InstalledMTLS))
    check("mTLS disabled in plain e2e (terminator None)", _mtls.terminator is None)


# ---------------------------------------------------------------------------


def main() -> bool:
    print(f"HyperSecret E2E — port {PORT}")
    setup_db()
    tokens, keks = load_demo_state()

    with AppRunner(
        "services.hypersecret.app:app",
        host="127.0.0.1",
        port=PORT,
        readiness_path="/ready",
        # Sweep on a sub-second cadence so a backdated soft-delete is purged
        # within a test's lifetime. The sweep only touches rows already past the
        # retention window, so fresh soft-deletes are untouched. The audit sweep
        # runs on the same fast cadence for its retention test.
        env={
            "HYPERSECRET_RETENTION_SWEEP_INTERVAL": "0.5",
            "HYPERSECRET_AUDIT_SWEEP_INTERVAL": "0.5",
        },
    ):
        test_read_path(tokens, keks)
        test_fail_closed(tokens, keks)
        test_crypto_isolation(tokens, keks)
        test_provisioning(tokens, keks)
        test_batch_retained_version(tokens, keks)
        test_client_idempotent_retry(tokens, keks)
        test_scheduler_skip_if_running(tokens, keks)
        test_concurrent_write_conflict(tokens, keks)
        test_metadata_segregation(tokens, keks)
        test_validation_caps(tokens, keks)
        test_strict_int_params(tokens, keks)
        test_tamper_detection(tokens, keks)
        test_delete_lifecycle(tokens, keks)
        test_retention_time_sweep(tokens, keks)
        test_retention_revive_race(tokens, keks)
        test_denial_audit_coverage(tokens, keks)
        test_audit_contract_gaps(tokens, keks)
        test_rewrap_dek_integrity(tokens, keks)
        test_rotation_due_clobber(tokens, keks)
        test_admin_duplicate_conflict(tokens, keks)
        test_unauth_metric_cardinality(tokens, keks)
        test_env_file_hygiene(tokens, keks)
        test_mtls_install_handle(tokens, keks)
        test_identity_lifecycle(tokens, keks)
        test_wildcard_admin_include_deleted(tokens, keks)
        test_audit(tokens, keks)
        test_audit_conflict_durability(tokens, keks)
        test_audit_retention_sweep(tokens, keks)
        test_metrics_auth(tokens, keks)
        test_secrets_run(tokens, keks)
        test_provision_cli(tokens, keks)
        test_provision_rewrap_resume(tokens, keks)
        test_rewrap_covers_soft_deleted(tokens, keks)
        test_provision_help(tokens, keks)
        test_health_and_parity(tokens, keks)

    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    if ERRORS:
        print("Failures:")
        for err in ERRORS:
            print(f"  - {err}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
