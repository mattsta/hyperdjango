"""
HyperGuard SQL generator — translates policy conditions to parameterized WHERE clauses.

Converts FieldConditionAST and CrossFieldConditionAST from parsed .guard policies
into SQL fragments that can be injected into QuerySet via where_raw().

This is the key differentiator vs Cedar/OPA/Oso — those systems only give allow/deny
decisions. HyperGuard generates the WHERE clause, so listing pages enforce the same
rules as single-object checks automatically.

Usage:
    from hyperdjango.guard.sql import generate_where, SQLFragment

    fragment = generate_where(compiled_resource, "read", user_id=42)
    # fragment.sql = "(is_public = $1) OR (forum_id IN (SELECT ...))"
    # fragment.params = [True]

    qs = Post.objects.where_raw(fragment.sql, *fragment.params)
"""

from dataclasses import dataclass

from hyperdjango.guard.compiler import (
    _OP_AND,
    _OP_CMP_EQ,
    _OP_CMP_GE,
    _OP_CMP_GT,
    _OP_CMP_LE,
    _OP_CMP_LT,
    _OP_CMP_NE,
    _OP_LOAD_C,
    _OP_LOAD_R,
    _OP_LOAD_U,
    _OP_OR,
    _OP_YIELD,
)
from hyperdjango.guard.parser import RuleEffect
from hyperdjango.guard.registry import CompiledResource, CompiledRule


@dataclass(frozen=True)
class SQLFragment:
    """Generated SQL WHERE fragment with parameters.

    sql uses {idx} placeholders (compatible with QuerySet.where_raw).
    params are the bound parameter values.
    """

    sql: str
    params: tuple[object, ...]

    @property
    def is_empty(self) -> bool:
        return not self.sql


_SCALAR_TYPES = (int, bool, str, float)


def generate_where(
    resource: CompiledResource,
    action: str,
    *,
    user_fields: dict[str, object] | None = None,
    table_name: str = "",
) -> SQLFragment:
    """Generate a WHERE clause from a compiled resource's rules for an action.

    Only processes rules that are fully bytecode-compilable (no needs_python).
    Deny rules become NOT(...) fragments. Allow rules become OR-joined alternatives.
    User fields (like user.id) are inlined as parameter values.

    Args:
        resource: CompiledResource from PolicyRegistry.
        action: The action to filter for (e.g., "read", "write_post").
        user_fields: Dict of user attributes to inline as parameters.
                     Required for conditions that reference user.* fields.
        table_name: Optional table prefix for column names (e.g., "hn_forums").

    Returns:
        SQLFragment with parameterized WHERE clause.
    """
    if user_fields is None:
        user_fields = {}

    rules = [r for r in resource.rules if r.action == action]
    if not rules:
        return SQLFragment(sql="FALSE", params=())  # No rules = deny all

    deny_fragments: list[str] = []
    deny_params: list[object] = []
    allow_fragments: list[str] = []
    allow_params: list[object] = []

    for rule in rules:
        if rule.needs_python:
            # Cannot generate SQL for relation/cross-resource conditions
            if rule.effect == RuleEffect.DENY:
                # Deny with needs_python → can't safely allow anything
                return SQLFragment(sql="FALSE", params=())
            # Allow with needs_python → skip (won't be in SQL filter)
            continue

        fragment, params = _rule_to_sql(rule, user_fields, table_name)
        if not fragment:
            continue

        if rule.effect == RuleEffect.DENY:
            deny_fragments.append(fragment)
            deny_params.extend(params)
        else:
            allow_fragments.append(fragment)
            allow_params.extend(params)

    if not allow_fragments:
        return SQLFragment(sql="FALSE", params=())  # No allow rules = deny all

    # Build: (allow1 OR allow2) AND NOT (deny1) AND NOT (deny2)
    parts: list[str] = []
    all_params: list[object] = []

    if len(allow_fragments) == 1:
        parts.append(allow_fragments[0])
    else:
        parts.append("(" + " OR ".join(allow_fragments) + ")")
    all_params.extend(allow_params)

    for deny_frag in deny_fragments:
        parts.append(f"NOT ({deny_frag})")
    all_params.extend(deny_params)

    sql = " AND ".join(parts)
    return SQLFragment(sql=sql, params=tuple(all_params))


