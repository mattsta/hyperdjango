.PHONY: build release safe test test-safe bench bench-http bench-http-quick bench-all bench-websocket bench-websocket-full release-check run clean check setup validate ci ci-safe docs docs-serve docs-build docs-deploy bootstrap bootstrap-db bootstrap-bench

# Optional flags forwarded to scripts/dev_bootstrap.sh by `make bootstrap`
# (make itself rejects `--flags` typed after a target name).
BOOTSTRAP_FLAGS ?=

# Default: build release + test + validate
all: release test validate

# === Setup ===
setup:
	uv sync --group dev

# Fresh-machine bootstrap (Ubuntu/macOS): uv + free-threaded 3.14t + venv
# self-heal + zig check + native build + import smoke test. Idempotent.
#
# `make` OWNS the words after the target — `make bootstrap --with-postgres`
# dies with "unrecognized option". The optional stages therefore get their own
# targets (and BOOTSTRAP_FLAGS for anything else the script grows).
bootstrap:
	bash scripts/dev_bootstrap.sh $(BOOTSTRAP_FLAGS)

# Bootstrap + PostgreSQL 18 & pgvector install/provisioning (apt/Ubuntu; on
# macOS the install half no-ops and only the role/database provisioning runs).
bootstrap-db:
	bash scripts/dev_bootstrap.sh --with-postgres

# Bootstrap + database + benchmark tooling (comparison deps, wrk, sysctls).
bootstrap-bench:
	bash scripts/dev_bootstrap.sh --with-postgres --bench

# === Build ===
build: setup
	uv run hyper-build

release: setup
	uv run hyper-build --install --release

# ReleaseSafe build: keeps bounds/overflow/UB panics (production speed) — the
# safety gate. Silent ReleaseFast memory corruption becomes a loud panic.
safe: setup
	uv run hyper-build --safe

# === Test ===
test:
	uv run hyper-test

test-core:
	uv run hyper-test rest serializers sql_builder upstream_sync native_results_stress

# Memory-safety gate: build ReleaseSafe, then run the whole suite against it.
# The runner already fails a file on nonzero exit (SIGABRT/panic), so the
# existing suite doubles as a memory-safety check under this build.
test-safe: safe
	uv run hyper-test

test-pytest:
	uv run pytest tests/ -q

# === Validate native stack end-to-end ===
validate:
	uv run python scripts/validate_native.py

# === CI: full pipeline ===
ci: release test validate check

# CI safety pipeline: run the full suite under the ReleaseSafe build so memory
# corruption on any exercised path fails the build instead of passing silently.
ci-safe: test-safe validate check

# === Benchmark ===
bench:
	uv run python benchmarks/bench.py --iterations 100000

bench-quick:
	uv run python benchmarks/bench.py --iterations 10000

# The label BOTH suites feed the unified record under. It is the merge key:
# benchmarks/core's save_run merges a feed into the existing entry carrying the
# same label (same commit, same host, inside the merge window, no suite it
# already holds), so `bench-http` then `bench-websocket` land in ONE canonical
# two-suite record rather than two half-records. Override per box/experiment:
#     make bench-all BENCH_LABEL=epyc-tuned
BENCH_LABEL ?= canonical

# The suites the unified record is SUPPOSED to end up carrying. `bench-all` sets
# it for both halves (target-specific variables propagate to prerequisites), so
# the record DECLARES its intended coverage the moment the first suite is fed —
# and a record still missing the second reads as visibly incomplete on the
# dashboard instead of silently passing as the whole battery. Empty for a
# single-suite run, which then declares only itself.
BENCH_EXPECT ?=
_EXPECT_FLAG = $(if $(BENCH_EXPECT),--expect-suites $(BENCH_EXPECT),)

# One-command HTTP framework comparison suite: machine prep (performance
# governor via sudo -n, fd limits), topology auto-pin (disjoint physical
# cores, NUMA split, SMT idle), then ALL sweeps — worker scaling (c>>W),
# bounded connections (c=W / c=2W, threaded vs reactor), concurrency curve,
# connection scaling — archived as ONE run in benchmarks/http/out/report.html.
bench-http:
	uv run --group benchmark-comparison hyper-bench --mode all --duration 10 --warmup 2 --client wrk --label $(BENCH_LABEL) $(_EXPECT_FLAG)

# Fast smoke of the same suite (small matrix, short windows) — validate a
# box's setup before committing to the ~30-40 min full run.
bench-http-quick:
	uv run --group benchmark-comparison hyper-bench --mode all --quick --duration 3 --warmup 1 --label $(BENCH_LABEL)-quick

# THE CANONICAL-RECORD RECIPE. The full benchmark battery — HTTP comparison
# suite + WebSocket suite — and the ONLY recipe that produces ONE record
# covering BOTH suites: each runner feeds the shared history under the SAME
# $(BENCH_LABEL), and core's merge-into semantics unify them into a single
# entry (each suite keeping its own provenance stamp). The dashboard's Suite
# selector then switches views of that one record.
# Running just one suite archives a single-suite record, which the dashboard
# labels as such ("[websocket only]"); combine after the fact with
#     uv run python -m benchmarks.core.merge <label-or-id> <label-or-id>
# Both halves declare the full intended coverage (BENCH_EXPECT), so the record
# states what it is supposed to contain rather than having coverage inferred
# from whatever happened to land. The WebSocket half runs the FULL matrix: a
# --quick smoke matrix is a diagnostic, and a diagnostic never enters the
# comparison history (so it could never merge into this record).
# Three reports (separate interfaces):
#   benchmarks/out/index.html             — UNIFIED dashboard (every suite, run history)
#   benchmarks/http/out/report.html       — HTTP dashboard (all sweeps + run history)
#   benchmarks/websocket/out/report.html  — WebSocket suite (native vs websockets)
bench-all: BENCH_EXPECT = http,websocket
bench-all: bench-http bench-websocket-full

