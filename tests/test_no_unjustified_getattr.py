"""Validation gate: no unjustified getattr/setattr in product source.

Fails if any getattr()/setattr()/__getattr__/__setattr__ call in hyperdjango/
lacks a `# dynamic-attr: <reason>` prover comment. See
scripts/check_dynamic_attr.py for the rule. This keeps the architecture free of
reflection-slop: dynamic attribute access must be genuinely necessary AND
proven necessary at the call site.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_dynamic_attr as gate  # noqa: E402


def _all_violations():
    pkg = _ROOT / "hyperdjango"
    out = []
    for f in sorted(pkg.rglob("*.py")):
        for lineno, text in gate.check_file(f):
            out.append(f"{f.relative_to(_ROOT)}:{lineno}: {text}")
    return out


def test_no_unjustified_dynamic_attr():
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} unjustified getattr/setattr call site(s). Remove "
        "(direct access on the known type) or annotate `# dynamic-attr: <why>`:\n"
        + "\n".join(violations[:50])
        + ("\n…" if len(violations) > 50 else "")
    )
