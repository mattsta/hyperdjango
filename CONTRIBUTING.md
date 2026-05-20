# Contributing to HyperDjango

## Quick start (any fresh Ubuntu or macOS machine)

```bash
git clone https://github.com/anthropics/hyperdjango.git
cd hyperdjango
make bootstrap        # uv → 3.14t → venv → deps → zig check → build → smoke test
```

`make bootstrap` (`scripts/dev_bootstrap.sh`) is idempotent and self-healing:
it installs free-threaded CPython 3.14t via uv, **recreates a `.venv` that was
made with the wrong interpreter**, syncs dependencies, verifies Zig ≥ 0.16 is
on `PATH` (with per-platform install guidance when it isn't), builds and
installs the native extension, and import-verifies the result. The only two
things it asks you to install yourself are `uv` and `zig` — everything else is
managed.

## Prerequisites (what bootstrap checks for)

- **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS: `brew install uv`)
- **Zig 0.16+** — <https://ziglang.org/download/>: extract the tarball and put
  the directory on `PATH`. On macOS `brew install zig` works when the formula
  is current; on Ubuntu use the tarball (distro packages lag). CI pins its
  exact version in `.github/actions/setup-toolchain/action.yml`.
- **Python 3.14t** (free-threaded) — pinned in `.python-version`; uv installs
  it automatically. A standard (GIL-enabled) 3.14 **cannot build or run** the
  native extension — the PyObject ABI differs under `Py_GIL_DISABLED`, and
  `hyper-build` aborts immediately with remediation if it detects one.
- **PostgreSQL** — needed by the test suite only, not the build.
  Ubuntu: `sudo apt install postgresql && sudo -u postgres createuser -s $USER`.
  macOS: `brew install postgresql@17 && brew services start postgresql@17`.
  Then `createdb hyperdjango_test` — and let the doctor verify the rest:

  ```bash
  uv run hyper db doctor    # connectivity → auth → db → privileges → extensions → capacity
  ```

  It probes each link with the framework's own driver and prints the exact
  per-platform fix for the first broken one (bootstrap runs it for you at the
  end). The classic Ubuntu trap it catches: default `pg_hba.conf` uses `peer`
  auth on the unix socket but `scram-sha-256` on TCP, so a passwordless role
  works in `psql` yet fails the driver's localhost connection.

## Day-to-day builds

```bash
make release                      # optimized build + install (= uv run hyper-build --install --release)
make build                        # same, via hyper-build defaults
uv run hyper-build --safe         # ReleaseSafe: keeps bounds/overflow/UB panics (the CI test gate)
```

### Troubleshooting

- **`no field named 'ob_refcnt'` from zig, or `hyper-build` aborts with
  "incompatible Python"** — your venv holds a standard GIL-enabled
  interpreter. `make bootstrap` fixes it (or: `uv python install 3.14t &&
rm -rf .venv && uv sync --group dev`).
- **`ModuleNotFoundError: hyperdjango._hyperdjango_native` while building** —
  can't happen from `hyper-build` (its import spine runs without the
  extension); if you see it elsewhere, the extension isn't built yet: run
  `make release`.
- **`zig: command not found`** — see the Zig prerequisite above.
- **`failed to connect to database: postgres://...`** (from tests or an
  app) — run `uv run hyper db doctor`: it walks connectivity → auth →
  database → privileges in order and prints the exact fix for the first
  broken link.

## Running Tests

```bash
uv run hyper-test                # Full suite (auto-writes logs)
uv run hyper-test rest admin     # Pattern matching
uv run hyper-test --list         # List all available tests
```

The test runner auto-writes a complete transcript to `logs/test_runs/<timestamp>_<slug>.log` and prints the path. **Never pipe or tee output** — just read the log file after.

Per-subprocess failures are logged to `logs/test_runs/subprocess/<name>.log`. Always read these for proof before debugging.

## Running Bundled Services

