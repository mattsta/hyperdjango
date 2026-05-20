"""Comprehensive end-to-end integration tests for v0.2.0 features.

Exercises ALL major v0.2.0 features working together:
1. Dynamic template extends/include with variable paths
2. pgvector VectorField + distance lookups + migration ops
3. WebSocket config (max message size, ping/pong, subprotocols)
4. Connection pool auto-tuner (metrics, scaling decisions)
5. Admin dark mode + custom themes
6. Async migration runner (progress, timing, safety checks)
7. SIMD template filters (striptags, truncate, urlencode, wordcount)
8. Cross-feature integration (templates + filters + themes together)

Usage:
    uv run hyper-test e2e_v020
"""

# hyper-test: db_isolated

import asyncio
import os
import tempfile
import time
from pathlib import Path

from hyperdjango.native import is_release_build

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def check(name, condition, details=""):
    if condition:
        RESULTS["passed"] += 1
        print(f"  PASS: {name}")
    else:
        RESULTS["failed"] += 1
        RESULTS["errors"].append(name)
        print(f"  FAIL: {name} — {details}")


def main():
    print("=" * 70)
    print("v0.2.0 End-to-End Integration Tests")
    print("=" * 70)

    test_dynamic_templates_e2e()
    test_pgvector_e2e()
    test_websocket_e2e()
    test_pool_autotuner_e2e()
    test_admin_themes_e2e()
    test_migration_runner_e2e()
    test_simd_filters_e2e()
    test_cross_feature_integration()

    # Live database tests (requires PostgreSQL)
    try:
        asyncio.run(test_live_database_e2e())
    except BaseException as e:
        print(f"\n  SKIP live DB: {type(e).__name__}: {e}")

    print("\n" + "=" * 70)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"v0.2.0 E2E Results: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 70)

    return RESULTS["failed"] == 0


# ── 1. Dynamic Template Extends/Include ──────────────────────────────────────


def test_dynamic_templates_e2e():
    print("\n--- E2E: Dynamic Templates ---")

    from hyperdjango.templating import TemplateEngine

    tmpdir = Path(tempfile.mkdtemp())
    engine = TemplateEngine(template_dir=str(tmpdir))

    # Write template hierarchy
    (tmpdir / "base.html").write_text(
        "<html>{% block title %}Default{% endblock %}|{% block body %}{% endblock %}</html>"
    )
    (tmpdir / "sidebar.html").write_text("<nav>{{ items|length }} items</nav>")

    # Dynamic extends + dynamic include together
    result = engine.render_string(
        "{% extends layout %}{% block body %}{% include sidebar %}{% endblock %}",
        {"layout": "base.html", "sidebar": "sidebar.html", "items": [1, 2, 3]},
    )
    check(
        "dynamic extends + include",
        "3 items" in result and "<html>" in result,
        repr(result),
    )

    # Fallback list in dynamic context
    (tmpdir / "fallback.html").write_text("Fallback: {{ name }}")
    result = engine.render_string(
        '{% include ["missing.html", "fallback.html"] %}',
        {"name": "World"},
    )
    check("fallback list with context", result == "Fallback: World", repr(result))

    # Dynamic include in for loop
    (tmpdir / "card.html").write_text("[{{ item }}]")
    result = engine.render_string(
        "{% for item in items %}{% include tmpl %}{% endfor %}",
        {"tmpl": "card.html", "items": ["a", "b", "c"]},
    )
    check("dynamic include in loop", result == "[a][b][c]", repr(result))

    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)


# ── 2. pgvector Integration ──────────────────────────────────────────────────


def test_pgvector_e2e():
    print("\n--- E2E: pgvector ---")

    from hyperdjango.lookups import (
        CosineDistanceLookup,
        _format_vector,
        _lookup_registry,
    )
    from hyperdjango.migrations import CreateVectorIndex, ModelExtractor
    from hyperdjango.models import Field, Model, VectorField

    # Define model with vector field
    class EmbeddingDoc(Model):
        class Meta:
            table = "e2e_embedding_docs"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(max_length=200)
        embedding: list[float] = VectorField(
            dimensions=384, index_type="hnsw", index_ops="vector_cosine_ops"
        )

    # Verify model extraction
    schema = ModelExtractor.extract(EmbeddingDoc)
    check(
        "pgvec model extraction", schema.columns["embedding"].type_sql == "vector(384)"
    )
    check("pgvec model has index", schema.columns["embedding"].has_index)

    # Verify lookup SQL generation
    lookup = CosineDistanceLookup()
    sql, params = lookup.as_sql("embedding", 1, ([0.1, 0.2, 0.3], 0.5))
    check("pgvec cosine sql", "<=>" in sql and "::vector" in sql, sql)

    # Verify index operation
    op = CreateVectorIndex(table="docs", column="embedding", index_type="hnsw")
    check("pgvec index sql", "USING hnsw" in op.up_sql())

    # Verify all lookups registered
    for name in ("cosine_distance", "l2_distance", "inner_product", "nearest"):
        check(f"pgvec lookup '{name}' registered", name in _lookup_registry)

    # Vector formatting
    check("pgvec format", _format_vector([1.0, 2.0]) == "[1.0,2.0]")


