"""Validation gate: every declared setting is consumed (or explicitly waived).

A setting declared in SETTING_DEFINITIONS but read by no ``get_setting("NAME")``
call in hyperdjango/ is dead weight — it looks configurable but does nothing, the
exact "config that lies" class this codebase kept re-introducing.

This gate FAILS if a declared setting has no in-package reader AND is not in the
reviewed WAIVERS set below. WAIVERS documents WHY each currently-unconsumed
setting is acceptable (consumed off the hyperdjango/ path, or reserved) — so the
inert set is explicit and reviewed, and a NEWLY-orphaned setting can't slip in
silently. It also asserts the two setting registries stay congruent.
"""

import pathlib
import re

from hyperdjango.conf import DEFAULTS, SETTING_DEFINITIONS

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Declared-but-not-read-in-hyperdjango/ settings that are nonetheless legitimate.
# Each entry is (name -> reason). Reviewed; a new orphan NOT listed here fails.
WAIVERS = {
    # Consumed by the semantic_search example feature (not framework core).
    "API_KEY": "consumed by the semantic_search example / external-API auth",
    "EMBEDDINGS_API_KEY": "semantic_search example (embedding provider auth)",
    "EMBEDDINGS_API_URL": "semantic_search example (embedding provider URL)",
    "EMBEDDINGS_MODEL": "semantic_search example (embedding model id)",
    "EMBEDDINGS_VECTOR_DIM": "semantic_search example (vector dimension)",
    # Reserved: the native pg pool ABI (_db_configure "si|iiLL") has no parameter
    # for these; honored only if/when the native signature grows one.
    "PREPARED_STATEMENTS": "reserved — native pool ABI does not yet accept it",
    "STATEMENT_CACHE_SIZE": "reserved — native pool ABI does not yet accept it",
    # Reserved: hot reload uses the native kqueue/inotify watcher; no poll mode.
    "HOT_RELOAD_POLL_INTERVAL": "reserved — native watcher has no poll fallback",
    # Reserved: TokenConfig requires explicit SigningKey lists (session cookie
    # signing uses SESSION_SECRET); no default-key injection point.
    "SESSION_SIGNING_KEY": "reserved — TokenConfig takes explicit keys; sessions use SESSION_SECRET",
}


def _readers():
    rx = re.compile(r'get_setting\(\s*["\']([A-Z_][A-Z0-9_]*)["\']')
    out = set()
    for f in (_ROOT / "hyperdjango").rglob("*.py"):
        for m in rx.finditer(f.read_text()):
            out.add(m.group(1))
    return out


def test_registries_congruent():
    assert set(DEFAULTS) == set(SETTING_DEFINITIONS), (
        "DEFAULTS and SETTING_DEFINITIONS drifted: "
        f"only-DEFAULTS={sorted(set(DEFAULTS) - set(SETTING_DEFINITIONS))}, "
        f"only-DEFS={sorted(set(SETTING_DEFINITIONS) - set(DEFAULTS))}"
    )


def test_no_dead_settings():
    readers = _readers()
    inert = {n for n in SETTING_DEFINITIONS if n not in readers}
    unexplained = sorted(inert - set(WAIVERS))
    assert not unexplained, (
        f"{len(unexplained)} declared setting(s) with no get_setting reader and no "
        "waiver — wire a consumer, delete the declaration, or add a reviewed WAIVERS "
        f"entry:\n  " + "\n  ".join(unexplained)
    )


def test_no_stale_waivers():
    # A waiver for a setting that now HAS a reader (or was deleted) is stale.
    readers = _readers()
    stale = sorted(n for n in WAIVERS if n not in SETTING_DEFINITIONS or n in readers)
    assert not stale, f"stale WAIVERS entries (now consumed or removed): {stale}"