def _rule_to_sql(
    rule: CompiledRule,
    user_fields: dict[str, object],
    table_prefix: str,
) -> tuple[str, list[object]]:
    """Convert a single compiled rule's source conditions to SQL.

    Returns (sql_fragment, params) or ("", []) if not convertible.
    """
    # We need the original AST conditions to generate SQL.
    # The CompiledRule stores or_indices from the AST, but we need the
    # actual field names and values. Since compiled rules have a CompiledGuard
    # with field_names and constants, we can reconstruct from that.
    compiled = rule.compiled
    if compiled is None:
        return "", []

    # Walk the conditions from the CompiledGuard metadata
    # field_names tells us which fields are referenced
    # constants tells us what values to compare against
    # bytecode tells us the comparison operations
    #
    # Instead of reverse-engineering bytecode, we reconstruct from the
    # original Condition objects that were compiled. The registry stores
    # the AST, but the CompiledRule doesn't carry the original conditions.
    #
    # Better approach: generate SQL directly from the policy AST conditions
    # during compilation, stored alongside the bytecode.
    #
    # For now, we use the compiled guard's field_names + constants + bytecode
    # structure to reconstruct the SQL. The bytecode is a sequence of:
    # LOAD_x idx, LOAD_x idx, CMP_x, [AND/OR], ..., YIELD

    return _bytecode_to_sql(
        compiled.bytecode,
        compiled.field_names,
        compiled.constants,
        user_fields,
        table_prefix,
    )


def _bytecode_to_sql(
    bytecode: bytes,
    field_names: tuple[str, ...],
    constants: tuple[object, ...],
    user_fields: dict[str, object],
    table_prefix: str,
) -> tuple[str, list[object]]:
    """Reverse-engineer bytecode into SQL conditions.

    The bytecode has a predictable structure from compile_conditions():
    For each condition: LOAD_x field_idx, LOAD_x const_idx, CMP_x
    Then: AND/OR between conditions, YIELD at the end.

    We walk the bytecode and reconstruct the SQL from the structure.
    """
    _CMP_SQL = {
        _OP_CMP_EQ: "=",
        _OP_CMP_NE: "!=",
        _OP_CMP_GT: ">",
        _OP_CMP_GE: ">=",
        _OP_CMP_LT: "<",
        _OP_CMP_LE: "<=",
    }

    conditions: list[str] = []
    params: list[object] = []
    combine = "AND"

    ip = 0
    load_stack: list[tuple[str, int]] = []  # (source_type, index)

    while ip + 3 <= len(bytecode):
        opcode = bytecode[ip]
        arg = bytecode[ip + 1] | (bytecode[ip + 2] << 8)
        ip += 3

        if opcode in (_OP_LOAD_U, _OP_LOAD_R, _OP_LOAD_C):
            load_stack.append(
                (
                    "user"
                    if opcode == _OP_LOAD_U
                    else "resource"
                    if opcode == _OP_LOAD_R
                    else "const",
                    arg,
                )
            )

        elif opcode in _CMP_SQL:
            if len(load_stack) < 2:
                return "", []  # Malformed
            right = load_stack.pop()
            left = load_stack.pop()
            sql_op = _CMP_SQL[opcode]

            sql_cond, cond_params = _operands_to_sql(
                left, right, sql_op, field_names, constants, user_fields, table_prefix
            )
            if sql_cond:
                conditions.append(sql_cond)
                params.extend(cond_params)

        elif opcode == _OP_AND:
            combine = "AND"
        elif opcode == _OP_OR:
            combine = "OR"
        elif opcode == _OP_YIELD:
            break

    if not conditions:
        return "", []

    sql = f" {combine} ".join(conditions)
    if len(conditions) > 1:
        sql = f"({sql})"
    return sql, params


def _operands_to_sql(
    left: tuple[str, int],
    right: tuple[str, int],
    sql_op: str,
    field_names: tuple[str, ...],
    constants: tuple[object, ...],
    user_fields: dict[str, object],
    table_prefix: str,
) -> tuple[str, list[object]]:
    """Convert two stack operands and an operator into a SQL condition fragment."""
    left_sql, left_params = _operand_to_sql(
        left, field_names, constants, user_fields, table_prefix
    )
    right_sql, right_params = _operand_to_sql(
        right, field_names, constants, user_fields, table_prefix
    )

    if not left_sql or not right_sql:
        return "", []

    return f"{left_sql} {sql_op} {right_sql}", left_params + right_params


def _operand_to_sql(
    operand: tuple[str, int],
    field_names: tuple[str, ...],
    constants: tuple[object, ...],
    user_fields: dict[str, object],
    table_prefix: str,
) -> tuple[str, list[object]]:
    """Convert a single operand to SQL expression + params."""
    source, idx = operand

    if source == "resource":
        # Resource field → quoted column name (safe against reserved words)
        field_name = field_names[idx]
        col = f'"{table_prefix}"."{field_name}"' if table_prefix else f'"{field_name}"'
        return col, []

    if source == "const":
        # Constant → parameterized value
        value = constants[idx]
        return "{idx}", [value]

    if source == "user":
        # User field → parameterized value from user_fields dict
        field_name = field_names[idx]
        value = user_fields.get(field_name)
        if value is None:
            return "NULL", []
        if not isinstance(value, _SCALAR_TYPES):
            return "NULL", []  # Non-scalar values can't be SQL params
        return "{idx}", [value]

    return "", []
