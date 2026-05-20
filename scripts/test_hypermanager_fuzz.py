"""
Property-based fuzz: HyperManager subject grammar + mTLS head rewriting.

# hyper-test: unit

Properties proven:

  1. Subject/prefix grammar: generated valid subjects pass, arbitrary text
     is accepted iff it matches the grammar — never crashes.
  2. Prefix-match soundness (the authorization property): if a requested
     prefix P is covered by a grant G, then EVERY subject matching P also
     matches G — narrowing a subscription can never widen visibility.
  3. Prefix matching never crosses segment boundaries ("secrets/pro" must
     not match "secrets/production/x").
  4. rewrite_request_head: every inbound x-hyper-mtls-* / x-real-ip /
     x-forwarded-for header is stripped — a client can never smuggle an
     attested identity past the terminator — and the injected attestation
     appears exactly once in a head ending in CRLFCRLF.
  5. rewrite_request_head emits exactly one Connection header, chosen by a
     lexical header-name check: Connection: close for an ordinary request,
     Connection: Upgrade when the head carries an Upgrade line (WebSocket).
     The client's inbound Connection header never survives. The terminator
     makes no other framing decision — native owns Content-Length/chunking.
"""

import string
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hyperdjango.mtls import rewrite_request_head  # noqa: E402
from services.hypermanager.catchup import RESYNC, CatchupBuffer  # noqa: E402
from services.hypermanager.models import (  # noqa: E402
    MAX_SUBJECT_LEN,
    prefix_covered,
    subject_matches,
    valid_identity_name,
    valid_prefix,
    valid_subject,
)

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


segment = st.from_regex(r"[a-z0-9][a-z0-9_.-]{0,63}", fullmatch=True)
subject_s = st.builds("/".join, st.lists(segment, min_size=1, max_size=4))
INJECTED = b"x-hyper-mtls-attest: sekrit\r\nx-hyper-mtls-cn: service:x\r\n"


# -- 1..3: grammar + authorization soundness ---------------------------------


