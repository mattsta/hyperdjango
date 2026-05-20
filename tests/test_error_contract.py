"""Validation gate: every 4xx/5xx error body uses the unified contract.

Fails if any Response.json/JsonResponse call in hyperdjango/ builds a bespoke
4xx/5xx error body instead of the unified {"detail","status"}(+"errors") shape.
See scripts/check_error_contract.py for the rule. Prevents the error-shape drift
(a {"error":...} key / a missing status field) this codebase kept re-introducing.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_error_contract as gate  # noqa: E402


def _all_violations():
    pkg = _ROOT / "hyperdjango"
    out = []
    for f in sorted(pkg.rglob("*.py")):
        for lineno, text in gate.check_file(f):
            out.append(f"{f.relative_to(_ROOT)}:{lineno}: {text}")
    return out


def test_no_bespoke_error_bodies():
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} bespoke 4xx/5xx error body/ies. Build via "
        "exception_to_response(HTTPException(...)) / emit {'detail','status'}, or "
        "annotate `# error-contract: <why>`:\n" + "\n".join(violations[:50])
    )
