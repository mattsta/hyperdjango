"""In-memory catch-up buffer for the default (non-ledger) delivery tiers.

The default hub is a live pub/sub notifier: a publish assigns an in-memory
monotonic sequence and pushes the event to connected subscribers. This buffer
is the whole persistence story for that path — a bounded ring of the most
recent events keyed by their sequence — so a subscriber that briefly drops off
can reconnect and replay exactly what it missed instead of resyncing wholesale.

The sequence is assigned under a lock, which is what makes it naturally
gap-free and totally ordered with none of the Postgres ceiling / xid /
advisory-lock machinery the ledger tier needs: no two publishes can be assigned
the same seq, and a committed seq can never have a lower sibling still about to
appear. It is process-local and resets to 0 on restart.

A restart also mints a fresh ``epoch`` (a random per-process token). The seq
alone cannot make a restart safe: a new incarnation starts at seq 0 but can burst
past a long-absent subscriber's stale ``last_seq`` before it reconnects, at which
point the raw ``floor <= last_seq <= seq`` window passes and the resume would
replay the NEW incarnation's unrelated events as this client's "catch-up" —
silently skipping what it actually missed. The subscriber therefore echoes the
``epoch`` it last saw, and a resume is recoverable only when that epoch matches
the current one AND the seq window holds; a mismatched (dead-incarnation) epoch
is always unrecoverable, so a restart correctly resyncs every client whose seq
space belonged to the previous incarnation.

Bounded and best-effort by design: once an event ages out of the ring (a
subscriber that fell too far behind) or the process restarts, the subscriber is
told to full-resync rather than served a gap. The heavier at-least-once ordered
replay lives in the opt-in ledger tier.
"""

import secrets
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass

from .subjects import subject_matches


@dataclass(slots=True, frozen=True)
class BufferedEvent:
    """One retained change record: its in-memory seq plus the nudge fields the
    hub broadcasts in an ``event`` frame (never a secret payload)."""

    seq: int
    subject: str
    kind: str
    metadata: dict


@dataclass(slots=True, eq=False)
class _Resync:
    """Sentinel type for :data:`RESYNC` (a distinct type so it is unmistakable
    for an empty replay list)."""

    def __repr__(self) -> str:
        return "RESYNC"


# Returned by ``since`` when the requested resume point is unrecoverable — the
# subscriber fell behind the retained ring, has no prior state, or its epoch names
# a previous (restarted) incarnation whose seq space is unrelated to this one. The
# caller must full-resync, not replay.
RESYNC = _Resync()


