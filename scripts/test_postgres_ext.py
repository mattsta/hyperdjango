#!/usr/bin/env python3
"""
Tests for PostgreSQL-specific extensions (hyperdjango.postgres).

Covers field types, full-text search, trigram similarity, array lookups,
aggregate functions, range types, constraints, indexes, and ORM lookup
registration.

Usage:
    uv run hyper-test postgres_ext
"""

# hyper-test: unit

import dataclasses
import datetime

from hyperdjango.expressions import Expression
from hyperdjango.lookups import resolve_lookup
from hyperdjango.postgres import (
    ArrayAgg,
    ArrayAppend,
    ArrayCat,
    ArrayContainedBy,
    ArrayContains,
    ArrayField,
    ArrayIndex,
    ArrayLength,
    ArrayOverlap,
    ArrayPosition,
    ArrayPrepend,
    ArrayRemove,
    BigIntegerRange,
    BitAnd,
    BitOr,
    BoolAnd,
    BoolOr,
    BrinIndex,
    BTreeIndex,
    DateRange,
    DateTimeRange,
    DecimalRange,
    ExclusionConstraint,
    GinIndex,
    GistIndex,
    HashIndex,
    HStoreField,
    IntegerRange,
    JSONBAgg,
    JSONBField,
    RangeAdjacentTo,
    RangeContainedBy,
    RangeContains,
    RangeFullyGreaterThan,
    RangeFullyLessThan,
    RangeOverlap,
    SearchHeadline,
    SearchQuery,
    SearchRank,
    SearchVector,
    SpGistIndex,
    StringAgg,
    TrigramDistance,
    TrigramSimilarity,
    TrigramWordDistance,
    TrigramWordSimilarity,
    Unnest,
)
from hyperdjango.testkit import check, finish, run_main


def main() -> bool:
    print("=" * 60)
    print("PostgreSQL Extensions Tests")
    print("=" * 60)

    # --- ArrayField ---
    print("\n--- ArrayField db_type ---")
    test_array_field_db_types()

    print("\n--- ArrayField create_sql ---")
    test_array_field_create_sql()

    print("\n--- ArrayField dataclass ---")
    test_array_field_dataclass()

    # --- HStoreField / JSONBField ---
    print("\n--- HStoreField / JSONBField ---")
    test_hstore_jsonb_fields()

    # --- SearchVector ---
    print("\n--- SearchVector ---")
    test_search_vector()

    # --- SearchQuery ---
    print("\n--- SearchQuery ---")
    test_search_query()

    # --- SearchRank ---
    print("\n--- SearchRank ---")
    test_search_rank()

    # --- SearchHeadline ---
    print("\n--- SearchHeadline ---")
    test_search_headline()

    # --- TrigramSimilarity ---
    print("\n--- TrigramSimilarity ---")
    test_trigram_similarity()

    # --- TrigramDistance ---
    print("\n--- TrigramDistance ---")
    test_trigram_distance()

    # --- TrigramWordSimilarity/Distance ---
    print("\n--- TrigramWord ---")
    test_trigram_word()

    # --- Array Lookups ---
    print("\n--- Array Lookups ---")
    test_array_lookups()

    # --- Array Functions ---
    print("\n--- Array Functions ---")
    test_array_functions()

    # --- ArrayAgg ---
    print("\n--- ArrayAgg ---")
    test_array_agg()

    # --- JSONBAgg ---
    print("\n--- JSONBAgg ---")
    test_jsonb_agg()

    # --- StringAgg ---
    print("\n--- StringAgg ---")
    test_string_agg()

    # --- Bit/Bool Aggregates ---
    print("\n--- Bit/Bool Aggregates ---")
    test_bit_bool_agg()

    # --- IntegerRange ---
    print("\n--- IntegerRange ---")
    test_integer_range()

    # --- BigIntegerRange ---
    print("\n--- BigIntegerRange ---")
    test_biginteger_range()

    # --- DecimalRange ---
    print("\n--- DecimalRange ---")
    test_decimal_range()

    # --- DateRange ---
    print("\n--- DateRange ---")
    test_date_range()

    # --- DateTimeRange ---
    print("\n--- DateTimeRange ---")
    test_datetime_range()

    # --- Range Lookups ---
    print("\n--- Range Lookups ---")
    test_range_lookups()

    # --- ExclusionConstraint ---
    print("\n--- ExclusionConstraint ---")
    test_exclusion_constraint()

    # --- Indexes ---
    print("\n--- Indexes ---")
    test_indexes()

    # --- ORM Lookup Registration ---
    print("\n--- ORM Lookup Registration ---")
    test_orm_lookups()

    # --- Dataclass slots verification ---
    print("\n--- Slots Verification ---")
    test_slots()

    # --- Summary ---
    print("\n" + "=" * 60)
    return finish()


