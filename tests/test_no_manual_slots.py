"""Validation gate: no manual ``__slots__`` — use ``@dataclass(slots=True)``.

Fails if any class in the product source declares a hand-written
``__slots__ = (...)`` instead of the codebase convention
``@dataclass(slots=True)``, unless it carries a ``# slots-required: <reason>``
prover comment (reserved for a type a dataclass genuinely cannot model, e.g. a
``threading.local`` / other C-extension subclass). See
scripts/check_no_manual_slots.py for the rule. Keeps the legacy manual-slots +
hand-written ``__init__`` slop from drifting back in.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_no_manual_slots as gate  # noqa: E402


def _all_violations():
    out = []
    for root in ("hyperdjango", "services"):
        for f in sorted((_ROOT / root).rglob("*.py")):
            for lineno, text in gate.check_file(f):
                out.append(f"{f.relative_to(_ROOT)}:{lineno}: {text}")
    return out


def test_no_manual_slots():
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} manual __slots__ declaration(s). Use "
        "`@dataclass(slots=True)`, or annotate `# slots-required: <why a dataclass "
        "cannot model this>` for a genuine C-extension/builtin subclass:\n"
        + "\n".join(violations[:50])
        + ("\n…" if len(violations) > 50 else "")
    )