```bash
uv run hyper setup --app services.bookstore_api.app:app --drop --seed services.bookstore_api.seed:run
uv run hyper start --app services.bookstore_api.app:app --port 18900
```

## Documentation

User-facing docs live in `docs/` and are published to <https://mattsta.github.io/hyperdjango/> via MkDocs Material on every push to `main` (see `.github/workflows/docs.yml`).

```bash
make docs-serve    # Live preview at http://127.0.0.1:8000 (auto-reloads on edit)
make docs-build    # One-off build into ./site/
make docs-deploy   # Manual push to gh-pages branch (CI usually handles this)
```

`mkdocs.yml` controls the navigation tree and theme. Pages not listed in `nav:` are still built but unreachable from the sidebar — when adding a new page, place it under the relevant section in `nav:`.

**First-time setup (repo owner only):** in GitHub repo Settings → Pages, set "Source" to "GitHub Actions". The first push to `main` after that will publish the site.

## Code Style

### Mandatory Rules

- **All imports at top of file** — never import inside functions or methods
- **All classes are dataclasses** — `@dataclass(slots=True)` preferred
- **Proper container types** — `dict[str, int]` not `dict`, `list[str]` not `list`, never `Any` or `object` for typed fields
- **Named type aliases** — `type Name = ...` (PEP 695) for reused types
- **No getattr/hasattr/setattr** on classes you control — access properties directly
- **No defensive programming** — no `isinstance` checks where you know the type, no `or ()` fallbacks where the data is guaranteed

### Framework Conventions

- **TimestampMixin on ALL models** — every Model subclass uses it
- **ORM for ALL seed data** — `Model(...).save()`, never raw SQL INSERT
- **ALL DDL through `hyper setup`** — never hardcode CREATE TABLE. Model classes are the single source of truth
- **Meta.indexes for ALL indexes** — `Index(fields=(...))` on the Model class, never manual CREATE INDEX
- **RBAC via groups** — `@guard(Require.role("admin"))`, never check `user.is_staff` directly
- **SessionUser typed helpers** — `user.in_group("staff")`, `user.has_perm("codename")`, never `user.get("groups")`
- **build_session_data()** at login — derives `is_staff`/`is_superuser` from groups
- **ensure_admin_user()** in seeds — every app with HyperAdmin needs a `hyper_users` admin
- **get_setting()** for config — never `os.environ` in app code
- **TokenEngine** on all SessionAuth — signed session cookies, never bare `sign_data()`

### What NOT to Do

- Never use `pip` — always `uv`
- Never use `uv run python` — use `uv run hyper-build`, `uv run hyper-test`, `uv run hyper`
- Never write to `$TMPDIR` — use `./logs/` for output files
- Never f-string SQL from user input — use ORM or parameterized queries
- Never `except X, Y:` style changes — that syntax is valid Python 3
- Never add `import logging` — use `from hyperdjango.logging import logger`
- Never use `time.sleep` for synchronization — use event-based checks
- Never dismiss test failures as "flaky" — every failure is a real bug

## Source Invariants (enforced gates)

Several rules above are enforced mechanically, not by review. Each has a
checker under `scripts/check_*.py` and a matching test under `tests/`, so
breaking one fails the build immediately instead of five weeks later in
somebody else's pull request. All of them run in `uv run pytest tests/ -q`.

| Gate                         | Rule                                                         | Escape hatch              |
| ---------------------------- | ------------------------------------------------------------ | ------------------------- |
| `check_no_os_environ`        | config comes from `get_setting()`, not ad-hoc env reads      | `# env-boundary: <why>`   |
| `check_no_manual_slots`      | use `@dataclass(slots=True)`, not a hand-written `__slots__` | `# slots-required: <why>` |
| `check_dynamic_attr`         | no `getattr`/`setattr` on types you control                  | `# dynamic-attr: <why>`   |
| `check_error_contract`       | 4xx/5xx bodies are `{"detail", "status"}`                    | `# error-contract: <why>` |
| `check_no_hardcoded_db_user` | no developer's role in a connection default                  | `# db-url-fixture: <why>` |
| `check_timing_assertions`    | no assertion gated by a fixed sleep                          | `# timing-window: <why>`  |
| `check_test_markers`         | every `scripts/test_*.py` declares its kind                  | —                         |