# ---------------------------------------------------------------------------
# ArrayField
# ---------------------------------------------------------------------------


def test_array_field_db_types():
    check("int -> integer[]", ArrayField(base_type="int").db_type == "integer[]")
    check(
        "integer -> integer[]", ArrayField(base_type="integer").db_type == "integer[]"
    )
    check("text -> text[]", ArrayField(base_type="text").db_type == "text[]")
    check("uuid -> uuid[]", ArrayField(base_type="uuid").db_type == "uuid[]")
    check(
        "float -> double precision[]",
        ArrayField(base_type="float").db_type == "double precision[]",
    )
    check("bool -> boolean[]", ArrayField(base_type="bool").db_type == "boolean[]")
    check(
        "boolean -> boolean[]", ArrayField(base_type="boolean").db_type == "boolean[]"
    )
    check("bigint -> bigint[]", ArrayField(base_type="bigint").db_type == "bigint[]")
    check(
        "smallint -> smallint[]",
        ArrayField(base_type="smallint").db_type == "smallint[]",
    )
    check("date -> date[]", ArrayField(base_type="date").db_type == "date[]")
    check("jsonb -> jsonb[]", ArrayField(base_type="jsonb").db_type == "jsonb[]")
    check(
        "numeric -> numeric[]", ArrayField(base_type="numeric").db_type == "numeric[]"
    )
    check("inet -> inet[]", ArrayField(base_type="inet").db_type == "inet[]")
    check("default base_type is text", ArrayField().db_type == "text[]")
    check(
        "unknown type fallback", ArrayField(base_type="macaddr").db_type == "macaddr[]"
    )


def test_array_field_create_sql():
    af = ArrayField(base_type="int", default=[1, 2, 3])
    sql = af.create_sql
    check("create_sql contains type", "integer[]" in sql)
    check("create_sql contains default", "DEFAULT" in sql)
    check("create_sql default values", "1,2,3" in sql)

    af_no_default = ArrayField(base_type="text")
    check("create_sql no default", "DEFAULT" not in af_no_default.create_sql)


def test_array_field_dataclass():
    af = ArrayField(base_type="int", size=5, default=[1])
    check("ArrayField base_type", af.base_type == "int")
    check("ArrayField size", af.size == 5)
    check("ArrayField default", af.default == [1])

    af2 = ArrayField()
    check(
        "ArrayField defaults",
        af2.base_type == "text" and af2.size is None and af2.default is None,
    )


# ---------------------------------------------------------------------------
# HStoreField / JSONBField
# ---------------------------------------------------------------------------


def test_hstore_jsonb_fields():
    h = HStoreField()
    check("HStoreField db_type", h.db_type == "hstore")
    check("HStoreField default None", h.default is None)

    h2 = HStoreField(default={"k": "v"})
    check("HStoreField with default", h2.default == {"k": "v"})

    j = JSONBField()
    check("JSONBField db_type", j.db_type == "jsonb")
    check("JSONBField default None", j.default is None)


# ---------------------------------------------------------------------------
# SearchVector
# ---------------------------------------------------------------------------


