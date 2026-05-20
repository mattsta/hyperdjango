"""Hardening round 12 — three defensive fixes (H1/H2/H3).

Covers, without touching the DB or network:

  H1  Unicode identity normalization (hyperdjango/auth/user.py)
      NFC and NFD spellings of the same username/email collapse to ONE
      canonical key on the write path (``User(...)`` construction), so the
      unique constraint / lookups cannot be bypassed by code-point variants.
      ASCII is unchanged.

  H2  FileSystemStorage path containment (hyperdjango/storage.py)
      A name containing ``..`` or an absolute path is rejected with
      SuspiciousFileOperation; legitimate subdirectories are preserved.

  H3  Rate-limit backoff cap (hyperdjango/ratelimit_client.py)
      Jitter is applied BEFORE the max_wait cap, so wait_time() is always
      within [0, max_wait] no matter how large the underlying reset value.

Run:  uv run hyper-test hardening_r12
"""

# hyper-test: unit

import tempfile
import time
import unicodedata

from hyperdjango.auth.user import User, normalize_email, normalize_username
from hyperdjango.ratelimit_client import PolicyState, RateLimitState
from hyperdjango.storage import FileSystemStorage, SuspiciousFileOperation

_PASS = 0
_FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ── H1: Unicode normalization ────────────────────────────────────────────────

# "café" composed (NFC: é = U+00E9) vs decomposed (NFD: e + U+0301 combining)
_NFC_NAME = "café"
_NFD_NAME = "café"


def test_h1_username_normalization() -> None:
    print("\n=== H1: username normalization ===")
    check(
        "NFC and NFD inputs differ before normalization",
        _NFC_NAME != _NFD_NAME,
        "test fixture is not exercising distinct code points",
    )
    check(
        "normalize_username folds NFD -> NFC key",
        normalize_username(_NFC_NAME) == normalize_username(_NFD_NAME),
    )
    check(
        "normalized key is NFKC",
        normalize_username(_NFD_NAME) == unicodedata.normalize("NFKC", _NFD_NAME),
    )

    u_nfc = User(username=_NFC_NAME)
    u_nfd = User(username=_NFD_NAME)
    check(
        "User(NFD).username stored as canonical key",
        u_nfc.username == u_nfd.username,
        f"{u_nfc.username!r} != {u_nfd.username!r}",
    )
    check(
        "stored username matches lookup normalization",
        u_nfd.username == normalize_username(_NFD_NAME),
    )


def test_h1_ascii_unchanged() -> None:
    print("\n=== H1: ASCII usernames preserved ===")
    for name in ("admin", "bob_smith", "User123", "a.b-c"):
        check(f"ASCII {name!r} unchanged", User(username=name).username == name)


def test_h1_compatibility_fold() -> None:
    print("\n=== H1: NFKC compatibility fold ===")
    # Fullwidth 'ａｄｍｉｎ' should fold to ASCII 'admin' under NFKC.
    fullwidth = "ａｄｍｉｎ"
    check(
        "fullwidth folds to ascii admin",
        User(username=fullwidth).username == "admin",
        User(username=fullwidth).username,
    )


def test_h1_email_normalization() -> None:
    print("\n=== H1: email normalization (NFC, local part case-preserved) ===")
    email_nfc = f"{_NFC_NAME}@example.com"
    email_nfd = f"{_NFD_NAME}@example.com"
    check(
        "normalize_email folds NFD -> NFC",
        normalize_email(email_nfc) == normalize_email(email_nfd),
    )
    check(
        "User email stored canonical",
        User(username="x", email=email_nfd).email == normalize_email(email_nfc),
    )
    # Local part case is significant per RFC 5321 — must NOT be lowercased.
    mixed = "MixedCase@Example.com"
    check(
        "local-part case preserved (not lowercased)",
        User(username="y", email=mixed).email == unicodedata.normalize("NFC", mixed),
    )


# ── H2: path containment ─────────────────────────────────────────────────────


def test_h2_traversal_rejected() -> None:
    print("\n=== H2: path traversal rejected ===")
    with tempfile.TemporaryDirectory() as root:
        st = FileSystemStorage(location=root, base_url="/media/")
        for evil in (
            "../etc/passwd",
            "../../secret",
            "photos/../../escape",
            "a/../../b",
            "/../escape",  # leading slash stripped, then .. still escapes
        ):
            raised = False
            try:
                st._path(evil)
            except SuspiciousFileOperation:
                raised = True
            check(f"_path rejects {evil!r}", raised)

        # A leading-slash "absolute" name is neutralized to root-relative
        # (contained), never allowed to reference the real filesystem root.
        neutralized = st._path("/etc/passwd")
        check(
            "absolute path neutralized to root-relative (contained)",
            neutralized.startswith(st._path("")),
            neutralized,
        )

        # url() must also refuse to emit a traversal URL.
        raised = False
        try:
            st.url("../escape")
        except SuspiciousFileOperation:
            raised = True
        check("url() rejects traversal", raised)


