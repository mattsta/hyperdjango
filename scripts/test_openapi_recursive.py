"""
Tests for OpenAPI 3.1 schema generation against recursive / self-referential
and cyclic serializers (task D5).

Before the recursion guard, `serializer_to_schema` only checked
`nested_name not in schemas` before recursing, with no placeholder
pre-inserted — so a serializer that nests itself (NodeSerializer with a
`child: NodeSerializer` field) or a cycle (A→B→A) recursed forever and
raised RecursionError. These tests assert the fix terminates, produces a
valid spec, and that every emitted `$ref` resolves to a real component.
"""

# hyper-test: unit

import json
import sys

from hyperdjango.app import HyperApp
from hyperdjango.openapi import (
    api_output,
    generate_openapi,
    serializer_to_schema,
)
from hyperdjango.serializers import Serializer, SerializerField

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        msg = f"  FAIL: {name}"
        if detail:
            msg += f" — {detail}"
        errors.append(msg)
        print(msg)


def collect_refs(node: object, out: list[str]) -> None:
    """Recursively gather every '$ref' target string in a schema tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                out.append(value)
            else:
                collect_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            collect_refs(item, out)


# ── Direct self-reference (NodeSerializer.child: NodeSerializer) ────────────

print("=== Self-referential serializer ===")


class NodeSerializer(Serializer):
    value: int = SerializerField()
    # Placeholder annotation so the field exists; the real recursive type is
    # patched in after class creation (the class isn't bound during its own
    # body). This is the canonical "recursive field" construction.
    child: int = SerializerField(required=False)


# Make `child` a self-reference: NodeSerializer nested inside NodeSerializer.
NodeSerializer._serializer_fields["child"].field_type = NodeSerializer

schemas: dict[str, dict[str, object]] = {}
# Must NOT raise RecursionError.
node_schema = serializer_to_schema(NodeSerializer, mode="output", schemas=schemas)

check("selfref_completes", True, "no RecursionError raised")
# Self-referential root returns a root $ref pointing at the stored component.
# Components are mode-scoped, so an output-mode call yields "…Output".
check(
    "selfref_returns_ref",
    node_schema == {"$ref": "#/components/schemas/NodeSerializerOutput"},
    detail=repr(node_schema),
)
check("selfref_component_stored", "NodeSerializerOutput" in schemas)
real = schemas.get("NodeSerializerOutput", {})
check(
    "selfref_component_is_object",
    isinstance(real, dict) and real.get("type") == "object",
    detail=repr(real),
)
props = real.get("properties", {}) if isinstance(real, dict) else {}
check("selfref_has_value", "value" in props)
check("selfref_has_child", "child" in props)
check(
    "selfref_child_is_ref",
    props.get("child") == {"$ref": "#/components/schemas/NodeSerializerOutput"},
    detail=repr(props.get("child")),
)
# The stored component must never be the empty placeholder.
check(
    "selfref_no_empty_placeholder",
    real != {},
    detail="component is empty placeholder",
)

# Every $ref in the produced schemas must resolve to a real component.
refs: list[str] = []
collect_refs(node_schema, refs)
collect_refs(schemas, refs)
all_resolve = all(
    ref.startswith("#/components/schemas/")
    and ref.rsplit("/", 1)[-1] in schemas
    and schemas[ref.rsplit("/", 1)[-1]] != {}
    for ref in refs
)
check("selfref_all_refs_resolve", all_resolve, detail=f"refs={refs}")


# ── Optional self-reference (parent: NodeSerializer | None style) ───────────

print("\n=== Optional self-reference ===")


class TreeSerializer(Serializer):
    name: str = SerializerField()
    parent: int = SerializerField(required=False, default=None)


# Optional self-reference: parent points back at TreeSerializer.
TreeSerializer._serializer_fields["parent"].field_type = TreeSerializer

opt_schemas: dict[str, dict[str, object]] = {}
tree_schema = serializer_to_schema(TreeSerializer, mode="output", schemas=opt_schemas)
check("optref_completes", True, "no RecursionError raised")
check(
    "optref_returns_ref",
    tree_schema == {"$ref": "#/components/schemas/TreeSerializerOutput"},
    detail=repr(tree_schema),
)
tree_real = opt_schemas.get("TreeSerializerOutput", {})
tree_props = tree_real.get("properties", {}) if isinstance(tree_real, dict) else {}
check("optref_has_name", "name" in tree_props)
check(
    "optref_parent_is_ref",
    tree_props.get("parent") == {"$ref": "#/components/schemas/TreeSerializerOutput"},
    detail=repr(tree_props.get("parent")),
)


# ── Mutual recursion cycle A → B → A ────────────────────────────────────────

print("\n=== Mutual recursion (A → B → A) ===")


class BSerializer(Serializer):
    label: str = SerializerField()
    a_ref: int = SerializerField(required=False)


class ASerializer(Serializer):
    title: str = SerializerField()
    b_ref: BSerializer = SerializerField(required=False)


# Close the cycle: B.a_ref → A.
BSerializer._serializer_fields["a_ref"].field_type = ASerializer

cyc_schemas: dict[str, dict[str, object]] = {}
a_schema = serializer_to_schema(ASerializer, mode="output", schemas=cyc_schemas)
check("cycle_completes", True, "no RecursionError raised")
# A is the root and is reachable from B → A must be returned as a $ref.
check(
    "cycle_a_returns_ref",
    a_schema == {"$ref": "#/components/schemas/ASerializerOutput"},
    detail=repr(a_schema),
)
check("cycle_a_component", "ASerializerOutput" in cyc_schemas)
check("cycle_b_component", "BSerializerOutput" in cyc_schemas)
a_real = cyc_schemas.get("ASerializerOutput", {})
b_real = cyc_schemas.get("BSerializerOutput", {})
check(
    "cycle_a_b_ref",
    a_real.get("properties", {}).get("b_ref")
    == {"$ref": "#/components/schemas/BSerializerOutput"},
    detail=repr(a_real),
)
check(
    "cycle_b_a_ref",
    b_real.get("properties", {}).get("a_ref")
    == {"$ref": "#/components/schemas/ASerializerOutput"},
    detail=repr(b_real),
)
# No empty placeholders left behind anywhere.
check(
    "cycle_no_empty_components",
    all(v != {} for v in cyc_schemas.values()),
    detail=repr(cyc_schemas),
)
cyc_refs: list[str] = []
collect_refs(cyc_schemas, cyc_refs)
check(
    "cycle_all_refs_resolve",
    all(
        r.rsplit("/", 1)[-1] in cyc_schemas and cyc_schemas[r.rsplit("/", 1)[-1]] != {}
        for r in cyc_refs
    ),
    detail=f"refs={cyc_refs}",
)


# ── Non-recursive output must be unchanged (regression guard) ───────────────

print("\n=== Non-recursive output unchanged ===")


class PlainSerializer(Serializer):
    id: int = SerializerField(read_only=True)
    name: str = SerializerField(max_length=100)


class WrapperSerializer(Serializer):
    title: str = SerializerField()
    plain: PlainSerializer = SerializerField(read_only=True)


plain_schemas: dict[str, dict[str, object]] = {}
wrapper_schema = serializer_to_schema(
    WrapperSerializer, mode="output", schemas=plain_schemas
)
# Non-recursive root still returns the INLINE schema (not a $ref).
check(
    "plain_inline_returned",
    wrapper_schema.get("type") == "object" and "properties" in wrapper_schema,
    detail=repr(wrapper_schema),
)
check(
    "plain_nested_ref",
    wrapper_schema["properties"].get("plain")
    == {"$ref": "#/components/schemas/PlainSerializerOutput"},
)
check("plain_nested_component", "PlainSerializerOutput" in plain_schemas)
# The non-recursive root name must NOT leak into schemas (no stale placeholder).
check(
    "plain_root_not_in_schemas",
    "WrapperSerializerOutput" not in plain_schemas,
    detail=repr(list(plain_schemas)),
)
check(
    "plain_no_empty_components",
    all(v != {} for v in plain_schemas.values()),
    detail=repr(plain_schemas),
)

# A bare non-recursive serializer with no schemas dict is fully inline.
bare = serializer_to_schema(PlainSerializer, mode="output")
check("bare_inline", bare.get("type") == "object")
check("bare_props", set(bare["properties"]) == {"id", "name"})


# ── Full generate_openapi() with a recursive response serializer ────────────

print("\n=== generate_openapi with recursive serializer ===")

app = HyperApp(title="Recursive API")


@app.get("/nodes/{id:int}")
@api_output(NodeSerializer)
async def get_node(request, id: int):
    return None


spec = generate_openapi(app, version="9.9.9")
check("spec_completes", True, "generate_openapi did not raise RecursionError")
check("spec_openapi_version", spec.get("openapi") == "3.1.0")
spec_schemas = spec.get("components", {}).get("schemas", {})
# The recursive response serializer is stored as its mode-scoped component.
check("spec_node_output_component", "NodeSerializerOutput" in spec_schemas)
node_out = spec_schemas.get("NodeSerializerOutput", {})
check(
    "spec_output_component_is_object",
    isinstance(node_out, dict)
    and node_out.get("type") == "object"
    and "child" in node_out.get("properties", {}),
    detail=repr(node_out),
)
# The recursive self-reference points back at the same mode-scoped component.
check(
    "spec_output_child_selfref",
    node_out.get("properties", {}).get("child")
    == {"$ref": "#/components/schemas/NodeSerializerOutput"},
    detail=repr(node_out.get("properties", {}).get("child")),
)
# Every $ref across the whole spec must resolve to a non-empty component.
spec_refs: list[str] = []
collect_refs(spec, spec_refs)
unresolved = [
    r
    for r in spec_refs
    if r.startswith("#/components/schemas/")
    and (
        r.rsplit("/", 1)[-1] not in spec_schemas
        or spec_schemas[r.rsplit("/", 1)[-1]] == {}
    )
]
check(
    "spec_all_refs_resolve",
    not unresolved,
    detail=f"unresolved={unresolved}",
)
# Spec must be JSON-serializable (no cycles, no non-serializable objects).
try:
    json.dumps(spec)
    json_ok = True
except (TypeError, ValueError) as exc:
    json_ok = False
    errors.append(f"  FAIL: spec_json_serializable — {exc}")
    failed += 1
if json_ok:
    passed += 1


# ── Summary ────────────────────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print(f"OpenAPI recursive tests: {passed} passed, {failed} failed")
if errors:
    print("\nFailures:")
    for e in errors:
        print(e)
print(f"{'=' * 60}")

sys.exit(0 if failed == 0 else 1)