def test_search_vector():
    sv = SearchVector(fields=["title"])
    sql, params = sv.as_sql()
    check("single field", "to_tsvector('english', COALESCE(\"title\", ''))" in sql)
    check("single field quoted col", '"title"' in sql)
    check("single field no params", params == [])

    sv2 = SearchVector(fields=["title", "body"])
    sql2, _ = sv2.as_sql()
    check("multi-field contains ||", " || " in sql2)
    check("multi-field title", "COALESCE(\"title\", '')" in sql2)
    check("multi-field body", "COALESCE(\"body\", '')" in sql2)

    sv3 = SearchVector(fields=["title"], weight="A")
    sql3, _ = sv3.as_sql()
    check("weighted vector", "setweight(" in sql3)
    check("weight value A", "'A'" in sql3)

    sv4 = SearchVector(fields=["title"], config="spanish")
    sql4, _ = sv4.as_sql()
    check("custom config", "'spanish'" in sql4)

    sv5 = SearchVector(fields=["title", "body"], weight="B", config="french")
    sql5, _ = sv5.as_sql()
    check(
        "multi weighted french", "'french'" in sql5 and "'B'" in sql5 and " || " in sql5
    )

    sv6 = SearchVector(fields=["title"], config="it's")
    sql6, _ = sv6.as_sql()
    check("config with apostrophe escaped", "'it''s'" in sql6)

    # Expression interface
    check("is Expression", isinstance(sv, Expression))
    check("default_alias", sv.default_alias == "search_vector")
    check("not aggregate", sv.contains_aggregate is False)


# ---------------------------------------------------------------------------
# SearchQuery
# ---------------------------------------------------------------------------


def test_search_query():
    sq = SearchQuery(query="hello world")
    sql, params = sq.as_sql()
    check("plain default", "plainto_tsquery" in sql)
    check("config in sql", "'english'" in sql)
    check("query is parameterized", params == ["hello world"])
    check("param placeholder", "$1" in sql)

    sq2 = SearchQuery(query="hello world", search_type="phrase")
    sql2, _ = sq2.as_sql()
    check("phrase type", "phraseto_tsquery" in sql2)

    sq3 = SearchQuery(query="hello & world", search_type="raw")
    sql3, _ = sq3.as_sql()
    check("raw type", "to_tsquery" in sql3 and "plainto" not in sql3)

    sq4 = SearchQuery(query="hello world", search_type="websearch")
    sql4, _ = sq4.as_sql()
    check("websearch type", "websearch_to_tsquery" in sql4)

    sq5 = SearchQuery(query="hola", config="spanish")
    sql5, _ = sq5.as_sql()
    check("spanish config query", "'spanish'" in sql5)

    sq6 = SearchQuery(query="test", search_type="unknown")
    sql6, _ = sq6.as_sql()
    check("unknown type fallback", "plainto_tsquery" in sql6)

    # Param offset tracking
    sql_offset, params_offset = sq.as_sql(param_offset=5)
    check("param offset respected", "$6" in sql_offset)
    check("is Expression", isinstance(sq, Expression))


# ---------------------------------------------------------------------------
# SearchRank
# ---------------------------------------------------------------------------


def test_search_rank():
    sv = SearchVector(fields=["title"])
    sq = SearchQuery(query="test")
    sr = SearchRank(vector=sv, query=sq)
    sql, params = sr.as_sql()
    check("basic rank", "ts_rank(" in sql)
    check("rank contains vector", "to_tsvector" in sql)
    check("rank has query param", params == ["test"])

    sr2 = SearchRank(vector=sv, query=sq, weights=[0.1, 0.2, 0.4, 1.0])
    sql2, _ = sr2.as_sql()
    check("weighted rank", "0.1" in sql2 and "1.0" in sql2)
    check("weighted rank array syntax", "'{" in sql2)

    # Composed param offset
    sql3, params3 = sr.as_sql(param_offset=3)
    check("rank param offset", "$4" in sql3)
    check("is Expression", isinstance(sr, Expression))


# ---------------------------------------------------------------------------
# SearchHeadline
# ---------------------------------------------------------------------------


