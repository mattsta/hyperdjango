#!/usr/bin/env bash
# Dev/server bootstrap: from a fresh clone (Ubuntu or macOS) to a built,
# import-verified native extension with a working database — self-managing.
# Idempotent: safe to re-run any time; every step checks before it acts, and
# it SELF-HEALS the classic failure of a .venv created with the wrong
# interpreter (standard GIL-enabled CPython instead of free-threaded 3.14t).
#
#     make bootstrap                    (or: bash scripts/dev_bootstrap.sh)
#     bash scripts/dev_bootstrap.sh --with-postgres   # also apt-install PG18+pgvector (Ubuntu)
#     bash scripts/dev_bootstrap.sh --bench           # also install wrk/numactl for benchmarking
#
# Manages: uv (auto-installed), free-threaded CPython 3.14t, the .venv,
# dependencies, a Zig >= 0.16 toolchain (AUTO-DOWNLOADED into .toolchain/
# when none is found — hyper-build discovers it there with no PATH setup),
# the native build, an import smoke test, and PostgreSQL provisioning (role
# for the login user, hyperdjango_test database, and extensions installed
# into template1 so every per-test database inherits them — a plain role
# cannot CREATE EXTENSION, which otherwise fails every pgvector test).
set -euo pipefail

cd "$(dirname "$0")/.."

# Mirror of .github/actions/setup-toolchain/action.yml — the CI action is the
# authority for the pinned version + SHAs; keep the two in lockstep.
ZIG_VERSION=0.16.0
ZIG_MIN_MINOR=16

WITH_POSTGRES=0
WITH_BENCH=0
for arg in "$@"; do
    case "$arg" in
        --with-postgres) WITH_POSTGRES=1 ;;
        --bench) WITH_BENCH=1 ;;
        *) printf 'Unknown flag: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n== %s\n' "$1"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

step "uv (Python toolchain manager)"
if ! command -v uv >/dev/null 2>&1 && [ -x "$HOME/.local/bin/uv" ]; then
    # Installed but not on this shell's PATH (fresh server, non-login shell).
    export PATH="$HOME/.local/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found — installing (https://astral.sh/uv)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv install failed — install manually and re-run"
fi
uv --version

step "Free-threaded CPython 3.14t (pinned in .python-version)"
uv python install 3.14t

if [ -x .venv/bin/python ]; then
    # A venv created before the 3.14t pin (or by an old uv) keeps a standard
    # GIL-enabled interpreter forever; the native build cannot use it (the
    # PyObject ABI differs under Py_GIL_DISABLED). Recreate rather than warn.
    if ! .venv/bin/python -c 'import sys, sysconfig; sys.exit(0 if sysconfig.get_config_var("Py_GIL_DISABLED") == 1 else 1)'; then
        echo "Existing .venv uses a non-free-threaded interpreter — recreating it."
        rm -rf .venv
    fi
fi

step "Python dependencies (uv sync --group dev)"
uv sync --group dev
uv run python -c 'import sys, sysconfig
assert sysconfig.get_config_var("Py_GIL_DISABLED") == 1, "venv interpreter is not free-threaded"
print(f"venv interpreter OK: {sys.version.split()[0]} (free-threaded)")'

step "Zig toolchain (>= 0.${ZIG_MIN_MINOR})"
# hyper-build resolves Zig itself (HYPER_ZIG -> PATH -> .toolchain/zig*/ ->
# zig-*/ at the repo root -> ~/.zig). This step only needs to guarantee at
# least one of those locations holds a usable toolchain — downloading the
# pinned version into .toolchain/ when none does.
find_zig() {
    if command -v zig >/dev/null 2>&1; then echo "zig"; return; fi
    local cand
    for cand in .toolchain/zig*/zig zig-*/zig "$HOME/.zig/zig"; do
        [ -x "$cand" ] && { echo "$cand"; return; }
    done
    return 1
}
zig_ok() { # $1 = zig binary; version >= 0.MIN
    local minor
    minor="$("$1" version 2>/dev/null | cut -d. -f2)" || return 1
    [ -n "$minor" ] && [ "$minor" -ge "$ZIG_MIN_MINOR" ]
}
ZIG_BIN="$(find_zig || true)"
if [ -z "${ZIG_BIN}" ] || ! zig_ok "$ZIG_BIN"; then
    case "$(uname -sm)" in
        "Linux x86_64")  ZARCH=x86_64-linux;  ZSHA=70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00 ;;
        "Linux aarch64") ZARCH=aarch64-linux; ZSHA=ea4b09bfb22ec6f6c6ceac57ab63efb6b46e17ab08d21f69f3a48b38e1534f17 ;;
        "Darwin arm64")  ZARCH=aarch64-macos; ZSHA=b23d70deaa879b5c2d486ed3316f7eaa53e84acf6fc9cc747de152450d401489 ;;
        *) die "no usable zig found and no pinned download for '$(uname -sm)' — install Zig >= 0.${ZIG_MIN_MINOR} from https://ziglang.org/download/" ;;
    esac
    ZDIR=".toolchain/zig-${ZARCH}-${ZIG_VERSION}"
    echo "No usable zig found — downloading pinned ${ZIG_VERSION} into ${ZDIR}"
    mkdir -p .toolchain
    TARBALL=".toolchain/zig-${ZARCH}-${ZIG_VERSION}.tar.xz"
    curl -fsSL "https://ziglang.org/download/${ZIG_VERSION}/zig-${ZARCH}-${ZIG_VERSION}.tar.xz" -o "$TARBALL"
    echo "${ZSHA}  ${TARBALL}" | shasum -a 256 -c - || die "zig tarball SHA mismatch — refusing to use it"
    mkdir -p "$ZDIR"
    tar -xJf "$TARBALL" -C "$ZDIR" --strip-components=1
    rm -f "$TARBALL"
    ZIG_BIN="$ZDIR/zig"