def test_h2_legit_paths_allowed() -> None:
    print("\n=== H2: legitimate subdirectories preserved ===")
    with tempfile.TemporaryDirectory() as root:
        st = FileSystemStorage(location=root, base_url="/media/")
        root_real = st._path("")  # root itself resolves cleanly
        for good in ("avatar.jpg", "photos/avatar.jpg", "a/b/c/deep.txt"):
            p = st._path(good)
            check(
                f"_path allows {good!r} within root",
                p.startswith(root_real),
                p,
            )
        # Absolute-looking but root-relative name is treated as relative.
        check(
            "leading slash treated as root-relative",
            st._path("/photos/x.jpg") == st._path("photos/x.jpg"),
        )
        # url() emits a normal relative URL for legit names.
        check(
            "url() emits legit relative url",
            st.url("photos/x.jpg") == "/media/photos/x.jpg",
            st.url("photos/x.jpg"),
        )


def test_h2_save_open_roundtrip() -> None:
    print("\n=== H2: save/open honor containment ===")
    import asyncio

    with tempfile.TemporaryDirectory() as root:
        st = FileSystemStorage(location=root, base_url="/media/")

        async def _run() -> None:
            # Normal save + read-back works.
            name = await st.save("docs/hello.txt", b"hi")
            check("save legit returns name", name == "docs/hello.txt", name)
            check("open reads back bytes", await st.open("docs/hello.txt") == b"hi")

            # Traversal save is rejected, nothing written outside root.
            raised = False
            try:
                await st.save("../evil.txt", b"pwn")
            except SuspiciousFileOperation:
                raised = True
            check("save rejects traversal", raised)

        asyncio.run(_run())


# ── H3: backoff cap after jitter ─────────────────────────────────────────────


def test_h3_wait_never_exceeds_max_blocked() -> None:
    print("\n=== H3: blocked_until wait capped after jitter ===")
    max_wait = 10.0
    over = 0
    for _ in range(5000):
        st = RateLimitState(max_wait=max_wait, jitter_factor=0.5)
        # A reset far in the future — pre-cap wait would be huge.
        st.blocked_until = time.monotonic() + 100_000.0
        w = st.wait_time()
        if w > max_wait or w < 0:
            over += 1
    check(
        f"blocked wait in [0, {max_wait}] across 5000 samples",
        over == 0,
        f"{over} violations",
    )


def test_h3_wait_never_exceeds_max_policy() -> None:
    print("\n=== H3: per-policy wait capped after jitter ===")
    max_wait = 5.0
    over = 0
    for _ in range(5000):
        st = RateLimitState(max_wait=max_wait, jitter_factor=0.9)
        st.policies["p"] = PolicyState(
            name="p", remaining=0, reset_at=time.monotonic() + 99_999.0
        )
        w = st.wait_time()
        if w > max_wait or w < 0:
            over += 1
    check(
        f"policy wait in [0, {max_wait}] across 5000 samples",
        over == 0,
        f"{over} violations",
    )


def test_h3_jitter_still_applied_under_cap() -> None:
    print("\n=== H3: jitter still spreads waits below the cap ===")
    # With a small underlying wait well under max_wait, jitter must still vary
    # the result (thundering-herd protection preserved), and stay <= max_wait.
    st = RateLimitState(max_wait=300.0, jitter_factor=0.5)
    base = 2.0
    seen = set()
    all_capped = True
    for _ in range(200):
        st.blocked_until = time.monotonic() + base
        w = st.wait_time()
        seen.add(round(w, 4))
        if w > 300.0 or w < base - 0.5:  # jitter only adds, never subtracts
            all_capped = False
    check("jitter produces varied waits", len(seen) > 5, f"{len(seen)} distinct")
    check("all jittered waits within bounds", all_capped)


def run() -> bool:
    test_h1_username_normalization()
    test_h1_ascii_unchanged()
    test_h1_compatibility_fold()
    test_h1_email_normalization()
    test_h2_traversal_rejected()
    test_h2_legit_paths_allowed()
    test_h2_save_open_roundtrip()
    test_h3_wait_never_exceeds_max_blocked()
    test_h3_wait_never_exceeds_max_policy()
    test_h3_jitter_still_applied_under_cap()
    print(f"\n{'=' * 60}")
    print(f"Results: {_PASS} passed, {_FAIL} failed")
    print(f"{'=' * 60}")
    return _FAIL == 0


if __name__ == "__main__":
    import sys

    sys.exit(0 if run() else 1)