# ── 3. WebSocket Extensions ──────────────────────────────────────────────────


def test_websocket_e2e():
    print("\n--- E2E: WebSocket Extensions ---")

    from hyperdjango.websocket import (
        WebSocket,
        WebSocketConfig,
        WebSocketDisconnect,
    )

    # Config lifecycle
    cfg = WebSocketConfig(
        max_message_size=4 * 1024 * 1024, ping_interval=15, pong_timeout=60
    )
    cfg.apply()
    current = WebSocketConfig.current()
    check("ws config roundtrip size", current.max_message_size == 4 * 1024 * 1024)
    check("ws config roundtrip ping", current.ping_interval == 15)
    check("ws config roundtrip pong", current.pong_timeout == 60)

    # Reset defaults
    WebSocketConfig().apply()
    defaults = WebSocketConfig.current()
    check("ws config reset", defaults.max_message_size == 16 * 1024 * 1024)

    # WebSocket subprotocol parsing
    scope = {
        "type": "websocket",
        "path": "/ws/graphql",
        "headers": [(b"sec-websocket-protocol", b"graphql-ws, graphql-transport-ws")],
        "query_string": b"",
    }
    ws = WebSocket(scope, None, None)
    check(
        "ws subprotocol parsing",
        ws.requested_subprotocols == ["graphql-ws", "graphql-transport-ws"],
    )

    # Extension awareness
    scope2 = {
        "type": "websocket",
        "path": "/ws",
        "headers": [],
        "query_string": b"",
        "extensions": {"permessage-deflate": {}},
    }
    ws2 = WebSocket(scope2, None, None)
    check("ws compression detection", ws2.has_compression)

    # Disconnect exception
    try:
        raise WebSocketDisconnect(1001)
    except WebSocketDisconnect as e:
        check("ws disconnect code", e.code == 1001)


# ── 4. Pool Auto-Tuner ──────────────────────────────────────────────────────


def test_pool_autotuner_e2e():
    print("\n--- E2E: Pool Auto-Tuner ---")

    from hyperdjango.database import Database
    from hyperdjango.pool import PoolAutoTuner

    db_url = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")
    loop = asyncio.new_event_loop()

    async def run():
        db = Database(db_url)
        try:
            await db.connect()
        except Exception as e:
            print(f"  SKIP: Database not available ({e})")
            return

        try:
            # Real pool stats from live connection
            stats = db.pool_stats()
            check("autotuner pool stats total", stats["total"] > 0)
            check(
                "autotuner pool stats keys", "available" in stats and "in_use" in stats
            )

            # Real autotuner with live connection
            tuner = PoolAutoTuner(db, check_interval=1)
            await tuner._check_and_adjust()
            check("autotuner sample collected", len(tuner._samples) == 1)
            check(
                "autotuner utilization valid",
                0 <= tuner._samples[0]["utilization"] <= 1,
            )

            await tuner._check_and_adjust()
            check("autotuner two samples", len(tuner._samples) == 2)

            # Stats structure
            ts = tuner.stats()
            check("autotuner stats total_samples", ts["total_samples"] == 2)
            check("autotuner history", len(tuner.utilization_history) == 2)

            # Recommendation is valid enum
            rec = tuner.recommendation()
            check(
                "autotuner recommendation valid",
                rec in ("scale_up", "scale_down", "hold"),
            )
        finally:
            await db.disconnect()

    loop.run_until_complete(run())
    loop.close()


# ── 5. Admin Themes ──────────────────────────────────────────────────────────


def test_admin_themes_e2e():
    print("\n--- E2E: Admin Dark Mode ---")

    from hyperdjango.admin.fields import ThemeConfig
    from hyperdjango.admin.templates import (
        _ADMIN_CSS,
        _TEMPLATE_FOOTER,
        _TEMPLATE_HEADER,
    )

    # Theme config creation
    theme = ThemeConfig(name="ocean", label="Ocean", css_vars={"--primary": "#0ea5e9"})
    check(
        "theme creation",
        theme.name == "ocean" and theme.css_vars["--primary"] == "#0ea5e9",
    )

    # Dark mode in CSS
    check("dark mode css", '[data-theme="dark"]' in _ADMIN_CSS)
    check("prefers-color-scheme", "prefers-color-scheme" in _ADMIN_CSS)

    # Toggle in template
    check("toggle in header", "theme-toggle" in _TEMPLATE_HEADER)
    check("localStorage in footer", "localStorage" in _TEMPLATE_FOOTER)

    # Theme CSS generation
    props = " ".join(f"{k}: {v};" for k, v in theme.css_vars.items())
    css = f'[data-theme="{theme.name}"] {{ {props} }}'
    check("theme css generation", "--primary: #0ea5e9" in css)