def test_search_headline():
    sq = SearchQuery(query="test")
    sh = SearchHeadline(field="body", query=sq)
    sql, params = sh.as_sql()
    check("headline contains ts_headline", "ts_headline(" in sql)
    check("headline field quoted", '"body"' in sql)
    check("headline start_sel", "StartSel=<b>" in sql)
    check("headline stop_sel", "StopSel=</b>" in sql)
    check("headline has query param", params == ["test"])

    sh2 = SearchHeadline(
        field="body", query=sq, start_sel="<em>", stop_sel="</em>", max_words=50
    )
    sql2, _ = sh2.as_sql()
    check(
        "custom headline markers", "StartSel=<em>" in sql2 and "StopSel=</em>" in sql2
    )
    check("custom max_words", "MaxWords=50" in sql2)

    sh3 = SearchHeadline(field="body", query=sq, config="it's")
    sql3, _ = sh3.as_sql()
    check("headline config escaped", "'it''s'" in sql3)


# ---------------------------------------------------------------------------
# Trigram
# ---------------------------------------------------------------------------


def test_trigram_similarity():
    ts = TrigramSimilarity(field="name", value="test")
    sql, params = ts.as_sql()
    check("similarity function", "similarity(" in sql and '"name"' in sql)
    check("similarity parameterized", params == ["test"])
    check("similarity is Expression", isinstance(ts, Expression))

    # Param offset
    sql2, _ = ts.as_sql(param_offset=5)
    check("similarity offset", "$6" in sql2)


def test_trigram_distance():
    td = TrigramDistance(field="name", value="test")
    sql, params = td.as_sql()
    check("distance operator", "<->" in sql)
    check("distance parameterized", params == ["test"])


def test_trigram_word():
    tws = TrigramWordSimilarity(field="description", value="test")
    sql, params = tws.as_sql()
    check("word_similarity", "word_similarity(" in sql and '"description"' in sql)
    check("word_similarity param", params == ["test"])

    twd = TrigramWordDistance(field="description", value="test")
    sql2, params2 = twd.as_sql()
    check("word distance operator", "<<->" in sql2)
    check("word distance param", params2 == ["test"])


# ---------------------------------------------------------------------------
# Array Lookups (standalone)
# ---------------------------------------------------------------------------


def test_array_lookups():
    ac = ArrayContains(column="tags", values=[1, 2])
    check("array contains @>", ac.as_sql() == "tags @> ${param}")

    acb = ArrayContainedBy(column="tags", values=[1, 2, 3])
    check("array contained_by <@", acb.as_sql() == "tags <@ ${param}")

    ao = ArrayOverlap(column="tags", values=[1, 2])
    check("array overlap &&", ao.as_sql() == "tags && ${param}")

    al = ArrayLength(column="tags")
    check("array length default dim", al.as_sql() == "array_length(tags, 1)")

    al2 = ArrayLength(column="matrix", dimension=2)
    check("array length dim 2", al2.as_sql() == "array_length(matrix, 2)")

    ai = ArrayIndex(column="tags", index=1)
    check("array index 1", ai.as_sql() == "tags[1]")

    ai3 = ArrayIndex(column="tags", index=3)
    check("array index 3", ai3.as_sql() == "tags[3]")


# ---------------------------------------------------------------------------
# Array Functions
# ---------------------------------------------------------------------------


def test_array_functions():
    ar = ArrayRemove(column="tags", value=5)
    check("array_remove", ar.as_sql() == "array_remove(tags, ${param})")

    aa = ArrayAppend(column="tags", value=10)
    check("array_append", aa.as_sql() == "array_append(tags, ${param})")

    ap = ArrayPrepend(column="tags", value=0)
    check("array_prepend", ap.as_sql() == "array_prepend(${param}, tags)")

    acat = ArrayCat(column="a", other_column="b")
    check("array_cat", acat.as_sql() == 'array_cat("a", "b")')

    apos = ArrayPosition(column="tags", value=5)
    check("array_position", apos.as_sql() == "array_position(tags, ${param})")

    un = Unnest(column="tags")
    check("unnest", un.as_sql() == "unnest(tags)")


# ---------------------------------------------------------------------------
# ArrayAgg
# ---------------------------------------------------------------------------