@settings(max_examples=100, deadline=None)
@given(subject_s)
def prop_valid_subjects_accepted(subject):
    """Within the length cap a grammar-valid subject is accepted; over the cap it
    is rejected on length alone (the long case is asserted, never skipped)."""
    if len(subject) <= MAX_SUBJECT_LEN:
        assert valid_subject(subject)
    else:
        assert not valid_subject(subject)
    # Always exercise the over-length rejection: repeat a valid segment past the
    # cap so only the length check — not the grammar — can reject it.
    seg = subject.split("/", 1)[0]
    long_subject = "/".join([seg] * (MAX_SUBJECT_LEN // (len(seg) + 1) + 3))
    assert len(long_subject) > MAX_SUBJECT_LEN
    assert not valid_subject(long_subject)


@settings(max_examples=100, deadline=None)
@given(st.text(alphabet=string.printable, max_size=64))
def prop_grammar_never_crashes(text):
    valid_subject(text)
    valid_prefix(text)
    subject_matches(text, text)


@settings(max_examples=100, deadline=None)
@given(subject_s, st.lists(segment, min_size=1, max_size=3))
def prop_prefix_match_soundness(grant_root, extra):
    """Narrowing a subscription can never widen visibility: if a requested
    prefix is covered by a grant, every subject under the request also
    matches the grant."""
    grant = grant_root + "/"
    requested = grant_root + "/" + "/".join(extra)
    subject = requested + "/leaf_key"
    # The request is strictly under the grant subtree.
    assert subject_matches(grant, requested)
    # Therefore anything matching the request also matches the grant.
    assert subject_matches(requested, subject)
    assert subject_matches(grant, subject)


@settings(max_examples=200, deadline=None)
@given(
    st.lists(segment, min_size=1, max_size=3),
    st.lists(segment, min_size=1, max_size=2),
    st.booleans(),
    st.booleans(),
)
def prop_prefix_covered_honors_trailing_slash(root_segs, extra, grant_slash, req_slash):
    """``prefix_covered`` is sound against ``subject_matches`` AND honors the
    trailing-slash distinction (the authorization-widening the audit found).

    Every example asserts three concrete facts, so the property is never
    vacuous:

      1. A request strictly DEEPER than a grant is always covered, and every
         subject under that request also matches the grant (soundness — a
         narrowed subscription can never widen visibility).
      2. A subtree-only grant "r/" NEVER covers the exact-node request "r":
         the exact node is matched by the request but excluded by the grant, so
         admitting it would be a one-subject authorization leak.
      3. An exact-prefix grant "r" DOES cover the exact-node request "r".
    """
    grant_root = "/".join(root_segs)

    # (1) deeper request is covered; soundness holds on a concrete leaf subject.
    grant = grant_root + ("/" if grant_slash else "")
    requested_root = grant_root + "/" + "/".join(extra)
    requested = requested_root + ("/" if req_slash else "")
    subject = requested_root + "/leaf_key"
    assert prefix_covered(grant, requested)
    assert subject_matches(requested, subject)
    assert subject_matches(grant, subject)

    # (2) subtree-only grant excludes the exact node it is rooted at.
    subtree_grant = grant_root + "/"
    exact_request = grant_root  # no trailing slash → includes the exact node
    assert not prefix_covered(subtree_grant, exact_request)
    assert subject_matches(exact_request, grant_root)
    assert not subject_matches(subtree_grant, grant_root)

    # (3) an exact-prefix grant covers the exact-node request.
    assert prefix_covered(grant_root, exact_request)


@settings(max_examples=100, deadline=None)
@given(segment)
def prop_no_partial_segment_match(a):
    """A prefix ending mid-segment must never match a longer segment that merely
    starts with it: 'secrets/pro' must not cover 'secrets/production/x'. The
    adversarial sibling is derived from `a` itself, so every example exercises
    the negative branch rather than only the random pairs that happen to overlap.
    """
    prefix = f"secrets/{a}"
    # Same first segment: the prefix covers the exact subject and its subtree.
    assert subject_matches(prefix, f"secrets/{a}")
    assert subject_matches(prefix, f"secrets/{a}/x")
    # A strictly longer segment that starts with `a` is a DIFFERENT segment and
    # must not be covered — the partial-segment-match trap.
    assert not subject_matches(prefix, f"secrets/{a}x/leaf")


# -- identity-name grammar (finding 9) ----------------------------------------

# Valid names: the conventional "namespace:name" shape from the same restricted
# charset as subjects. Two segments joined by ':' keeps every generated example
# in-grammar (start/end alphanumeric, 3..128 chars).
_name_seg = st.from_regex(r"[a-z0-9][a-z0-9_.-]{0,20}[a-z0-9]", fullmatch=True)
identity_name = st.builds(lambda a, b: f"{a}:{b}", _name_seg, _name_seg)


@settings(max_examples=200, deadline=None)
@given(identity_name)
def prop_valid_identity_names_accepted(name):
    """A grammar-valid identity name (namespace:name, lowercase, subject charset)
    within the length cap is accepted."""
    if 3 <= len(name) <= 128:
        assert valid_identity_name(name)
    else:
        assert not valid_identity_name(name)


@settings(max_examples=200, deadline=None)
@given(
    st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",), min_codepoint=1, max_codepoint=0x2FFF
        ),
        min_size=3,
        max_size=40,
    )
)
def prop_identity_names_reject_control_and_unicode(text):
    """Names are grammar-checked, not merely length-checked: any name carrying a
    control char, whitespace, uppercase, or non-ASCII/unicode code point is
    rejected — the name flows into mTLS CN matching, audit rows, and the admin UI.
    """
    has_illegal = any(
        (not c.islower() and not c.isdigit() and c not in ":_.-") or ord(c) > 127
        for c in text
    )
    if has_illegal:
        assert not valid_identity_name(text), repr(text)
    # A NUL, a space, and an uppercase variant are always rejected regardless of
    # the random body — never a vacuous pass.
    assert not valid_identity_name("svc:\x00probe")
    assert not valid_identity_name("svc: probe")
    assert not valid_identity_name("Svc:Probe")


# -- in-memory catch-up buffer (default-tier seq/ring/floor/resume) ------------


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=1, max_value=32), st.integers(min_value=1, max_value=256))
def prop_catchup_seq_ring_resume(ring_size, n_events):
    """The seq is monotonic and gap-free from 1; the ring retains the last
    ``ring_size`` events; and a resume replays exactly the retained suffix after
    ``last_seq`` while a resume below the evicted floor (or above the head, or
    with no prior state) resyncs — the whole reconnect contract of the default
    tiers, proven directly against the buffer."""
    buf = CatchupBuffer(ring_size=ring_size)
    prefixes = ("a/",)
    seqs = []
    for i in range(n_events):
        seq, created = buf.append(f"a/x_{i}", "created", {"i": i})
        assert created is True
        seqs.append(seq)
    # Monotonic, gap-free, starting at 1 — the lock-assigned ordering property.
    assert seqs == list(range(1, n_events + 1))
    head = buf.current_seq()
    assert head == n_events

    # A resume exactly at the head is caught up: an empty replay, never a resync.
    assert buf.since(head, prefixes) == []

    retained = min(ring_size, n_events)
    floor = head - retained  # highest evicted seq (0 when nothing was evicted)

    # At the floor the retained window is fully recoverable: replay is exactly
    # the contiguous suffix (floor, head].
    within = buf.since(floor, prefixes)
    assert within is not RESYNC
    assert [e.seq for e in within] == list(range(floor + 1, head + 1))

    # Below the floor (only possible once the ring has evicted) is unrecoverable.
    if floor > 0:
        assert buf.since(floor - 1, prefixes) is RESYNC

    # A last_seq above the head (a process restart that reset the seq) and a
    # subscriber with no prior state both resync.
    assert buf.since(head + 1, prefixes) is RESYNC
    assert buf.since(None, prefixes) is RESYNC

    # A prefix the events don't match yields an empty (recoverable) replay, not
    # a resync — the resume point is valid, nothing simply matched.
    assert buf.since(floor, ("b/",)) == []