class CatchupBuffer:
    """Process-global monotonic seq + bounded event ring + producer dedupe.

    Free-threading-safe: the seq bump, ring append/evict, floor update, and the
    recent-key dedupe map are all mutated under one lock, so a publish assigns a
    gap-free ordered seq and a reconnect query sees a consistent snapshot.
    """

    def __init__(self, *, ring_size: int, dedupe_capacity: int = 4096):
        # Process-incarnation token, minted once per buffer (i.e. once per
        # process start). A reconnecting subscriber echoes the epoch it last saw;
        # a mismatch means its seq space belongs to a DEAD incarnation whose seq
        # reset to 0, so its last_seq is meaningless here and the only safe answer
        # is a full resync — even when this incarnation's seq has already climbed
        # back into the stale last_seq's numeric range. token_hex is computed once
        # at construction and never touches import-time state.
        self.epoch = secrets.token_hex(8)
        self._ring_size = max(0, int(ring_size))
        # maxlen=0 is a ring that drops every append — the pure-ephemeral tier
        # keeps only the seq counter, no retained events.
        self._ring: deque[BufferedEvent] = deque(maxlen=self._ring_size)
        # Highest seq that has aged OUT of the ring: the retained window is
        # (floor, seq]. A resume point below the floor missed evicted events.
        self._floor = 0
        self._seq = 0
        # Best-effort per-producer idempotency: a bounded LRU of
        # (producer, dedupe_key) -> seq. A re-publish of a key still in the map
        # returns the existing seq without appending a second event. Once the
        # key is evicted (or the process restarts) a later re-publish MAY append
        # again — the in-memory tiers make no durable idempotency promise; only
        # the ledger tier's unique constraint does.
        self._dedupe: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._dedupe_capacity = max(0, int(dedupe_capacity))
        self._lock = threading.Lock()

    def append(
        self,
        subject: str,
        kind: str,
        metadata: dict,
        *,
        producer: str = "",
        dedupe_key: str | None = None,
    ) -> tuple[int, bool]:
        """Assign the next seq and append the event to the ring.

        Returns ``(seq, created)``. ``created`` is False when ``dedupe_key``
        matched a recent publish by the same producer — the existing seq is
        returned and nothing is appended or broadcast (best-effort idempotency).
        """
        with self._lock:
            if dedupe_key is not None:
                key = (producer, dedupe_key)
                existing = self._dedupe.get(key)
                if existing is not None:
                    self._dedupe.move_to_end(key)
                    return existing, False
            self._seq += 1
            seq = self._seq
            if self._ring_size and len(self._ring) == self._ring_size:
                # The ring is full: the leftmost event is about to be evicted by
                # this append, so its seq becomes the new floor.
                self._floor = self._ring[0].seq
            self._ring.append(BufferedEvent(seq, subject, kind, metadata))
            if dedupe_key is not None and self._dedupe_capacity:
                self._dedupe[(producer, dedupe_key)] = seq
                if len(self._dedupe) > self._dedupe_capacity:
                    self._dedupe.popitem(last=False)
            return seq, True

    def _since_locked(
        self, last_seq: int | None, prefixes: tuple[str, ...], epoch: str | None
    ) -> list[BufferedEvent] | _Resync:
        # Caller holds the lock. Recoverable exactly when the client's epoch names
        # THIS incarnation AND floor <= last_seq <= seq: the ring still holds every
        # event after last_seq. A non-None epoch that differs (a previous, dead
        # incarnation) is unrecoverable — its seq space is unrelated to ours, so an
        # in-range last_seq must not be honored. epoch=None means a first connect
        # (no epoch yet) and falls through to the last_seq is None resync below. A
        # last_seq below the floor (fell behind the ring), above the current seq
        # (the process restarted and reset the seq), or None (no prior state) is
        # likewise unrecoverable.
        if epoch is not None and epoch != self.epoch:
            return RESYNC
        if self._ring_size == 0 or last_seq is None:
            return RESYNC
        if last_seq < self._floor or last_seq > self._seq:
            return RESYNC
        return [
            e
            for e in self._ring
            if e.seq > last_seq and any(subject_matches(p, e.subject) for p in prefixes)
        ]

    def since(
        self,
        last_seq: int | None,
        prefixes: tuple[str, ...],
        *,
        epoch: str | None = None,
    ) -> list[BufferedEvent] | _Resync:
        """Events after ``last_seq`` matching ``prefixes``, or :data:`RESYNC``.

        ``epoch`` is the incarnation token the subscriber last saw (None on a
        first connect); a non-None value that differs from this buffer's epoch is
        always :data:`RESYNC`."""
        with self._lock:
            return self._since_locked(last_seq, prefixes, epoch)

    def snapshot(
        self,
        last_seq: int | None,
        prefixes: tuple[str, ...],
        *,
        epoch: str | None = None,
    ) -> tuple[list[BufferedEvent] | _Resync, int]:
        """Atomic ``(since(last_seq, prefixes, epoch), head_seq)`` under one lock.

        The replay list and the head seq must be consistent: a feed replays the
        missed events up to ``head`` then streams events with seq > head. Reading
        the two separately would let an event land between them and fall into
        neither (a gap); taking both under one lock closes that window."""
        with self._lock:
            return self._since_locked(last_seq, prefixes, epoch), self._seq

    def current_seq(self) -> int:
        """The in-memory head seq (advertised in the hello frame)."""
        with self._lock:
            return self._seq
