"""Validation gate: CI workflows leak no deployment-tuning / database-selection
env vars at workflow or job level.

A workflow/job-level ``env:`` entry is visible to every step of the job,
including test steps that bypass the runner's env scrubbing — so a job-level
``PGDATABASE`` / ``DATABASE_URL`` / ``HYPER_POOL_SIZE`` / ``HYPER_THREAD_POOL_SIZE``
/ ``HYPER_DATABASE_URL`` silently invalidates tests that assert the framework's
defaults. This gate keeps those variables off workflow/job level (step-level
``env:`` on the setup step that needs them is fine). See
scripts/check_ci_env_contract.py for the rule and the
``# ci-env-contract: allow <VAR>`` escape hatch.
"""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import check_ci_env_contract as gate  # noqa: E402


def _all_violations():
    out = []
    for f in gate._workflow_files([_ROOT / ".github" / "workflows"]):
        for lineno, message in gate.check_file(f):
            out.append(f"{f.relative_to(_ROOT)}:{lineno}: {message}")
    return out


def test_no_ci_env_leaks():
    violations = _all_violations()
    assert not violations, (
        f"{len(violations)} forbidden workflow/job-level env var(s). Move each to "
        "the step that needs it, or annotate `# ci-env-contract: allow <VAR>`:\n"
        + "\n".join(violations)
    )


def test_job_level_forbidden_var_is_flagged(tmp_path):
    wf = tmp_path / "wf.yml"
    wf.write_text(
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      PGDATABASE: hyperdjango_test\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    violations = gate.check_file(wf)
    assert len(violations) == 1
    assert violations[0][0] == 5  # the PGDATABASE line
    assert "PGDATABASE" in violations[0][1]


def test_workflow_level_forbidden_var_is_flagged(tmp_path):
    wf = tmp_path / "wf.yml"
    wf.write_text(
        "env:\n"
        "  DATABASE_URL: postgres://x/y\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    violations = gate.check_file(wf)
    assert [v[1].split(" env")[0] for v in violations] == ["workflow-level"]
    assert "DATABASE_URL" in violations[0][1]


def test_step_level_env_is_not_flagged(tmp_path):
    # A forbidden var on a STEP's env is allowed — it does not leak job-wide.
    wf = tmp_path / "wf.yml"
    wf.write_text(
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: setup\n"
        "        env:\n"
        "          PGDATABASE: hyperdjango_test\n"
        "        run: psql\n"
    )
    assert gate.check_file(wf) == []


def test_var_inside_run_command_is_not_flagged(tmp_path):
    # `DATABASE_URL=... uv run ...` is a shell assignment inside a run string,
    # not an env: declaration — structural parsing must ignore it.
    wf = tmp_path / "wf.yml"
    wf.write_text(
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: DATABASE_URL=postgres://x/y uv run hyper db extensions ensure\n"
    )
    assert gate.check_file(wf) == []


def test_escape_hatch_allows_named_var(tmp_path):
    wf = tmp_path / "wf.yml"
    wf.write_text(
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      PGDATABASE: hyperdjango_test  # ci-env-contract: allow PGDATABASE\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    assert gate.check_file(wf) == []


def test_escape_hatch_must_name_the_same_var(tmp_path):
    # An allow token for a DIFFERENT variable does not excuse this line.
    wf = tmp_path / "wf.yml"
    wf.write_text(
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      PGDATABASE: hyperdjango_test  # ci-env-contract: allow DATABASE_URL\n"
        "    steps:\n"
        "      - run: echo hi\n"
    )
    assert len(gate.check_file(wf)) == 1
