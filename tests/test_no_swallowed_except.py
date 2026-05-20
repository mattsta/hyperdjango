"""Validation gate: no bug-hiding exception swallowing in product source.

Fails if any broad exception handler (`except Exception` / `except
BaseException` / bare `except:` / a tuple containing either) in hyperdjango/
neither re-raises on every path nor carries a `# blind-except: <reason>` prover
comment. See scripts/check_swallowed_except.py for the rule. This keeps broad
catch-and-continue slop out of the architecture: swallowing a broad exception
must be genuinely correct AND proven correct at the handler.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_swallowed_except as gate  # noqa: E402


def _all_violations():
    pkg = _ROOT / "hyperdjango"
    out = []
    for f in sorted(pkg.rglob("*.py")):
        for lineno, text in gate.check_file(f):
            out.append(f"{f.relative_to(_ROOT)}:{lineno}: {text}")
    return out


def test_no_swallowed_except():
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} unjustified broad exception handler(s). Make each "
        "re-raise on every path (narrow the catch, or log + `raise`) or annotate "
        "`# blind-except: <why swallowing is correct>`:\n"
        + "\n".join(violations[:50])
        + ("\n…" if len(violations) > 50 else "")
    )