def test_array_agg():
    aa = ArrayAgg(field="name")
    check("basic array_agg", aa.as_sql() == "array_agg(name)")

    aa2 = ArrayAgg(field="name", distinct=True)
    check("distinct array_agg", aa2.as_sql() == "array_agg(DISTINCT name)")

    aa3 = ArrayAgg(field="name", ordering="name ASC")
    check("ordered array_agg", aa3.as_sql() == "array_agg(name ORDER BY name ASC)")

    aa4 = ArrayAgg(field="name", filter_condition="active = true")
    sql4 = aa4.as_sql()
    check("filtered array_agg", "FILTER (WHERE active = true)" in sql4)

    aa5 = ArrayAgg(
        field="name", distinct=True, ordering="name DESC", filter_condition="id > 0"
    )
    sql5 = aa5.as_sql()
    check(
        "full array_agg", "DISTINCT" in sql5 and "ORDER BY" in sql5 and "FILTER" in sql5
    )

    aa6 = ArrayAgg(field="val", default=[])
    sql6 = aa6.as_sql()
    check("array_agg with default", "COALESCE(" in sql6)


# ---------------------------------------------------------------------------
# JSONBAgg
# ---------------------------------------------------------------------------


def test_jsonb_agg():
    ja = JSONBAgg(field="data")
    check("basic jsonb_agg", ja.as_sql() == "jsonb_agg(data)")

    ja2 = JSONBAgg(field="data", distinct=True)
    check("distinct jsonb_agg", ja2.as_sql() == "jsonb_agg(DISTINCT data)")

    ja3 = JSONBAgg(field="data", ordering="id ASC")
    check("ordered jsonb_agg", ja3.as_sql() == "jsonb_agg(data ORDER BY id ASC)")

    ja4 = JSONBAgg(field="data", filter_condition="active = true")
    sql4 = ja4.as_sql()
    check("filtered jsonb_agg", "FILTER (WHERE active = true)" in sql4)

    ja5 = JSONBAgg(field="data", default="[]")
    sql5 = ja5.as_sql()
    check("jsonb_agg with default", "COALESCE(" in sql5 and "'[]'::jsonb" in sql5)


# ---------------------------------------------------------------------------
# StringAgg
# ---------------------------------------------------------------------------


def test_string_agg():
    sa = StringAgg(field="name")
    check("basic string_agg", sa.as_sql() == "string_agg(name, ', ')")

    sa2 = StringAgg(field="name", delimiter=" | ")
    check("custom delimiter", sa2.as_sql() == "string_agg(name, ' | ')")

    sa3 = StringAgg(field="name", distinct=True)
    check("distinct string_agg", sa3.as_sql() == "string_agg(DISTINCT name, ', ')")

    sa4 = StringAgg(field="name", ordering="name ASC")
    check(
        "ordered string_agg", sa4.as_sql() == "string_agg(name ORDER BY name ASC, ', ')"
    )

    sa5 = StringAgg(field="name", filter_condition="active = true")
    sql5 = sa5.as_sql()
    check("filtered string_agg", "FILTER (WHERE active = true)" in sql5)

    sa6 = StringAgg(field="name", default="N/A")
    sql6 = sa6.as_sql()
    check("string_agg with default", "COALESCE(" in sql6 and "'N/A'" in sql6)

    sa7 = StringAgg(field="tag", delimiter="-", distinct=True, ordering="tag")
    sql7 = sa7.as_sql()
    check(
        "full string_agg", "DISTINCT" in sql7 and "'-'" in sql7 and "ORDER BY" in sql7
    )


# ---------------------------------------------------------------------------
# Bit/Bool Aggregates
# ---------------------------------------------------------------------------


def test_bit_bool_agg():
    ba = BitAnd(field="flags")
    check("bit_and", ba.as_sql() == "bit_and(flags)")

    bo = BitOr(field="flags")
    check("bit_or", bo.as_sql() == "bit_or(flags)")

    ba2 = BoolAnd(field="active")
    check("bool_and", ba2.as_sql() == "bool_and(active)")

    bo2 = BoolOr(field="active")
    check("bool_or", bo2.as_sql() == "bool_or(active)")

    ba3 = BitAnd(field="flags", filter_condition="id > 5")
    sql3 = ba3.as_sql()
    check("bit_and filtered", "FILTER (WHERE id > 5)" in sql3)

    bo3 = BoolOr(field="active", filter_condition="role = 'admin'")
    sql3b = bo3.as_sql()
    check("bool_or filtered", "FILTER" in sql3b)