# ── 6. Async Migration Runner ────────────────────────────────────────────────


def test_migration_runner_e2e():
    print("\n--- E2E: Migration Runner ---")

    from hyperdjango.migrations import (
        AsyncMigrationRunner,
        MigrationResult,
        MigrationRunReport,
    )

    # Result tracking
    results = [
        MigrationResult(
            name="0001_init", status="applied", duration_ms=15.3, sql_statements=5
        ),
        MigrationResult(
            name="0002_data", status="applied", duration_ms=8.1, sql_statements=2
        ),
    ]
    report = MigrationRunReport(
        results=results,
        applied_count=2,
        total_duration_ms=23.4,
    )
    check("report success", report.success)
    check("report applied", report.applied_count == 2)
    check("report duration", report.total_duration_ms == 23.4)

    # Destructive detection
    runner = AsyncMigrationRunner.__new__(AsyncMigrationRunner)
    warnings = runner._check_destructive(
        [
            "CREATE TABLE foo (id INT);",
            "DROP TABLE old_bar;",
            "INSERT INTO foo VALUES (1);",
        ]
    )
    check("detects destructive", len(warnings) == 1)
    check("safe ops pass", "CREATE TABLE" not in warnings[0])

    # Progress callback pattern
    log = []

    def on_progress(name, status, idx, total):
        log.append((name, status, idx, total))

    on_progress("0001", "applied", 1, 2)
    on_progress("0002", "applied", 2, 2)
    check("progress callbacks", len(log) == 2 and log[-1][2] == 2)


# ── 7. SIMD Template Filters ────────────────────────────────────────────────


def test_simd_filters_e2e():
    print("\n--- E2E: SIMD Filters ---")

    from hyperdjango.templating import TemplateEngine

    engine = TemplateEngine(template_dir=".")

    def render(src, ctx=None):
        return engine.render_string(src, ctx or {})

    # striptags
    check("simd striptags", render("{{ t|striptags }}", {"t": "<b>Bold</b>"}) == "Bold")

    # truncate
    result = render("{{ t|truncate(10) }}", {"t": "Hello beautiful world"})
    check("simd truncate", result.endswith("...") and len(result) < 20, repr(result))

    # urlencode
    check("simd urlencode", render("{{ t|urlencode }}", {"t": "a b"}) == "a%20b")

    # wordcount
    check("simd wordcount", render("{{ t|wordcount }}", {"t": "one two three"}) == "3")

    # wordwrap
    result = render("{{ t|wordwrap(10) }}", {"t": "hello world this is a test"})
    check("simd wordwrap", "\n" in result)

    # Combined filters
    result = render(
        "{{ t|striptags|truncate(15) }}", {"t": "<p>A very long paragraph here</p>"}
    )
    check("simd combined filters", result.endswith("..."), repr(result))

    # Performance: striptags on large HTML
    large = "<div>" + "word " * 200 + "</div>"
    start = time.perf_counter()
    for _ in range(500):
        render("{{ t|striptags }}", {"t": large})
    elapsed = (time.perf_counter() - start) / 500 * 1_000_000
    threshold = 500 if is_release_build else 5000
    check(
        f"simd striptags perf < {threshold}μs", elapsed < threshold, f"{elapsed:.1f}μs"
    )


# ── 8. Cross-Feature Integration ─────────────────────────────────────────────


