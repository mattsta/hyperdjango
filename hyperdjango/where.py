"""
WhereNode — composable WHERE clause tree.

Separates SQL template structure from bind parameter values, enabling:
1. Composable mixin WHERE injection via tree nodes (not string concatenation)
2. Structural cache keys for compiled query caching
3. Independent param collection for cache hit fast-path

The tree holds SQL template fragments with {} placeholders for bind values.
During compilation, placeholders are renumbered to $1, $2, ... in order.

Architecture:
    WhereNode
      ├── template: str       # Leaf: "name = {}" or "is_deleted = FALSE"
      ├── bind_values: list   # Leaf: [value] or [low, high] or []
      ├── children: list      # Branch: child WhereNodes
      ├── connector: str      # Branch: "AND" or "OR"
      └── negated: bool       # Wraps output in NOT(...)

    compile(start_idx) flattens the tree:
      {} → $1, $2, $3... in declaration order
      bind_values collected in same order

    cache_key() returns structural fingerprint (excludes values)

Performance:
    compile() uses split/join (O(n) single-pass, not O(n*m) replace loop)
    collect_bind_values() uses in-place append (no intermediate list copies)
"""

from dataclasses import dataclass, field

from hyperdjango._hyperdjango_native import _where_compile as _native_compile

# SQL bind parameter value — any type pg.zig can serialize to PostgreSQL wire format.
# Covers: str, int, float, bool, None, bytes, list (→ PG array), dict (→ JSONB),
# datetime, date, Decimal, UUID, and enum values. Using object because the full
# union of 30+ pg.zig-supported OID types is impractical to enumerate.
type BindValue = object

# Sentinel param index used by Lookup.to_node() to generate templates.
# Chosen to never collide with real param indices (max realistic ~1000).
SENTINEL_IDX = 900_000


# Lookups where bind_params(value) returns [value] unchanged.
# Used by fast-path param collection to skip resolve_bind_params call.
PASSTHROUGH_SUFFIXES = frozenset(
    {"gt", "gte", "lt", "lte", "iexact", "regex", "iregex"}
)


def value_shape(value: object) -> int:
    """Classify a filter value by how it affects SQL template shape.

    Returns an int that distinguishes values that produce different SQL:
    - None: IS NULL (no params) → 0
    - True: IS NULL for isnull lookup → 1
    - False: IS NOT NULL for isnull lookup → 2
    - Empty collection: FALSE for in lookup → 3
    - Non-empty collection, no None: `col = ANY($n)` → 4
    - Non-empty collection, some (not all) None: `(= ANY($n) OR IS NULL)` → 5
    - Non-empty collection, all None: `col IS NULL` (no params) → 6
    - Everything else (scalars): standard $N parameterized → 4

    ⚠ LOCKSTEP INVARIANT: this function and Zig's valueShape() (in
    zig/src/where_compiler.zig) MUST return identical shape codes for every
    value. They key two halves of the SAME compiled-SQL cache. The __in lookup
    (lookups.py InLookup.to_node/as_sql) emits three different SQL templates
    with different bind-param counts based on None content; codes 4/5/6 keep
    those three variants in distinct cache buckets so a cache hit never reuses a
    template with the wrong `OR IS NULL` demotion or the wrong param count.
    Change one function ⇒ change the other.
    """
    if value is None:
        return 0
    if value is True:
        return 1
    if value is False:
        return 2
    if isinstance(value, (list, tuple, set, frozenset)):
        n = len(value)
        if n == 0:
            return 3
        none_count = sum(1 for v in value if v is None)
        if none_count == 0:
            return 4
        if none_count == n:
            return 6
        return 5
    return 4


@dataclass(slots=True)
class WhereNode:
    """Composable WHERE clause node.

    Leaf nodes hold a single SQL condition template with {} placeholders.
    Branch nodes compose children with AND/OR connectors.

    Compilation flattens the tree: {} placeholders become $1, $2, $3...
    and bind values are collected in the same order.
    """

    # Leaf node: single SQL condition
    template: str = ""
    bind_values: list[BindValue] = field(default_factory=list)

    # Branch node: composition
    children: list[WhereNode] = field(default_factory=list)
    connector: str = "AND"
    negated: bool = False

    @property
    def is_empty(self) -> bool:
        """True if this node produces no SQL."""
        return not self.template and not self.children

    def compile(self, start_idx: int = 1) -> tuple[str, list[BindValue], int]:
        """Flatten this node tree into (sql, params, next_idx).

        Replaces {} placeholders with $1, $2, ... in declaration order.
        Returns the SQL fragment, collected params, and next available index.

        Implementation: Native Zig single-pass walk via `_where_compile` FFI call,
        with cached interned attribute-name PyObjects. Verified parity with the
        prior Python implementation via 78 unit tests + 600+ hypothesis-generated
        random trees + 8 SQL injection vectors. Thread-safe under Python 3.14t
        free-threading (8 threads × 1000 iterations, zero races).

        Performance: 1.26-1.28x faster at the micro-benchmark level on branch
        nodes; no measurable impact on production request throughput because
        compile() is ~2μs of a ~500-3000μs total request. See
        logs/zig_where_compile_bench_report.md for before/after data.
        """
        return _native_compile(self, start_idx)

    def cache_key(self) -> tuple:
        """Structural fingerprint — excludes bind values.

        Two WhereNodes with the same cache_key produce identical SQL templates.
        """
        if self.children:
            return (
                self.connector,
                self.negated,
                tuple(c.cache_key() for c in self.children),
            )
        return (self.template, self.negated, len(self.bind_values))

    def collect_bind_values(
        self, target: list[BindValue] | None = None
    ) -> list[BindValue]:
        """Extract all bind values from tree in compilation order.

        Uses in-place append to a single target list to avoid O(n^2)
        intermediate list allocations from recursive extend() calls.
        """
        if target is None:
            target = []

        if self.template:
            target.extend(self.bind_values)
        else:
            for child in self.children:
                child.collect_bind_values(target)

        return target
