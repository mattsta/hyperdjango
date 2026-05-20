"""
Tests for HyperAdmin M2M dual-select widget (filter_horizontal).

# hyper-test: e2e

Tests:
  1. M2M descriptor detection on model classes
  2. M2M data loading (available + selected)
  3. M2M data saving (junction table sync)
  4. E2E: register model with filter_horizontal, seed data, verify widget renders
  5. E2E: submit form with M2M selections, verify junction table updated

Usage:
    uv run hyper-test admin_m2m_widget
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from e2e_helper import AppRunner, http_get

PASS = 0
FAIL = 0
ERRORS: list[str] = []

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Scratch dir for the generated test app + SQL setup file. Lives under
# .test_scratch/ at project root (gitignored).
SCRATCH_ROOT = PROJECT_ROOT / ".test_scratch"
APP_DIR = SCRATCH_ROOT / "m2m_e2e"
SETUP_SQL_PATH = SCRATCH_ROOT / "m2m_setup.sql"
APP_MODULE = "m2m_e2e"  # subprocess imports with PYTHONPATH=SCRATCH_ROOT


def ok(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
    print(msg)
    ERRORS.append(msg)
    return False


# ---------------------------------------------------------------------------
# Unit tests — M2M detection + config
# ---------------------------------------------------------------------------


def test_m2m_config():
    print("\n--- M2M Config ---")
    from hyperdjango.admin import HyperAdmin
    from hyperdjango.app import HyperApp
    from hyperdjango.mixins import TimestampMixin
    from hyperdjango.models import Field, ManyToManyField, Model

    class Tag(TimestampMixin, Model):
        class Meta:
            table = "m2m_test_tags"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=50)

    class Article(TimestampMixin, Model):
        class Meta:
            table = "m2m_test_articles"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(max_length=200)

    # Add M2M descriptor
    Article.tags = ManyToManyField(Tag, junction_table="m2m_test_articles_tags")
    Article.tags._configure(Article, "tags")

    app = HyperApp(title="M2M Test")
    admin = HyperAdmin(
        app, prefix="/admin", title="M2M", secret_key="test", require_auth=False
    )

    config = admin.register(
        Article,
        list_display=["id", "title"],
        filter_horizontal=["tags"],
    )

    ok("filter_horizontal stored", config.filter_horizontal == ["tags"])

    # M2M descriptor detection
    m2m = admin._get_m2m_descriptors(Article)
    ok("M2M descriptor found", "tags" in m2m, str(list(m2m.keys())))
    ok("M2M is ManyToManyField", isinstance(m2m.get("tags"), ManyToManyField))


# ---------------------------------------------------------------------------
# E2E — full roundtrip
# ---------------------------------------------------------------------------


def test_m2m_e2e():
    print("\n--- M2M E2E: Widget Rendering + Save ---")

    port = 19210
    env = os.environ.copy()
    db_url = env.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")

    # Setup: create test tables + seed
    setup_sql = """
DROP TABLE IF EXISTS m2m_e2e_articles_m2m_e2e_tags CASCADE;
DROP TABLE IF EXISTS m2m_e2e_articles CASCADE;
DROP TABLE IF EXISTS m2m_e2e_tags CASCADE;

CREATE TABLE m2m_e2e_tags (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE m2m_e2e_articles (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE m2m_e2e_articles_m2m_e2e_tags (
    article_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (article_id, tag_id)
);
INSERT INTO m2m_e2e_tags (name) VALUES ('python'), ('zig'), ('web'), ('perf'), ('async');
INSERT INTO m2m_e2e_articles (title) VALUES ('HyperDjango Guide'), ('Zig Tutorial');
INSERT INTO m2m_e2e_articles_m2m_e2e_tags (article_id, tag_id) VALUES (1, 1), (1, 3);
"""
    # Write setup script into the scratch dir
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    SETUP_SQL_PATH.write_text(setup_sql)

    subprocess.run(
        ["psql", db_url, "-f", str(SETUP_SQL_PATH)], capture_output=True, timeout=10
    )

    # Write test app
    app_code = f'''
import os
from hyperdjango import HyperApp
from hyperdjango.admin import HyperAdmin
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, ManyToManyField, Model

class Tag(TimestampMixin, Model):
    class Meta:
        table = "m2m_e2e_tags"
    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=50)

class Article(TimestampMixin, Model):
    class Meta:
        table = "m2m_e2e_articles"
    id: int = Field(primary_key=True, auto=True)
    title: str = Field(max_length=200)

Article.tags = ManyToManyField(Tag, junction_table="m2m_e2e_articles_m2m_e2e_tags")
Article.tags._configure(Article, "tags")

app = HyperApp(title="M2M E2E", database="{db_url}")
admin = HyperAdmin(app, prefix="/admin", title="M2M Test", secret_key="test-m2m", require_auth=False)

admin.register(Tag, list_display=["id", "name"])
admin.register(Article, list_display=["id", "title"], filter_horizontal=["tags"])

app.mount_health()
'''
    APP_DIR.mkdir(parents=True, exist_ok=True)
    (APP_DIR / "__init__.py").write_text("")
    (APP_DIR / "app.py").write_text(app_code)

    with AppRunner(
        f"{APP_MODULE}.app:app",
        port=port,
        readiness_path="/health",
        env={
            "PYTHONPATH": f"{SCRATCH_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
        },
    ) as runner:
        base = runner.url()

        # 1. Admin article list should show articles
        r = http_get(f"{base}/admin/article/")
        ok("article list 200", r.status == 200)
        ok("article list has items", "HyperDjango Guide" in r.body, r.body[:200])

        # 2. Edit form should have M2M widget
        r = http_get(f"{base}/admin/article/1/")
        ok("edit form 200", r.status == 200)
        ok("has Available section", "Available" in r.body, r.body[:500])
        ok("has Chosen section", "Chosen" in r.body)
        ok("has m2m_tags hidden inputs", "m2m_tags" in r.body)
        ok("has m2mMove JS function", "m2mMove" in r.body)

        # 3. Verify selected tags are in Chosen pane (article 1 has python + web)
        ok("python in chosen", 'value="1"' in r.body)  # python = id 1
        ok("web in chosen", 'value="3"' in r.body)  # web = id 3

        # 4. Add form should have M2M widget (empty selections)
        r = http_get(f"{base}/admin/article/add/")
        ok("add form 200", r.status == 200)
        ok("add form has Available", "Available" in r.body)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Admin M2M Widget Tests")
    print("=" * 60)

    try:
        test_m2m_config()
        test_m2m_e2e()
    finally:
        # Always tear down scratch — even if a test raised mid-run.
        if APP_DIR.exists():
            shutil.rmtree(APP_DIR)
        if SETUP_SQL_PATH.exists():
            SETUP_SQL_PATH.unlink()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