@settings(max_examples=300, deadline=None)
@given(
    st.integers(min_value=1, max_value=32),
    st.integers(min_value=1, max_value=64),
    st.integers(min_value=0, max_value=128),
)
def prop_catchup_epoch_gates_resume(ring_size, n_events, last_seq):
    """A resume is honored ONLY under the buffer's own incarnation epoch.

    A client echoing a DIFFERENT (dead-incarnation) epoch always resyncs — even
    when its ``last_seq`` sits numerically inside the live ``(floor, head]``
    window, the exact restart-burst misattribution the audit found: a new
    incarnation's seq climbs back into a stale ``last_seq``'s range and the raw
    seq-window check would replay THIS incarnation's unrelated events as the
    client's catch-up. Under the MATCHING epoch the ordinary seq-window contract
    still holds (in-window replays, below-floor/above-head resyncs), and a first
    connect (epoch=None with no prior state) resyncs.
    """
    buf = CatchupBuffer(ring_size=ring_size)
    prefixes = ("a/",)
    for i in range(n_events):
        buf.append(f"a/x_{i}", "created", {"i": i})
    head = buf.current_seq()
    assert head == n_events

    # A foreign epoch is deterministically distinct (perturb one char), so this
    # is never vacuous. It is unrecoverable for EVERY last_seq — including one
    # inside (floor, head] where the seq-window alone would pass.
    foreign = ("1" if buf.epoch[0] == "0" else "0") + buf.epoch[1:]
    assert foreign != buf.epoch
    assert buf.since(last_seq, prefixes, epoch=foreign) is RESYNC
    assert buf.snapshot(last_seq, prefixes, epoch=foreign)[0] is RESYNC

    # Under the buffer's own epoch the ordinary contract governs the resume.
    floor = head - min(ring_size, n_events)  # highest evicted seq (0 if none)
    matching = buf.since(last_seq, prefixes, epoch=buf.epoch)
    if last_seq < floor or last_seq > head:
        assert matching is RESYNC
    else:
        assert matching is not RESYNC
        assert [e.seq for e in matching] == list(range(last_seq + 1, head + 1))

    # A first connect (no epoch, no prior state) resyncs regardless of the buffer.
    assert buf.since(None, prefixes, epoch=None) is RESYNC


def prop_catchup_ephemeral_and_dedupe():
    """A ring_size=0 buffer keeps no events, so every resume resyncs (the
    ephemeral tier), while the seq still advances. Best-effort dedupe collapses a
    producer's repeated key to one seq, but a different producer's identical key
    lands its own event."""
    # Ephemeral: no ring, every resume resyncs, seq still monotonic.
    eph = CatchupBuffer(ring_size=0)
    s1, c1 = eph.append("a/x", "created", {})
    s2, c2 = eph.append("a/y", "created", {})
    assert c1 and c2 and (s1, s2) == (1, 2)
    assert eph.since(0, ("a/",)) is RESYNC
    assert eph.since(s2, ("a/",)) is RESYNC
    assert eph.since(None, ("a/",)) is RESYNC

    # Dedupe: same (producer, key) → same seq, not created, no new event.
    buf = CatchupBuffer(ring_size=16)
    a, ca = buf.append("a/x", "created", {}, producer="p", dedupe_key="k")
    assert ca is True
    b, cb = buf.append("a/y", "created", {}, producer="p", dedupe_key="k")
    assert cb is False and b == a
    assert buf.current_seq() == a  # the second call appended nothing
    # A different producer reusing the same key lands its own event.
    c, cc = buf.append("a/z", "created", {}, producer="q", dedupe_key="k")
    assert cc is True and c != a


# -- 4..5: mTLS head rewriting -------------------------------------------------

header_name = st.from_regex(r"[A-Za-z][A-Za-z0-9-]{0,20}", fullmatch=True)
header_value = st.from_regex(r"[ -~]{0,40}", fullmatch=True).filter(
    lambda v: ":" not in v[:1]
)
spoof_name = st.sampled_from(
    [
        "X-Hyper-MTLS-Attest",
        "x-hyper-mtls-cn",
        "X-Hyper-MTLS-Fingerprint",
        "X-Real-IP",
        "x-forwarded-for",
    ]
)


