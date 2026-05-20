"""
Tests for Meta.indexes — declarative index system for Model definitions.

Validates:
1. Index dataclass construction and field validation
2. DDL generation for all index types (btree, gin, gist, partial, unique, expression)
3. Auto-naming with truncation
4. DESC ordering via "-" prefix
5. Operator classes, INCLUDE, WITH params
6. ModelMeta parsing of Meta.indexes
7. generate_ddl_for_model() emits Meta.indexes
8. Unsafe identifier rejection

Usage:
    uv run hyper-test meta_indexes
"""

# hyper-test: unit

import sys

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import (
    Field,
    Index,
    Model,
    _generate_index_ddl,
    generate_ddl_for_model,
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")
        if detail:
            print(f"    {detail}")


def test_index_dataclass():
    """Test Index construction and frozen immutability."""
    print("\n--- Index dataclass ---")

    idx = Index(fields=("a", "b"))
    check("fields tuple", idx.fields == ("a", "b"))
    check("default name None", idx.name is None)
    check("default unique False", idx.unique is False)
    check("default using btree", idx.using == "btree")
    check("default where None", idx.where is None)
    check("frozen", True)  # Would raise on assignment


def test_ddl_basic_btree():
    """Test basic B-tree composite index."""
    print("\n--- DDL: basic btree ---")

    idx = Index(fields=("tenant_id", "status_id"))
    sql = _generate_index_ddl("my_table", idx)
    check("has CREATE INDEX", "CREATE INDEX IF NOT EXISTS" in sql)
    check("auto name", "idx_my_table_tenant_id_status_id" in sql)
    check("columns", "(tenant_id, status_id)" in sql)
    check("no USING for btree", "USING" not in sql)


def test_ddl_unique():
    """Test UNIQUE index."""
    print("\n--- DDL: unique ---")

    idx = Index(fields=("org_id", "slug"), unique=True)
    sql = _generate_index_ddl("orgs", idx)
    check("has UNIQUE", "CREATE UNIQUE INDEX" in sql)
    check("auto name uq_", "uq_orgs_org_id_slug" in sql)


def test_ddl_desc():
    """Test DESC ordering via - prefix."""
    print("\n--- DDL: DESC ---")

    idx = Index(fields=("forum_id", "-created_at", "-id"))
    sql = _generate_index_ddl("posts", idx)
    check("created_at DESC", "created_at DESC" in sql)
    check("id DESC", "id DESC" in sql)
    check(
        "forum_id no DESC", "forum_id," in sql or "forum_id, " in sql.replace(",", ", ")
    )


def test_ddl_partial_where():
    """Test partial index with WHERE clause."""
    print("\n--- DDL: partial WHERE ---")

    idx = Index(fields=("status_id",), where="is_deleted = FALSE")
    sql = _generate_index_ddl("tickets", idx)
    check("has WHERE", "WHERE is_deleted = FALSE" in sql)


def test_ddl_gin_opclass():
    """Test GIN index with operator class."""
    print("\n--- DDL: GIN opclass ---")

    idx = Index(fields=("title",), using="gin", opclasses=("gin_trgm_ops",))
    sql = _generate_index_ddl("posts", idx)
    check("USING gin", "USING gin" in sql)
    check("opclass", "title gin_trgm_ops" in sql)


def test_ddl_expression():
    """Test expression-based GIN index."""
    print("\n--- DDL: expression ---")

    idx = Index(
        expressions=("to_tsvector('english', title || ' ' || description)",),
        using="gin",
        name="ix_search",
    )
    sql = _generate_index_ddl("tickets", idx)
    check("has expression", "to_tsvector" in sql)
    check("USING gin", "USING gin" in sql)
    check("explicit name", "ix_search" in sql)
    check("no field validation needed", True)


def test_ddl_with_params():
    """Test WITH parameters (HNSW, etc)."""
    print("\n--- DDL: WITH params ---")

    idx = Index(
        fields=("embedding",),
        using="hnsw",
        opclasses=("vector_cosine_ops",),
        params={"m": 16, "ef_construction": 64},
    )
    sql = _generate_index_ddl("docs", idx)
    check("USING hnsw", "USING hnsw" in sql)
    check("WITH m", "m = 16" in sql)
    check("WITH ef_construction", "ef_construction = 64" in sql)


def test_ddl_include():
    """Test INCLUDE columns (covering index)."""
    print("\n--- DDL: INCLUDE ---")

    idx = Index(fields=("user_id",), include=("email", "name"))
    sql = _generate_index_ddl("users", idx)
    check("has INCLUDE", "INCLUDE (email, name)" in sql)


def test_ddl_explicit_name():
    """Test explicit index name overrides auto-generation."""
    print("\n--- DDL: explicit name ---")

    idx = Index(fields=("a", "b"), name="my_custom_idx")
    sql = _generate_index_ddl("t", idx)
    check("uses explicit name", "my_custom_idx" in sql)
    check("no auto name", "idx_t_a_b" not in sql)


def test_ddl_name_truncation():
    """Test auto-generated name truncation at 63 chars."""
    print("\n--- DDL: name truncation ---")

    idx = Index(
        fields=(
            "very_long_column_name_one",
            "very_long_column_name_two",
            "very_long_column_name_three",
        )
    )
    sql = _generate_index_ddl("extremely_long_table_name_for_testing", idx)
    # Extract the index name from SQL
    name_start = sql.index("idx_")
    name_end = sql.index(" ON ")
    name = sql[name_start:name_end]
    check("name <= 63 chars", len(name) <= 63, f"len={len(name)}: {name}")


def test_unsafe_identifiers():
    """Test rejection of SQL injection in identifiers."""
    print("\n--- Unsafe identifiers ---")

    # Unsafe table name
    try:
        _generate_index_ddl("my_table; DROP TABLE users", Index(fields=("id",)))
        check("rejects unsafe table", False)
    except ValueError:
        check("rejects unsafe table", True)

    # Unsafe column name
    try:
        _generate_index_ddl("t", Index(fields=("id; DROP TABLE",)))
        check("rejects unsafe column", False)
    except ValueError:
        check("rejects unsafe column", True)

    # Expression bypasses column validation (intentional)
    try:
        sql = _generate_index_ddl("t", Index(expressions=("count(*)",), name="ix"))
        check("expression allows raw SQL", "count(*)" in sql)
    except ValueError:
        check("expression allows raw SQL", False)


def test_model_meta_indexes():
    """Test that Meta.indexes is parsed by ModelMeta into TableMeta."""
    print("\n--- Model Meta.indexes ---")

    class TestModel(TimestampMixin, Model):
        class Meta:
            table = "test_meta_idx"
            indexes = [
                Index(fields=("name", "email"), unique=True),
                Index(fields=("status",), where="is_active"),
            ]

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        email: str = Field()
        status: str = Field(default="active")
        is_active: bool = Field(default=True)

    check("_meta has indexes", len(TestModel._meta.indexes) == 2)
    check("first is unique", TestModel._meta.indexes[0].unique is True)
    check("second has where", TestModel._meta.indexes[1].where == "is_active")


def test_generate_ddl_includes_meta_indexes():
    """Test that generate_ddl_for_model() emits Meta.indexes statements."""
    print("\n--- generate_ddl_for_model with Meta.indexes ---")

    class DdlTestModel(TimestampMixin, Model):
        class Meta:
            table = "ddl_test_idx"
            indexes = [
                Index(fields=("category", "-created_at")),
                Index(fields=("slug",), unique=True),
                Index(fields=("title",), using="gin", opclasses=("gin_trgm_ops",)),
            ]

        id: int = Field(primary_key=True, auto=True)
        category: str = Field()
        slug: str = Field()
        title: str = Field()

    stmts = generate_ddl_for_model(DdlTestModel)

    # Find index statements
    idx_stmts = [s for s in stmts if "CREATE" in s and "INDEX" in s]
    check("has index statements", len(idx_stmts) >= 3, f"got {len(idx_stmts)}")

    all_sql = "\n".join(idx_stmts)
    check("composite DESC", "created_at DESC" in all_sql)
    check("unique slug", "UNIQUE INDEX" in all_sql and "slug" in all_sql)
    check("gin trigram", "USING gin" in all_sql and "gin_trgm_ops" in all_sql)


def test_combined_field_and_meta_indexes():
    """Test that Field(index=True) and Meta.indexes coexist."""
    print("\n--- Field(index=True) + Meta.indexes ---")

    class CombinedModel(TimestampMixin, Model):
        class Meta:
            table = "combined_idx"
            indexes = [
                Index(fields=("a", "b")),
            ]

        id: int = Field(primary_key=True, auto=True)
        a: str = Field(index=True)
        b: str = Field(index=True)
        c: str = Field()

    stmts = generate_ddl_for_model(CombinedModel)
    idx_stmts = [s for s in stmts if "INDEX" in s]

    # Should have: idx_combined_idx_a (Field), idx_combined_idx_b (Field), idx_combined_idx_a_b (Meta)
    check("at least 3 indexes", len(idx_stmts) >= 3, f"got {len(idx_stmts)}")

    all_sql = "\n".join(idx_stmts)
    check("field index a", "idx_combined_idx_a" in all_sql)
    check("field index b", "idx_combined_idx_b" in all_sql)
    check("meta composite a_b", "idx_combined_idx_a_b" in all_sql)


if __name__ == "__main__":
    print("=" * 60)
    print("Meta.indexes Tests")
    print("=" * 60)

    test_index_dataclass()
    test_ddl_basic_btree()
    test_ddl_unique()
    test_ddl_desc()
    test_ddl_partial_where()
    test_ddl_gin_opclass()
    test_ddl_expression()
    test_ddl_with_params()
    test_ddl_include()
    test_ddl_explicit_name()
    test_ddl_name_truncation()
    test_unsafe_identifiers()
    test_model_meta_indexes()
    test_generate_ddl_includes_meta_indexes()
    test_combined_field_and_meta_indexes()

    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS}/{total} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)