# ---------------------------------------------------------------------------
# IntegerRange
# ---------------------------------------------------------------------------


def test_integer_range():
    ir = IntegerRange(lower=1, upper=10)
    sql = ir.as_sql()
    check("int4range basic", sql == "int4range(1, 10, '[)')")

    ir2 = IntegerRange(lower=1, upper=10, bounds="[]")
    check("int4range inclusive", ir2.as_sql() == "int4range(1, 10, '[]')")

    ir3 = IntegerRange(lower=None, upper=10)
    check("int4range null lower", ir3.as_sql() == "int4range(NULL, 10, '[)')")

    ir4 = IntegerRange(lower=5, upper=None)
    check("int4range null upper", ir4.as_sql() == "int4range(5, NULL, '[)')")

    check("int4range db_type", ir.db_type == "int4range")

    c = ir.contains(5)
    check("int4range contains", "@> 5" in c)


# ---------------------------------------------------------------------------
# BigIntegerRange
# ---------------------------------------------------------------------------


def test_biginteger_range():
    br = BigIntegerRange(lower=1, upper=1000000000)
    sql = br.as_sql()
    check("int8range basic", sql == "int8range(1, 1000000000, '[)')")
    check("int8range db_type", br.db_type == "int8range")

    br2 = BigIntegerRange(lower=None, upper=None)
    check("int8range null bounds", br2.as_sql() == "int8range(NULL, NULL, '[)')")


# ---------------------------------------------------------------------------
# DecimalRange
# ---------------------------------------------------------------------------


def test_decimal_range():
    dr = DecimalRange(lower=1.5, upper=9.9)
    sql = dr.as_sql()
    check("numrange basic", sql == "numrange(1.5, 9.9, '[)')")
    check("numrange db_type", dr.db_type == "numrange")


# ---------------------------------------------------------------------------
# DateRange
# ---------------------------------------------------------------------------


def test_date_range():
    dr = DateRange(lower="2024-01-01", upper="2024-12-31")
    sql = dr.as_sql()
    check("daterange basic", "daterange(" in sql)
    check("daterange lower", "'2024-01-01'" in sql)
    check("daterange upper", "'2024-12-31'" in sql)
    check("daterange db_type", dr.db_type == "daterange")

    dr2 = DateRange(lower=datetime.date(2024, 6, 1), upper=datetime.date(2024, 6, 30))
    sql2 = dr2.as_sql()
    check("daterange with date objects", "2024-06-01" in sql2 and "2024-06-30" in sql2)

    dr3 = DateRange()
    check("daterange empty", dr3.as_sql() == "daterange(NULL, NULL, '[)')")


# ---------------------------------------------------------------------------
# DateTimeRange
# ---------------------------------------------------------------------------


def test_datetime_range():
    dtr = DateTimeRange(lower="2024-01-01 00:00:00+00", upper="2024-12-31 23:59:59+00")
    sql = dtr.as_sql()
    check("tstzrange basic", "tstzrange(" in sql)
    check("tstzrange db_type", dtr.db_type == "tstzrange")

    dtr2 = DateTimeRange()
    check("tstzrange empty", dtr2.as_sql() == "tstzrange(NULL, NULL, '[)')")


# ---------------------------------------------------------------------------
# Range Lookups
# ---------------------------------------------------------------------------


def test_range_lookups():
    rc = RangeContains(column="period", value=5)
    check("range contains", rc.as_sql() == "period @> ${param}")

    rcb = RangeContainedBy(column="period", other="[1,10)")
    check("range contained_by", rcb.as_sql() == "period <@ ${param}")

    ro = RangeOverlap(column="period", other="[5,15)")
    check("range overlap", ro.as_sql() == "period && ${param}")

    rlt = RangeFullyLessThan(column="period", other="[20,30)")
    check("range fully_lt", rlt.as_sql() == "period << ${param}")

    rgt = RangeFullyGreaterThan(column="period", other="[1,5)")
    check("range fully_gt", rgt.as_sql() == "period >> ${param}")

    ra = RangeAdjacentTo(column="period", other="[10,20)")
    check("range adjacent", ra.as_sql() == "period -|- ${param}")