@settings(max_examples=100, deadline=None)
@given(
    st.lists(st.tuples(header_name, header_value), max_size=8),
    st.lists(st.tuples(spoof_name, header_value), min_size=1, max_size=4),
)
def prop_spoof_headers_always_stripped(benign, spoofed):
    lines = [b"POST /v1/secrets/prod/api/k HTTP/1.1"]
    reserved = {
        "connection",
        "upgrade",
        "x-hyper-mtls-attest",
        "x-hyper-mtls-cn",
        "x-hyper-mtls-fingerprint",
        "x-real-ip",
        "x-forwarded-for",
    }
    benign = [(n, v) for n, v in benign if n.lower() not in reserved]
    for name, value in benign + spoofed:
        lines.append(f"{name}: {value}".encode())
    head = b"\r\n".join(lines)

    # rewrite_request_head(head, injected) -> (bytes, has_upgrade) | None: the
    # rewritten head bytes plus the upgrade flag, or None on refusal (obs-fold).
    # No framing struct — native owns Content-Length/chunking.
    result = rewrite_request_head(head, INJECTED)
    assert result is not None
    out, has_upgrade = result
    assert has_upgrade is False
    assert out.endswith(b"\r\n\r\n")
    # This is not an upgrade head, so the tail is the injected identity block
    # plus a single Connection: close before the CRLFCRLF terminator.
    tail = INJECTED + b"connection: close\r\n\r\n"
    assert out.endswith(tail)
    # The forwarded head is everything the client sent; no reserved identity
    # header survives there (the injected block legitimately contains them).
    forwarded = out[: -len(tail)]
    for line in forwarded.split(b"\r\n"):
        name = line.split(b":", 1)[0].strip().lower()
        assert not name.startswith(b"x-hyper-mtls-")
        assert name not in (b"x-real-ip", b"x-forwarded-for")
    # Injected attestation present exactly once (only in the injected block).
    assert out.lower().count(b"x-hyper-mtls-attest:") == 1
    for name, value in benign:
        assert f"{name}: {value}".encode() in out


def prop_single_connection_header():
    """Exactly one Connection header, chosen lexically: close for an ordinary
    request, Upgrade when an Upgrade line is present — and the client's inbound
    Connection header never survives either way."""
    plain = (
        b"GET /probe HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\nContent-Length: 0"
    )
    result = rewrite_request_head(plain, INJECTED)
    assert result is not None
    out, has_upgrade = result
    assert has_upgrade is False
    low = out.lower()
    assert low.count(b"connection:") == 1
    assert b"connection: close" in low
    assert b"keep-alive" not in low

    for token in (b"websocket", b"WebSocket", b"WEBSOCKET"):
        ws = (
            b"GET /ws/feed HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n"
            b"Upgrade: " + token
        )
        result = rewrite_request_head(ws, INJECTED)
        assert result is not None
        out, has_upgrade = result
        assert has_upgrade is True
        low = out.lower()
        assert low.count(b"connection:") == 1
        assert b"connection: upgrade" in low
        assert b"connection: close" not in low
        # The Upgrade line is preserved so the native server performs the upgrade.
        assert b"upgrade: " + token.lower() in low
        assert b"keep-alive" not in low


PROPS = [
    ("valid subjects accepted", prop_valid_subjects_accepted),
    ("grammar never crashes", prop_grammar_never_crashes),
    ("prefix-match soundness", prop_prefix_match_soundness),
    ("prefix_covered honors trailing slash", prop_prefix_covered_honors_trailing_slash),
    ("no partial-segment match", prop_no_partial_segment_match),
    ("catchup seq/ring/resume contract", prop_catchup_seq_ring_resume),
    ("catchup epoch gates resume (restart resync)", prop_catchup_epoch_gates_resume),
    ("catchup ephemeral + dedupe", prop_catchup_ephemeral_and_dedupe),
    ("valid identity names accepted", prop_valid_identity_names_accepted),
    (
        "identity names reject control/unicode",
        prop_identity_names_reject_control_and_unicode,
    ),
    ("spoof headers always stripped", prop_spoof_headers_always_stripped),
    ("single connection header", prop_single_connection_header),
]


def main() -> bool:
    print("HyperManager grammar + mTLS head-rewrite fuzz")
    for name, prop in PROPS:
        try:
            prop()
            check(name, True)
        except AssertionError as exc:
            check(name, False, str(exc)[:200])
    print(f"\nResults: {PASS}/{PASS + FAIL} passed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