# Smoke matrix — archived under benchmarks/out/diagnostics/, never a baseline.
bench-websocket:
	uv run --group benchmark-comparison python -m benchmarks.websocket.run --label $(BENCH_LABEL) $(_EXPECT_FLAG)

bench-websocket-full:
	uv run --group benchmark-comparison python -m benchmarks.websocket.run --full --label $(BENCH_LABEL) $(_EXPECT_FLAG)

# === Release gate ===
# Everything that must be green before a public release, in one command:
# optimized build, the FULL test suite (zero test failures AND zero uncounted
# files — every file reports real check tallies), the end-to-end native
# validation, environment diagnosis, and a docs build. Each step fails loud.
release-check: release
	uv run hyper-test
	@echo "-- release-check: verifying zero uncounted test files --"
	@LAST=$$(ls -t logs/test_runs/*_all.log | head -1); \
	if grep -q "Uncounted:" "$$LAST"; then \
	  echo "FAIL: uncounted test files present (see $$LAST)"; exit 1; \
	else echo "OK: every test file reports real counts"; fi
	uv run python scripts/validate_native.py
	uv run hyper doctor
	uv run --with mkdocs-material mkdocs build --strict
	@echo "== release-check: ALL GATES GREEN =="

# === Run ===
run:
	uv run hyper run

# === Check ===
check:
	uv run hyper check

# === Lint ===
# Same command CI's lint job runs; rules live in pyproject.toml [tool.ruff].
lint:
	uv run ruff check

# Auto-fix everything ruff knows how to fix for our rule set. The pathlib
# (PTH) fixes are preview-gated, so they get a second, PTH-scoped pass —
# unscoped --preview would also drag in preview-only rules CI doesn't
# enforce. Fix passes tolerate leftover unfixable violations (leading -);
# the final plain check is the pass/fail verdict, same command CI runs.
# Review the diff afterwards — these fixes are marked "unsafe" by ruff.
lint-fix:
	-uv run ruff check --fix --unsafe-fixes
	-uv run ruff check --fix --unsafe-fixes --preview --select PTH
	uv run ruff check

# === CI (GitHub Actions) ===
.PHONY: ci-latest ci-watch ci-logs ci-errors

ci-latest:
	uv run python scripts/ci_watch.py latest

ci-watch:
	uv run python scripts/ci_watch.py watch

ci-logs:
	uv run python scripts/ci_watch.py logs

ci-errors:
	uv run python scripts/ci_watch.py errors

ci-report:
	uv run python scripts/ci_watch.py report

# === Docs (MkDocs Material) ===
docs: docs-serve

docs-serve:
	uv run --with mkdocs-material mkdocs serve

docs-build:
	uv run --with mkdocs-material mkdocs build

docs-deploy:
	uv run --with mkdocs-material mkdocs gh-deploy --force

# === Clean ===
clean:
	rm -rf hyperdjango/_hyperdjango_native*.so
	rm -rf zig/zig-out zig/.zig-cache zig/zig-cache

# === Help ===
help:
	@echo "HyperDjango Build System"
	@echo "========================"
	@echo "  make           — Build release + test + validate"
	@echo "  make bootstrap — Fresh machine → built native extension (idempotent)"
	@echo "  make bootstrap-db — Bootstrap + PostgreSQL install/provisioning"
	@echo "  make bootstrap-bench — Bootstrap + database + benchmark tooling"
	@echo "  make setup     — Install Python dependencies"
	@echo "  make build     — Build native Zig extension (debug)"
	@echo "  make release   — Build native Zig extension (optimized)"
	@echo "  make test      — Run the full test suite via hyper-test"
	@echo "  make test-core — Run core test suites (REST, serializers, etc.)"
	@echo "  make test-pytest — Run pytest suite (Django integration)"
	@echo "  make validate  — Validate native stack end-to-end"
	@echo "  make bench     — Run performance benchmarks"
	@echo "  make bench-http — Full HTTP comparison suite (all sweeps, auto-pin, one report)"
	@echo "  make bench-http-quick — Fast smoke of the HTTP suite (validate box setup)"
	@echo "  make bench-all — HTTP suite + WebSocket suite (both reports)"
	@echo "  make bench-websocket — Native vs websockets smoke matrix (diagnostic archive)"
	@echo "  make bench-websocket-full — Same, full matrix (enters the comparison history)"
	@echo "  make lint      — Ruff check (same command as CI's lint job)"
	@echo "  make lint-fix  — Auto-apply every available ruff fix, then verify"
	@echo "  make ci        — Full CI pipeline (build + test + validate)"
	@echo "  make release-check — Release gate: build+suite+counts+validate+doctor+docs"
	@echo "  make run       — Run dev server"
	@echo "  make check     — Check feature availability"
	@echo "  make clean     — Remove build artifacts"
	@echo "  make docs-serve — Live-preview docs at http://127.0.0.1:8000"
	@echo "  make docs-build — Build docs site into ./site/"
	@echo "  make docs-deploy — Manually push docs to gh-pages branch"