# ---------------------------------------------------------------------------
# ExclusionConstraint
# ---------------------------------------------------------------------------


def test_exclusion_constraint():
    ec = ExclusionConstraint(
        name="no_overlap",
        expressions=[("room", "="), ("period", "&&")],
    )
    sql = ec.as_sql()
    check("exclusion basic", 'CONSTRAINT "no_overlap" EXCLUDE USING GIST' in sql)
    check("exclusion quoted name", '"no_overlap"' in sql)
    check("exclusion quoted cols", '"room" WITH =' in sql and '"period" WITH &&' in sql)

    ec2 = ExclusionConstraint(
        name="cond_exclude",
        expressions=[("room", "="), ("period", "&&")],
        condition="cancelled = false",
    )
    sql2 = ec2.as_sql()
    check("exclusion with condition", "WHERE (cancelled = false)" in sql2)

    ec3 = ExclusionConstraint(
        name="btree_exclude",
        expressions=[("id", "=")],
        index_type="BTREE",
    )
    check("exclusion custom index type", "USING BTREE" in ec3.as_sql())


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def test_indexes():
    # GIN
    gi = GinIndex(name="idx_tags", fields=["tags"])
    sql = gi.as_sql("articles")
    check(
        "gin index basic",
        sql == 'CREATE INDEX "idx_tags" ON "articles" USING GIN ("tags")',
    )
    check("gin index quoted name", '"idx_tags"' in sql)
    check("gin index quoted table", '"articles"' in sql)
    check("gin index quoted col", '"tags"' in sql)

    gi2 = GinIndex(name="idx_search", fields=["search_vector"], opclass="gin_trgm_ops")
    sql2 = gi2.as_sql("docs")
    check("gin index opclass", "gin_trgm_ops" in sql2)
    check("gin index opclass quoted col", '"search_vector" gin_trgm_ops' in sql2)

    gi3 = GinIndex(name="idx_cond", fields=["data"], condition="active = true")
    sql3 = gi3.as_sql("items")
    check("gin index conditional", "WHERE (active = true)" in sql3)

    # GiST
    gist = GistIndex(name="idx_range", fields=["period"])
    sql4 = gist.as_sql("events")
    check("gist index", "USING GiST" in sql4)
    check("gist index quoted", '"idx_range" ON "events"' in sql4)

    gist2 = GistIndex(name="idx_geo", fields=["location"], opclass="gist_trgm_ops")
    sql5 = gist2.as_sql("places")
    check("gist index opclass", "gist_trgm_ops" in sql5)

    # BRIN
    brin = BrinIndex(name="idx_created", fields=["created_at"])
    sql6 = brin.as_sql("logs")
    check("brin index", "USING BRIN" in sql6)
    check("brin pages_per_range", "pages_per_range = 128" in sql6)
    check("brin index quoted", '"idx_created" ON "logs"' in sql6)

    brin2 = BrinIndex(name="idx_ts", fields=["ts"], pages_per_range=64)
    sql7 = brin2.as_sql("events")
    check("brin custom pages", "pages_per_range = 64" in sql7)

    # Hash
    hi = HashIndex(name="idx_email", fields=["email"])
    sql8 = hi.as_sql("users")
    check("hash index", "USING HASH" in sql8)
    check("hash index quoted", '"idx_email" ON "users"' in sql8)

    # SP-GiST
    sp = SpGistIndex(name="idx_ip", fields=["ip_addr"])
    sql9 = sp.as_sql("connections")
    check("spgist index", "USING SPGiST" in sql9)
    check("spgist index quoted", '"idx_ip" ON "connections"' in sql9)

    # BTree
    bt = BTreeIndex(name="idx_name", fields=["name"], opclass="varchar_pattern_ops")
    sql10 = bt.as_sql("users")
    check("btree index", "USING BTREE" in sql10)
    check("btree opclass", "varchar_pattern_ops" in sql10)
    check("btree index quoted", '"idx_name" ON "users"' in sql10)

    # Multi-field
    gi_multi = GinIndex(name="idx_multi", fields=["title", "body"])
    sql11 = gi_multi.as_sql("posts")
    check("multi-field gin", '"title", "body"' in sql11)

    # Conditional hash
    hi2 = HashIndex(
        name="idx_active_email", fields=["email"], condition="active = true"
    )
    sql12 = hi2.as_sql("users")
    check("hash conditional", "WHERE (active = true)" in sql12)


