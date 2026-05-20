"""
HyperGuard bytecode compiler — translates condition specs to Zig-evaluable bytecode.

The compiler takes a list of Condition/CrossFieldCondition objects and emits a compact
bytecode program that the Zig evaluator (guard_eval.zig) can execute in sub-microsecond.

The Zig VM supports int, bool, string, and none value types natively via a tagged
Value union. String comparisons use zero-copy borrowed pointers from Python's
internal UTF-8 buffers. Conditions requiring DB queries (relation checks) stay in
Python — only field comparisons are compiled to bytecode.

Usage:
    from hyperdjango.guard.compiler import compile_conditions, CompiledGuard, Condition, CondOp, CondSource

    conditions = [
        Condition(source=CondSource.USER, field="is_staff", op=CondOp.EQ, value=True),
        Condition(source=CondSource.RESOURCE, field="is_archived", op=CondOp.EQ, value=False),
    ]
    compiled = compile_conditions(conditions, combine="and")
    result = compiled.evaluate(user_dict, resource_dict)  # True/False
"""

from dataclasses import dataclass
from enum import Enum

from hyperdjango._hyperdjango_native import _guard_evaluate


class CondOp(Enum):
    """Comparison operators for guard conditions."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"


class CondSource(Enum):
    """Where a condition's field comes from."""

    USER = "user"
    RESOURCE = "resource"


# Bytecode opcodes — must match guard_eval.zig Op enum
_OP_LOAD_U: int = 0x01
_OP_LOAD_R: int = 0x02
_OP_LOAD_C: int = 0x03
_OP_CMP_EQ: int = 0x10
_OP_CMP_NE: int = 0x11
_OP_CMP_GT: int = 0x12
_OP_CMP_GE: int = 0x13
_OP_CMP_LT: int = 0x14
_OP_CMP_LE: int = 0x15
_OP_AND: int = 0x20
_OP_OR: int = 0x21
_OP_NOT: int = 0x22
_OP_YIELD: int = 0xFF

_CMP_MAP: dict[CondOp, int] = {
    CondOp.EQ: _OP_CMP_EQ,
    CondOp.NE: _OP_CMP_NE,
    CondOp.GT: _OP_CMP_GT,
    CondOp.GE: _OP_CMP_GE,
    CondOp.LT: _OP_CMP_LT,
    CondOp.LE: _OP_CMP_LE,
}


@dataclass(frozen=True)
class Condition:
    """Single condition in a guard policy.

    Compares a field from user or resource against a constant value.
    """

    source: CondSource  # USER or RESOURCE
    field: str  # Field name (e.g., "is_staff", "author_id")
    op: CondOp  # Comparison operator
    value: int | bool | str  # Constant to compare against


@dataclass(frozen=True)
class CrossFieldCondition:
    """Condition comparing a resource field against a user field.

    Example: resource.author_id == user.id
    """

    resource_field: str
    op: CondOp
    user_field: str


@dataclass(frozen=True)
class CompiledGuard:
    """Compiled bytecode + metadata for Zig-accelerated evaluation.

    Created by compile_conditions(). Immutable after creation.
    Call evaluate(user_dict, resource_dict) for fast allow/deny check.
    """

    bytecode: bytes
    field_names: tuple[str, ...]
    constants: tuple[int | bool | str, ...]
    condition_count: int

    def evaluate(
        self, user_dict: dict[str, object], resource_dict: dict[str, object]
    ) -> bool:
        """Evaluate compiled conditions against user and resource dicts.

        Calls into Zig native evaluator for sub-50ns execution.
        Returns True (allow) or False (deny).
        """
        return _guard_evaluate(
            self.bytecode,
            user_dict,
            resource_dict,
            self.field_names,
            self.constants,
        )


class CombineMode(Enum):
    """How to combine multiple conditions in a compiled guard."""

    AND = "and"
    OR = "or"


def compile_conditions(
    conditions: list[Condition | CrossFieldCondition],
    *,
    combine: CombineMode | str = CombineMode.AND,
) -> CompiledGuard:
    """Compile a list of conditions into Zig-evaluable bytecode.

    Args:
        conditions: List of Condition or CrossFieldCondition objects.
        combine: How to combine multiple conditions — CombineMode.AND or CombineMode.OR.
                 Also accepts "and"/"or" strings for convenience.

    Returns:
        CompiledGuard with bytecode ready for Zig evaluation.
    """
    # Normalize combine to string for internal use
    if isinstance(combine, CombineMode):
        combine = combine.value

    if not conditions:
        # Empty conditions = always allow
        bytecode = bytes([_OP_LOAD_C, 0, 0, _OP_YIELD, 0, 0])
        return CompiledGuard(
            bytecode=bytecode,
            field_names=(),
            constants=(1,),
            condition_count=0,
        )

    # Build field name index and constant pool
    field_index: dict[str, int] = {}
    constant_pool: list[int | bool | str] = []
    # Dedup key: (type_name, value) to distinguish e.g. int 1 vs bool True vs str "1"
    constant_index: dict[tuple[str, int | bool | str], int] = {}

    def get_field_idx(name: str) -> int:
        if name not in field_index:
            field_index[name] = len(field_index)
        return field_index[name]

    def get_const_idx(value: int | bool | str) -> int:
        key = (type(value).__name__, value)
        if key not in constant_index:
            constant_index[key] = len(constant_pool)
            constant_pool.append(value)
        return constant_index[key]

    # Emit bytecode
    bc = bytearray()

    def emit(opcode: int, arg: int = 0) -> None:
        bc.append(opcode)
        bc.append(arg & 0xFF)
        bc.append((arg >> 8) & 0xFF)

    combine_op = _OP_AND if combine == "and" else _OP_OR

    for i, cond in enumerate(conditions):
        if isinstance(cond, CrossFieldCondition):
            # resource_field op user_field
            r_idx = get_field_idx(cond.resource_field)
            u_idx = get_field_idx(cond.user_field)
            emit(_OP_LOAD_R, r_idx)
            emit(_OP_LOAD_U, u_idx)
            emit(_CMP_MAP[cond.op])
        else:
            # source.field op constant
            f_idx = get_field_idx(cond.field)
            c_idx = get_const_idx(cond.value)
            load_op = _OP_LOAD_U if cond.source == CondSource.USER else _OP_LOAD_R
            emit(load_op, f_idx)
            emit(_OP_LOAD_C, c_idx)
            emit(_CMP_MAP[cond.op])

        # Combine with previous condition result
        if i > 0:
            emit(combine_op)

    emit(_OP_YIELD)

    return CompiledGuard(
        bytecode=bytes(bc),
        field_names=tuple(field_index),
        constants=tuple(constant_pool),
        condition_count=len(conditions),
    )