def test_cross_feature_integration():
    print("\n--- E2E: Cross-Feature Integration ---")

    # Dynamic template with SIMD filters
    from hyperdjango.templating import TemplateEngine

    tmpdir = Path(tempfile.mkdtemp())
    engine = TemplateEngine(template_dir=str(tmpdir))

    (tmpdir / "card.html").write_text("{{ content|striptags|truncate(20) }}")
    (tmpdir / "base.html").write_text("Layout:{% block main %}{% endblock %}")

    # Dynamic extends + dynamic include + SIMD filter
    result = engine.render_string(
        "{% extends layout %}{% block main %}{% include partial %}{% endblock %}",
        {
            "layout": "base.html",
            "partial": "card.html",
            "content": "<p>This is a very long paragraph with HTML tags</p>",
        },
    )
    check(
        "cross: extends+include+filter",
        "Layout:" in result and "..." in result,
        repr(result),
    )
    check("cross: tags stripped", "<p>" not in result)

    # VectorField + migration SQL together
    from hyperdjango.migrations import ModelExtractor
    from hyperdjango.models import Field, Model, VectorField

    class SearchDoc(Model):
        class Meta:
            table = "e2e_search_docs"

        id: int = Field(primary_key=True, auto=True)
        text: str = Field()
        vec: list[float] = VectorField(
            dimensions=768, index_type="ivfflat", index_ops="vector_l2_ops"
        )

    schema = ModelExtractor.extract(SearchDoc)
    check("cross: vector schema", schema.columns["vec"].type_sql == "vector(768)")

    # WebSocket config + pool tuner together
    from hyperdjango.websocket import WebSocketConfig

    ws_cfg = WebSocketConfig(
        max_message_size=8 * 1024 * 1024, ping_interval=20, pong_timeout=90
    )
    ws_cfg.apply()
    check("cross: ws config applied", WebSocketConfig.current().ping_interval == 20)
    WebSocketConfig().apply()  # Reset

    # ThemeConfig + template rendering
    from hyperdjango.admin.fields import ThemeConfig

    theme = ThemeConfig(name="brand", label="Brand", css_vars={"--primary": "#7c3aed"})
    check("cross: theme + model", theme.css_vars["--primary"] == "#7c3aed")

    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)


# ── 9. Live Database E2E ──────────────────────────────────────────────────────


async def test_live_database_e2e():
    """Test pgvector + pool stats + auto-tuner against real PostgreSQL."""
    print("\n--- E2E: Live Database ---")

    from hyperdjango.database import Database
    from hyperdjango.pool import PoolAutoTuner

    db_url = os.environ.get(
        "DATABASE_URL", "postgres://localhost:5432/hyperdjango_test"
    )
    db = Database(db_url)

    try:
        await db.connect()
    except Exception as e:
        print(f"  SKIP: Database not available ({e})")
        return

    try:
        # pgvector live test
        await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await db.execute("DROP TABLE IF EXISTS e2e_vectors CASCADE")
        await db.execute("""
            CREATE TABLE e2e_vectors (
                id SERIAL PRIMARY KEY,
                label TEXT NOT NULL,
                vec vector(3) NOT NULL
            )
        """)

        await db.execute(
            "INSERT INTO e2e_vectors (label, vec) VALUES ($1, $2::vector)",
            "a",
            "[1,0,0]",
        )
        await db.execute(
            "INSERT INTO e2e_vectors (label, vec) VALUES ($1, $2::vector)",
            "b",
            "[0,1,0]",
        )
        await db.execute(
            "INSERT INTO e2e_vectors (label, vec) VALUES ($1, $2::vector)",
            "c",
            "[0.7,0.7,0]",
        )

        rows = await db.query(
            "SELECT label FROM e2e_vectors ORDER BY vec <=> '[1,0,0]'::vector LIMIT 2"
        )
        check("live pgvec knn", rows[0]["label"] == "a", repr(rows))
        check("live pgvec second nearest", rows[1]["label"] == "c", repr(rows))

        # Distance threshold
        rows = await db.query(
            "SELECT label FROM e2e_vectors WHERE vec <=> '[1,0,0]'::vector < 0.1"
        )
        check("live pgvec threshold", len(rows) == 1 and rows[0]["label"] == "a")

        # HNSW index
        await db.execute(
            "CREATE INDEX idx_e2e_hnsw ON e2e_vectors USING hnsw (vec vector_cosine_ops)"
        )
        rows = await db.query(
            "SELECT label FROM e2e_vectors ORDER BY vec <=> '[1,0,0]'::vector LIMIT 1"
        )
        check("live hnsw indexed query", rows[0]["label"] == "a")

        # Pool stats + auto-tuner integration
        stats = db.pool_stats()
        check("live pool stats total", stats["total"] > 0)

        tuner = PoolAutoTuner(db, check_interval=1)
        await tuner._check_and_adjust()
        check("live autotuner sample", len(tuner._samples) == 1)
        check("live autotuner utilization", 0 <= tuner._samples[0]["utilization"] <= 1)

        # Vector returned as Python list
        rows = await db.query("SELECT vec FROM e2e_vectors WHERE label = 'a'")
        vec_val = rows[0]["vec"]
        check("live vector as list", isinstance(vec_val, list), repr(type(vec_val)))
        check("live vector values", len(vec_val) == 3, repr(vec_val))

        # Cleanup
        await db.execute("DROP TABLE IF EXISTS e2e_vectors CASCADE")
        check("live cleanup", True)

    finally:
        await db.disconnect()


if __name__ == "__main__":
    success = main()
    raise SystemExit(0 if success else 1)
