"""Validation gate: the settings system is the SINGLE configuration authority.

Fails if any module in hyperdjango/ reads the process environment ad-hoc
(``os.environ`` / ``os.getenv``) instead of going through ``get_setting(...)`` /
``resolve_database_url()``. See scripts/check_no_os_environ.py for the rule and
the sanctioned-boundary allowlist. This keeps configuration on ONE path — a
value set in Django settings / DEFAULTS can never be silently ignored because a
module still peeks at the raw env.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_no_os_environ as gate  # noqa: E402


def _all_violations():
    pkg = _ROOT / "hyperdjango"
    out = []
    for f in sorted(pkg.rglob("*.py")):
        if gate._is_allowlisted(f):
            continue
        for lineno, text in gate.check_file(f):
            out.append(f"{f.relative_to(_ROOT)}:{lineno}: {text}")
    return out


def test_no_adhoc_env_reads():
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} ad-hoc os.environ/os.getenv read(s). Read config via "
        "get_setting('<NAME>') / resolve_database_url(), or annotate a genuine "
        "non-setting env read `# env-boundary: <why>`:\n"
        + "\n".join(violations[:50])
        + ("\n…" if len(violations) > 50 else "")
    )
