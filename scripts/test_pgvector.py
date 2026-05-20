"""Tests for pgvector integration — VectorField, distance lookups, vector indexes.

Tests the Python-side implementation:
- VectorField creation and metadata
- Vector distance lookups (SQL generation)
- Vector index migration operations (SQL generation)
- Type mapping for vector columns
- Model definition with vector fields

Usage:
    uv run hyper-test pgvector
"""

# hyper-test: unit

import sys

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
    print("=" * 60)
    print("pgvector Integration Tests")
    print("=" * 60)

    # ── VectorField Creation ──────────────────────────────────────

    print("\n--- VectorField ---")

    from hyperdjango.models import Field, Model, VectorField

    # Test 1: Default VectorField
    vf = VectorField()
    check("default dimensions", vf.vector_dimensions == 1536)
    check("default index_type", vf.vector_index_type == "hnsw")
    check("default index_ops", vf.vector_index_ops == "vector_cosine_ops")
    check("default index enabled", vf.index is True)

    # Test 2: Custom VectorField
    vf2 = VectorField(dimensions=768, index_type="ivfflat", index_ops="vector_l2_ops")
    check("custom dimensions", vf2.vector_dimensions == 768)
    check("custom index_type", vf2.vector_index_type == "ivfflat")
    check("custom index_ops", vf2.vector_index_ops == "vector_l2_ops")

    # Test 3: VectorField without index
    vf3 = VectorField(dimensions=384, index=False)
    check("no index", vf3.index is False)
    check("no index dimensions", vf3.vector_dimensions == 384)

    # Test 4: Large dimension (text-embedding-3-large)
    vf4 = VectorField(dimensions=3072)
    check("large dimensions", vf4.vector_dimensions == 3072)

    # Test 5: Inner product ops
    vf5 = VectorField(index_ops="vector_ip_ops")
    check("inner product ops", vf5.vector_index_ops == "vector_ip_ops")

    # ── Model with VectorField ────────────────────────────────────

    print("\n--- Model Definition ---")

    class Document(Model):
        class Meta:
            table = "test_documents"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(max_length=255)
        embedding: list[float] = VectorField(dimensions=1536)

    # Test 6: Model has vector field in annotations
    check("model has embedding annotation", "embedding" in Document.__annotations__)

    # Test 7: Model field metadata
    field_info = Document.__dict__["embedding"]
    check("model field dimensions", field_info.vector_dimensions == 1536)
    check("model field index_type", field_info.vector_index_type == "hnsw")

    # Test 8: Model with multiple vector fields
    class MultiVecModel(Model):
        class Meta:
            table = "test_multi_vec"

        id: int = Field(primary_key=True, auto=True)
        title_embedding: list[float] = VectorField(
            dimensions=768, index_ops="vector_cosine_ops"
        )
        image_embedding: list[float] = VectorField(
            dimensions=512, index_ops="vector_l2_ops"
        )

    check(
        "multi vec model title dims",
        MultiVecModel.__dict__["title_embedding"].vector_dimensions == 768,
    )
    check(
        "multi vec model image dims",
        MultiVecModel.__dict__["image_embedding"].vector_dimensions == 512,
    )

    # ── Distance Lookups (SQL Generation) ─────────────────────────

    print("\n--- Distance Lookups ---")

    from hyperdjango.lookups import (
        CosineDistanceLookup,
        InnerProductLookup,
        L2DistanceLookup,
        NearestLookup,
        _format_vector,
        _lookup_registry,
    )

    # Test 9: _format_vector
    vec = [0.1, 0.2, 0.3]
    formatted = _format_vector(vec)
    check("format vector list", formatted == "[0.1,0.2,0.3]", repr(formatted))

    # Test 10: _format_vector with string passthrough
    vec_str = "[0.1,0.2,0.3]"
    check("format vector string passthrough", _format_vector(vec_str) == vec_str)

    # Test 11: L2 distance SQL
    lookup = L2DistanceLookup()
    sql, params = lookup.as_sql("embedding", 1, ([0.1, 0.2], 1.5))
    check("l2 distance sql operator", "<->" in sql, sql)
    check("l2 distance sql threshold", "< $2" in sql, sql)
    check("l2 distance params count", len(params) == 2)
    check("l2 distance param vector", params[0] == "[0.1,0.2]", repr(params[0]))
    check("l2 distance param threshold", params[1] == 1.5)

    # Test 12: Cosine distance SQL
    lookup = CosineDistanceLookup()
    sql, params = lookup.as_sql("embedding", 1, ([0.5, 0.5], 0.2))
    check("cosine distance sql operator", "<=>" in sql, sql)
    check("cosine distance sql threshold", "< $2" in sql, sql)
    check("cosine distance params count", len(params) == 2)

    # Test 13: Inner product SQL
    lookup = InnerProductLookup()
    sql, params = lookup.as_sql("embedding", 1, ([0.1, 0.2], -0.8))
    check("inner product sql operator", "<#>" in sql, sql)
    check("inner product params count", len(params) == 2)

    # Test 14: Nearest lookup SQL
    lookup = NearestLookup()
    sql, params = lookup.as_sql("embedding", 1, ([0.1, 0.2], "cosine"))
    check("nearest sql cosine op", "<=>" in sql, sql)
    check("nearest params count", len(params) == 1)

    # Test 15: Nearest with L2 metric
    sql, params = lookup.as_sql("embedding", 1, ([0.1, 0.2], "l2"))
    check("nearest sql l2 op", "<->" in sql, sql)

    # Test 16: Nearest with inner_product metric
    sql, params = lookup.as_sql("embedding", 1, ([0.1, 0.2], "inner_product"))
    check("nearest sql ip op", "<#>" in sql, sql)

    # Test 17: Lookups registered in registry
    check("l2_distance in registry", "l2_distance" in _lookup_registry)
    check("cosine_distance in registry", "cosine_distance" in _lookup_registry)
    check("inner_product in registry", "inner_product" in _lookup_registry)
    check("nearest in registry", "nearest" in _lookup_registry)

    # ── Migration Operations (SQL Generation) ─────────────────────

    print("\n--- Migration Operations ---")

    from hyperdjango.migrations import (
        CreateVectorIndex,
        ModelExtractor,
    )

    # Test 18: HNSW index SQL
    op = CreateVectorIndex(
        table="documents",
        column="embedding",
        index_type="hnsw",
        index_ops="vector_cosine_ops",
    )
    sql = op.up_sql()
    check("hnsw index sql contains USING hnsw", "USING hnsw" in sql, sql)
    check("hnsw index sql contains cosine ops", "vector_cosine_ops" in sql, sql)
    check("hnsw index sql contains m param", "m = 16" in sql, sql)
    check("hnsw index sql contains ef param", "ef_construction = 64" in sql, sql)
    check("hnsw index description", "hnsw" in op.description())

    # Test 19: IVFFlat index SQL
    op2 = CreateVectorIndex(
        table="documents",
        column="embedding",
        index_type="ivfflat",
        index_ops="vector_l2_ops",
        lists=200,
    )
    sql2 = op2.up_sql()
    check("ivfflat index sql contains USING ivfflat", "USING ivfflat" in sql2, sql2)
    check("ivfflat index sql contains l2 ops", "vector_l2_ops" in sql2, sql2)
    check("ivfflat index sql contains lists", "lists = 200" in sql2, sql2)

    # Test 20: Drop vector index
    down_sql = op.down_sql()
    check("drop vector index sql", "DROP INDEX IF EXISTS" in down_sql, down_sql)

    # Test 21: Custom HNSW params
    op3 = CreateVectorIndex(
        table="docs",
        column="vec",
        index_type="hnsw",
        index_ops="vector_ip_ops",
        m=32,
        ef_construction=128,
    )
    sql3 = op3.up_sql()
    check("custom hnsw m", "m = 32" in sql3, sql3)
    check("custom hnsw ef", "ef_construction = 128" in sql3, sql3)

    # ── Type Mapping ──────────────────────────────────────────────

    print("\n--- Type Mapping ---")

    # Test 22: _get_type for vector field
    class VecModel(Model):
        class Meta:
            table = "test_vec_type"

        id: int = Field(primary_key=True, auto=True)
        embedding: list[float] = VectorField(dimensions=1536)

    type_sql = ModelExtractor._get_type(
        VecModel, "embedding", VecModel.__dict__["embedding"]
    )
    check("vector type sql", type_sql == "vector(1536)", repr(type_sql))

    # Test 23: _get_type for different dimensions
    class VecModel2(Model):
        class Meta:
            table = "test_vec_type2"

        id: int = Field(primary_key=True, auto=True)
        small_vec: list[float] = VectorField(dimensions=384)

    type_sql2 = ModelExtractor._get_type(
        VecModel2, "small_vec", VecModel2.__dict__["small_vec"]
    )
    check("vector type 384 dims", type_sql2 == "vector(384)", repr(type_sql2))

    # Test 24: Regular field type still works
    type_int = ModelExtractor._get_type(VecModel, "id", VecModel.__dict__["id"])
    check("int type still works", type_int in ("INTEGER", "SERIAL"), repr(type_int))

    # ── Vector Formatting Edge Cases ──────────────────────────────

    print("\n--- Vector Formatting ---")

    # Test 25: Empty vector
    check("format empty vector", _format_vector([]) == "[]")

    # Test 26: Single element vector
    check("format single element", _format_vector([1.0]) == "[1.0]")

    # Test 27: High-dimensional vector
    big_vec = [float(i) / 100 for i in range(1536)]
    formatted_big = _format_vector(big_vec)
    check("format 1536-dim vector starts with [", formatted_big.startswith("["))
    check("format 1536-dim vector ends with ]", formatted_big.endswith("]"))
    check("format 1536-dim vector has commas", formatted_big.count(",") == 1535)

    # Test 28: Integer values coerced to float
    check("format int vector", _format_vector([1, 2, 3]) == "[1.0,2.0,3.0]")

    # ── Lookup Resolution ──────────────────────────────────────────

    print("\n--- Lookup Resolution ---")

    from hyperdjango.lookups import resolve_lookup

    # Test 29: resolve_lookup for cosine_distance
    sql, params = resolve_lookup(
        "embedding__cosine_distance",
        ([0.5, 0.5], 0.2),
        param_idx=1,
        table_alias="documents",
    )
    check("resolve cosine_distance sql", "<=>" in sql, sql)

    # Test 30: resolve_lookup for l2_distance
    sql, params = resolve_lookup(
        "embedding__l2_distance",
        ([0.1, 0.2], 1.5),
        param_idx=1,
        table_alias="documents",
    )
    check("resolve l2_distance sql", "<->" in sql, sql)

    # Test 31: resolve_lookup for inner_product
    sql, params = resolve_lookup(
        "embedding__inner_product",
        ([0.1, 0.2], -0.5),
        param_idx=1,
        table_alias="documents",
    )
    check("resolve inner_product sql", "<#>" in sql, sql)

    # ── ModelExtractor with Vector Fields ──────────────────────────

    print("\n--- ModelExtractor ---")

    # Test 32: Full model extraction
    schema = ModelExtractor.extract(Document)
    check("extract table name", schema.table == "test_documents")
    check("extract has embedding column", "embedding" in schema.columns)
    check(
        "extract embedding type",
        schema.columns["embedding"].type_sql == "vector(1536)",
        repr(schema.columns["embedding"].type_sql),
    )
    check("extract embedding has index", schema.columns["embedding"].has_index is True)
    check("extract model ref stored", schema._model is Document)

    # Test 33: Multiple vector fields extraction
    schema2 = ModelExtractor.extract(MultiVecModel)
    check(
        "extract multi vec title",
        schema2.columns["title_embedding"].type_sql == "vector(768)",
    )
    check(
        "extract multi vec image",
        schema2.columns["image_embedding"].type_sql == "vector(512)",
    )

    # ── Summary ──────────────────────────────────────────────────────

    print("\n" + "=" * 60)
    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"Results: {RESULTS['passed']}/{total} passed")
    if RESULTS["errors"]:
        print(f"Failures: {', '.join(RESULTS['errors'])}")
    print("=" * 60)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