An escape-hatch comment needs a REASON — a bare marker states that a rule was
noticed, not why it does not apply, and the checkers reject it. The marker may
sit anywhere in the comment block directly above the statement, so explain
first and name the exemption last if that reads better.

### The timing gate is the one you are most likely to meet

It rejects a fixed `time.sleep(...)`/`asyncio.sleep(...)` followed by an
assertion. That shape passes on a fast development machine, where the async
work finished during the sleep, and fails on a loaded CI runner where it did
not — a sleep is a guess about how fast the machine is, standing in for a
condition nobody stated.

This is not hypothetical: a single instance of it, replicated across 33 files,
produced weeks of red CI naming a different test each time, while the platform
was correct throughout. Removing all 69 turned CI green in one run.

Write the condition instead:

```python
# NO — passes here, fails on a 2-core runner
queue.enqueue(job)
time.sleep(0.5)
assert processed == 5

# YES — exact on any machine, and stronger (== instead of >=)
queue.enqueue(job)
assert wait_for(lambda: queue.pending == 0 and queue.stats.running == 0)
assert processed == 5
```

If nothing observable exists to wait for, that is usually a missing capability
in the product rather than a reason to sleep: a consumer who wants to know
"has my work drained?" or "is my live feed connected?" deserves an answer too.
Adding it is the preferred fix. Genuinely bounded negatives — "this must NOT
happen within N seconds" — have nothing to wait on and keep their sleep behind
a `# timing-window:` justification; make sure oversleeping cannot flip the
result, because a loaded runner sleeps longer than you asked.

## Pull Request Process

1. Create a branch from main
2. Make your changes following the code style above
3. Run `uv run hyper-test` — full suite must pass with **zero failures**
4. Write tests for new features (test files go in `scripts/test_*.py`)
5. Update documentation if adding user-facing features
6. Submit PR with a clear description of what and why

## Architecture Overview

```
Python API (HyperApp, Model, Request, Response)
    |
_hyperdjango_native.so (Zig compiled)
    |-- HTTP Server: 24-thread pool, radix trie router
    |-- pg.zig: Native PostgreSQL driver, connection pool
    |-- Model Validation: SIMD-parallel (13M models/sec)
    |-- JSON: SIMD parser (6-10x faster than stdlib)
    |-- Template Engine: Compiled Zig templates (36us/render, 1.7x Jinja2)
    |-- WebSocket: RFC 6455 with SIMD XOR unmasking
```

No Python fallbacks — the native extension is required. Build with `uv run hyper-build`.

## Key Directories

| Directory                | Contents                                  |
| ------------------------ | ----------------------------------------- |
| `hyperdjango/`           | Framework source                          |
| `hyperdjango/admin/`     | HyperAdmin auto-CRUD panel                |
| `hyperdjango/auth/`      | RBAC, sessions, passwords, OAuth2         |
| `hyperdjango/guard/`     | Route guard system (Require.role, etc.)   |
| `hyperdjango/telemetry/` | Metrics, tracing, sinks                   |
| `hyperdjango/doctor/`    | `hyper doctor` diagnostic checks          |
| `zig/src/`               | Native Zig extension source               |
| `services/`              | 22 production-ready services              |
| `scripts/`               | Test scripts, benchmarks, profiling tools |
| `docs/`                  | User documentation (mkdocs)               |
| `tests/`                 | pytest suite (Django integration)         |

## Performance Work

All performance changes require the profile-driven process:

1. `uv run hyper-build --release` (optimized build)
2. Baseline wrk/cProfile run
3. Identify hotspot in cProfile top-15
4. Fix
5. Re-profile to verify improvement

Never optimize without a profile showing the hotspot. See `docs/profiling.md`.