# ---------------------------------------------------------------------------
# ORM Lookup Registration
# ---------------------------------------------------------------------------


def test_orm_lookups():
    # array_contains
    sql, params = resolve_lookup("tags__array_contains", [1, 2])
    check("orm array_contains sql", "@>" in sql)
    check("orm array_contains params", params == [[1, 2]])

    # array_contained_by
    sql2, params2 = resolve_lookup("tags__array_contained_by", [1, 2, 3])
    check("orm array_contained_by sql", "<@" in sql2)

    # array_overlap
    sql3, params3 = resolve_lookup("tags__array_overlap", [1, 2])
    check("orm array_overlap sql", "&&" in sql3)

    # array_len
    sql4, params4 = resolve_lookup("tags__array_len", 3)
    check("orm array_len sql", "array_length" in sql4)
    check("orm array_len params", params4 == [3])

    # trigram_similar
    sql5, params5 = resolve_lookup("name__trigram_similar", "test")
    check("orm trigram_similar sql", "%" in sql5)
    check("orm trigram_similar params", params5 == ["test"])

    # trigram_word_similar
    sql6, params6 = resolve_lookup("name__trigram_word_similar", "test")
    check("orm trigram_word_similar sql", "%>" in sql6)

    # search
    sql7, params7 = resolve_lookup("body__search", "hello world")
    check("orm search sql", "to_tsvector" in sql7 and "@@" in sql7)
    check("orm search params", params7 == ["hello world"])

    # has_key
    sql8, params8 = resolve_lookup("data__has_key", "foo")
    check("orm has_key sql", "?" in sql8)
    check("orm has_key params", params8 == ["foo"])

    # has_keys
    sql9, params9 = resolve_lookup("data__has_keys", ["a", "b"])
    check("orm has_keys sql", "?&" in sql9)

    # has_any_keys
    sql10, params10 = resolve_lookup("data__has_any_keys", ["x", "y"])
    check("orm has_any_keys sql", "?|" in sql10)


# ---------------------------------------------------------------------------
# Slots Verification
# ---------------------------------------------------------------------------


def test_slots():
    classes_to_check = [
        ArrayField,
        HStoreField,
        JSONBField,
        SearchVector,
        SearchQuery,
        SearchRank,
        SearchHeadline,
        TrigramSimilarity,
        TrigramDistance,
        TrigramWordSimilarity,
        TrigramWordDistance,
        ArrayContains,
        ArrayContainedBy,
        ArrayOverlap,
        ArrayLength,
        ArrayIndex,
        ArrayRemove,
        ArrayAppend,
        ArrayPrepend,
        ArrayCat,
        ArrayPosition,
        Unnest,
        ArrayAgg,
        JSONBAgg,
        StringAgg,
        BitAnd,
        BitOr,
        BoolAnd,
        BoolOr,
        IntegerRange,
        BigIntegerRange,
        DecimalRange,
        DateRange,
        DateTimeRange,
        RangeContains,
        RangeContainedBy,
        RangeOverlap,
        RangeFullyLessThan,
        RangeFullyGreaterThan,
        RangeAdjacentTo,
        ExclusionConstraint,
        GinIndex,
        GistIndex,
        BrinIndex,
        HashIndex,
        SpGistIndex,
        BTreeIndex,
    ]
    for cls in classes_to_check:
        has_slots = "__slots__" in cls.__dict__ or (
            dataclasses.is_dataclass(cls) and "__slots__" in dir(cls)
        )
        check(
            f"{cls.__name__} has slots", has_slots, f"{cls.__name__} missing __slots__"
        )


if __name__ == "__main__":
    run_main(main)