fi
echo "zig $("$ZIG_BIN" version) OK ($ZIG_BIN)"

step "Native extension (ReleaseFast build + install)"
uv run hyper-build --install --release

step "Import smoke test"
uv run python -c 'from hyperdjango._hyperdjango_native import hello; print(hello())'

if [ "$WITH_POSTGRES" = 1 ]; then
    step "PostgreSQL 18 + pgvector (apt, PGDG repo)"
    if command -v apt-get >/dev/null 2>&1; then
        if ! command -v psql >/dev/null 2>&1 || ! psql --version | grep -qE ' 1[89]| [2-9][0-9]'; then
            sudo apt-get install -y postgresql-common ca-certificates curl
            sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
            sudo apt-get install -y postgresql-18 postgresql-18-pgvector
            sudo systemctl enable --now postgresql
        else
            # PG present — still make sure pgvector is available for it.
            PGMAJ="$(psql --version | grep -oE '[0-9]+' | head -1)"
            sudo apt-get install -y "postgresql-${PGMAJ}-pgvector" || true
        fi
    else
        echo "(--with-postgres is apt/Ubuntu-only; install PostgreSQL 18 + pgvector manually)"
    fi
fi

step "PostgreSQL provisioning (role, database, trusted extensions)"
# Everything here is idempotent and NON-FATAL: a broken database environment
# must not fail the build bootstrap — but when a local PostgreSQL is running
# this makes it fully test-suite-ready:
#   1. a role named after the login user, with CREATEDB (the test runner
#      creates one isolated database per test),
#   2. the hyperdjango_test database owned by that role,
#   3. pgvector marked TRUSTED (upstream pgvector ships `trusted = true`;
#      some packagings omit it) so database OWNERS can CREATE/DROP the
#      extension themselves — `hyper setup` auto-creates required extensions
#      per database, and tests that re-register the extension need to own it.
#      Without this every pgvector test dies with "permission denied to
#      create extension". Fallback when the control file can't be edited:
#      pre-install into template1 (inherited by every created database —
#      works for everything except extension-lifecycle tests).
provision_pg() {
    local me; me="$(id -un)"
    if ! command -v psql >/dev/null 2>&1; then
        echo "(psql not installed — skipping; re-run with --with-postgres on Ubuntu)"
        return 0
    fi
    local SUDO_PG=""
    if command -v sudo >/dev/null 2>&1 && sudo -n -u postgres true 2>/dev/null; then
        SUDO_PG="sudo -u postgres"
    fi
    if [ -z "$SUDO_PG" ] && ! psql -d postgres -c 'SELECT 1' >/dev/null 2>&1; then
        echo "(no superuser access to PostgreSQL — skipping provisioning; run"
        echo " these as a superuser if the suite reports auth/extension errors:"
        echo "   CREATE ROLE \"$me\" LOGIN CREATEDB; CREATE DATABASE hyperdjango_test OWNER \"$me\";"
        echo "   and append 'trusted = true' to vector.control in the PG extension dir.)"
        return 0
    fi
    run_sql() { ${SUDO_PG} psql -v ON_ERROR_STOP=0 -qAt -d "$1" -c "$2" 2>&1 | sed 's/^/   /'; }
    ${SUDO_PG} psql -qAt -d postgres -c "SELECT 1 FROM pg_roles WHERE rolname='${me}'" | grep -q 1 \
        || run_sql postgres "CREATE ROLE \"${me}\" LOGIN CREATEDB"
    run_sql postgres "ALTER ROLE \"${me}\" CREATEDB"
    ${SUDO_PG} psql -qAt -d postgres -c "SELECT 1 FROM pg_database WHERE datname='hyperdjango_test'" | grep -q 1 \
        || run_sql postgres "CREATE DATABASE hyperdjango_test OWNER \"${me}\""

    # Mark pgvector trusted so role-owned databases can manage it themselves.
    local trusted=0 ctl sharedir
    sharedir="$(pg_config --sharedir 2>/dev/null || true)"
    for ctl in ${sharedir:+$sharedir/extension/vector.control} /usr/share/postgresql/*/extension/vector.control; do
        [ -f "$ctl" ] || continue
        if grep -q '^trusted' "$ctl"; then
            trusted=1
        elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
            echo 'trusted = true' | sudo tee -a "$ctl" >/dev/null && trusted=1 \
                && echo "   marked trusted: $ctl"
        fi
    done

    if [ "$trusted" = 1 ]; then
        # Role-owned model: databases create their own extensions (hyper setup
        # does this). Remove any superuser-owned copy a prior bootstrap put in
        # template1 — an inherited postgres-owned extension can't be dropped
        # by the owning role, which breaks extension-lifecycle tests.
        run_sql template1 "DROP EXTENSION IF EXISTS vector"
        run_sql hyperdjango_test "DROP EXTENSION IF EXISTS vector CASCADE" >/dev/null
        psql -v ON_ERROR_STOP=0 -qAt -d hyperdjango_test -c "CREATE EXTENSION IF NOT EXISTS vector" 2>&1 | sed 's/^/   /'
        psql -v ON_ERROR_STOP=0 -qAt -d hyperdjango_test -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" 2>&1 | sed 's/^/   /'
        echo "PostgreSQL provisioned: role=${me} db=hyperdjango_test, pgvector trusted (role-owned extensions)"
    else
        # Fallback: template1 pre-install (inherited by every created DB).
        for db in template1 hyperdjango_test; do
            run_sql "$db" "CREATE EXTENSION IF NOT EXISTS vector"
            run_sql "$db" "CREATE EXTENSION IF NOT EXISTS pg_trgm"
        done
        echo "PostgreSQL provisioned: role=${me} db=hyperdjango_test, extensions pre-installed in template1"
        echo "(pgvector control file not editable — extension-lifecycle tests will skip/fail)"
    fi
}
provision_pg || true

if [ "$WITH_BENCH" = 1 ]; then
    step "Benchmark tooling (comparison deps, wrk, numactl, kernel tunables)"
    # The comparison frameworks (FastAPI/uvicorn, Flask/gunicorn), psutil (RSS
    # sampling), and plotly (the HTML dashboard) live in an optional group. A
    # plain `uv sync --group dev` REMOVES them — so a bench box must sync both
    # groups or the suite silently skips comparison cells and the report
    # render dies at the very end on the missing plotly import.
    uv sync --group dev --group benchmark-comparison
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y wrk numactl linux-tools-common "linux-tools-$(uname -r)" || true
    else
        echo "(install wrk + numactl manually on non-apt systems)"
    fi
    if [ "$(uname -s)" = Linux ] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        # The full tunable set from docs/benchmarks.md. ip_local_reserved_ports
        # is the critical companion to the widened ephemeral range: without it
        # the kernel hands the suite's fixed server ports (18000-19999) out as
        # ephemeral SOURCE ports and test servers randomly fail to bind.
        sudo tee /etc/sysctl.d/99-hyperdjango-bench.conf >/dev/null <<'SYSCTL'
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.ip_local_reserved_ports = 18000-19999
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
fs.file-max = 2097152
SYSCTL
        sudo sysctl --system >/dev/null && echo "kernel tunables applied (incl. reserved test-port range)"
    fi
    echo "Benchmark reminders (see docs/benchmarks.md 'Measurement-validity rules'):"
    echo "  ulimit -n 1048576"
    echo "  echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"
fi

step "Database health check (hyper db doctor)"
# Layered diagnosis with per-failure remediation (connectivity, TCP auth,
# target database, CREATEDB privilege, extensions, capacity). Non-fatal.
if ! uv run hyper db doctor; then
    echo
    echo "(database not ready — fix the first ✗ above, then re-run:"
    echo "     uv run hyper db doctor"
    echo " until it reports OK. The build itself is complete.)"
fi

printf '\nBootstrap complete. Try: uv run hyper-test signing_mixins\n'
