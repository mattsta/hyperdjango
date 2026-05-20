#!/usr/bin/env python
"""Enforcement gate: CI workflows must not set deployment-tuning or database-
selection env vars at workflow or job level.

The test runner owns each test subprocess's environment and asserts framework
DEFAULTS (default pool size, "no database configured" behavior, ...). A workflow
that exports a deployment-tuning or database-selection variable at
workflow-level or job-level `env:` leaks it into EVERY step of that job —
including test steps that bypass the runner's scrubbing (a bare `pytest ...`,
`hyper doctor`, ...) — which silently invalidates those "assert the default"
tests. Such variables belong on the individual setup steps that genuinely need
them (a `psql` step naming a database), as STEP-level `env:`, which this gate
does not touch.

FAILS (exit 1) if any of the forbidden variables appears as a key in a
workflow-level or job-level `env:` mapping under `.github/workflows/*.yml`.
Step-level `env:` (inside a job's `steps:`) is out of scope by design. The YAML
is parsed structurally (PyYAML), so a variable named inside a shell `run:`
command — e.g. `DATABASE_URL=... uv run ...` — is never mistaken for an `env:`
declaration.

Escape hatch — annotate the offending line to allow a specific variable:

    PGDATABASE: hyperdjango_test  # ci-env-contract: allow PGDATABASE

The allow token must name the exact variable on that line; a bare marker or a
mismatched name does not count.

Run: uv run python scripts/check_ci_env_contract.py [paths...]
Default path: .github/workflows/
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

# Deployment-tuning + database-selection variables. Set job-wide, each one
# either retunes a subsystem away from its default (pool / thread-pool sizing)
# or points every test at a specific database — both defeat tests that assert
# the framework's out-of-the-box behavior.
FORBIDDEN = frozenset(
    {
        "HYPER_POOL_SIZE",
        "HYPER_THREAD_POOL_SIZE",
        "PGDATABASE",
        "DATABASE_URL",
        "HYPER_DATABASE_URL",
    }
)

_ALLOW = re.compile(r"#\s*ci-env-contract:\s*allow\s+(\S+)")


def _mapping_get(node: yaml.Node | None, key: str) -> yaml.Node | None:
    """Return the value node for `key` in a YAML MappingNode, else None."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node
    return None


def _scan_env(
    env_node: yaml.Node | None,
    scope: str,
    lines: list[str],
) -> list[tuple[int, str]]:
    """Return violations for one `env:` mapping (a forbidden key with no allow)."""
    if not isinstance(env_node, yaml.MappingNode):
        return []
    out: list[tuple[int, str]] = []
    for key_node, _value_node in env_node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        var = key_node.value
        if var not in FORBIDDEN:
            continue
        lineno = key_node.start_mark.line + 1
        line = lines[lineno - 1] if 0 <= lineno - 1 < len(lines) else ""
        allow = _ALLOW.search(line)
        if allow and allow.group(1) == var:
            continue
        out.append(
            (
                lineno,
                f"{scope} env sets '{var}' — deployment-tuning/database-selection "
                f"vars leak into every step; move it to the step that needs it or "
                f"annotate '# ci-env-contract: allow {var}'",
            )
        )
    return out


def check_file(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return [(lineno, message)] for forbidden workflow/job-level env keys."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return []
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return []
    if not isinstance(root, yaml.MappingNode):
        return []

    lines = text.splitlines()
    violations: list[tuple[int, str]] = []

    # Workflow-level env.
    violations.extend(_scan_env(_mapping_get(root, "env"), "workflow-level", lines))

    # Job-level env — one `env:` per job mapping. `steps:` (step-level env) is
    # deliberately NOT descended into.
    jobs = _mapping_get(root, "jobs")
    if isinstance(jobs, yaml.MappingNode):
        for job_key, job_val in jobs.value:
            if not isinstance(job_val, yaml.MappingNode) or not isinstance(
                job_key, yaml.ScalarNode
            ):
                continue
            violations.extend(
                _scan_env(_mapping_get(job_val, "env"), f"job '{job_key.value}'", lines)
            )

    return sorted(violations)


def _workflow_files(roots: list[pathlib.Path]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.glob("*.yml")))
            files.extend(sorted(root.glob("*.yaml")))
        elif root.suffix in (".yml", ".yaml"):
            files.append(root)
    return files


def main(argv: list[str]) -> int:
    roots = [pathlib.Path(p) for p in argv[1:]] or [pathlib.Path(".github/workflows")]
    files = _workflow_files(roots)

    total = 0
    for f in files:
        for lineno, message in check_file(f):
            print(f"{f}:{lineno} — {message}")
            total += 1

    if total:
        print(
            f"\nFAILED: {total} forbidden workflow/job-level env declaration(s).\n"
            "Deployment-tuning and database-selection vars must not be set at\n"
            "workflow or job level — put them on the individual step that needs\n"
            "them (step-level `env:`), or annotate the line with\n"
            "`# ci-env-contract: allow <VAR>`.",
            file=sys.stderr,
        )
        return 1
    print(f"OK: no forbidden workflow/job-level env vars in {len(files)} workflow(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
