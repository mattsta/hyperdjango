"""
HyperGuard policy registry — loads, validates, and compiles .guard policies.

The registry is the runtime store of compiled policies. At app startup:
1. Parse .guard files from a directory
2. Validate all policies (cross-file references, semantic checks)
3. Compile simple conditions to Zig bytecode
4. Register compiled policies for @guard("resource.action") lookup

Usage:
    from hyperdjango.guard.registry import PolicyRegistry

    registry = PolicyRegistry()
    registry.load_directory("policies/")
    compiled = registry.get("Forum", "read")  # -> CompiledGuard or None
"""

from dataclasses import dataclass, field
from pathlib import Path

from hyperdjango.guard.compiler import (
    CombineMode,
    CompiledGuard,
    Condition,
    CondOp,
    CondSource,
    CrossFieldCondition,
    compile_conditions,
)
from hyperdjango.guard.parser import (
    ConditionOp,
    CrossFieldConditionAST,
    FieldConditionAST,
    PolicyAST,
    RelationConditionAST,
    ResourceAST,
    ResourceRefConditionAST,
    RuleAST,
    RuleEffect,
    parse_policy,
    parse_policy_file,
)
from hyperdjango.guard.validator import validate_policies
from hyperdjango.logging import logger

_OP_MAP: dict[ConditionOp, CondOp] = {
    ConditionOp.EQ: CondOp.EQ,
    ConditionOp.NE: CondOp.NE,
    ConditionOp.GT: CondOp.GT,
    ConditionOp.GE: CondOp.GE,
    ConditionOp.LT: CondOp.LT,
    ConditionOp.LE: CondOp.LE,
}

_SOURCE_MAP: dict[str, CondSource] = {
    "user": CondSource.USER,
    "resource": CondSource.RESOURCE,
}

# When flipping cross-field operands (user.x > resource.y → resource.y < user.x),
# ordered comparison operators must be reversed. Symmetric ops (EQ, NE) stay the same.
_FLIPPED_OPS: dict[CondOp, CondOp] = {
    CondOp.EQ: CondOp.EQ,
    CondOp.NE: CondOp.NE,
    CondOp.GT: CondOp.LT,
    CondOp.GE: CondOp.LE,
    CondOp.LT: CondOp.GT,
    CondOp.LE: CondOp.GE,
}


def _flip_op(op: CondOp) -> CondOp:
    """Flip a comparison operator when swapping operand sides."""
    return _FLIPPED_OPS[op]


@dataclass(frozen=True)
class CompiledRule:
    """A single compiled allow/deny rule.

    Simple conditions are compiled to Zig bytecode.
    Rules with relation checks or cross-resource refs are flagged as needs_python.
    """

    action: str
    effect: RuleEffect
    compiled: CompiledGuard | None  # None if needs_python
    needs_python: bool
    python_conditions: tuple[RelationConditionAST | ResourceRefConditionAST, ...]
    or_indices: frozenset[int]


@dataclass(frozen=True)
class CompiledResource:
    """All compiled rules for a single resource type."""

    name: str
    rules: tuple[CompiledRule, ...]
    actions: frozenset[str]  # All defined actions


@dataclass
class PolicyRegistry:
    """Runtime store of compiled guard policies.

    Thread-safe for reads after initial load (all compilation happens at startup).
    """

    _resources: dict[str, CompiledResource] = field(default_factory=dict)
    _policies: list[PolicyAST] = field(default_factory=list)

    def load_directory(self, directory: str) -> None:
        """Load all .guard files from a directory.

        Parses, validates, and compiles all policies.
        Raises ValueError on validation errors.
        """
        dir_path = Path(directory)
        if not dir_path.is_dir():
            raise FileNotFoundError(f"Policy directory not found: {directory}")

        guard_files = sorted(dir_path.glob("*.guard"))
        if not guard_files:
            logger.warning(f"[GUARD] No .guard files found in {directory}")
            return

        policies: list[PolicyAST] = []
        for guard_file in guard_files:
            policy = parse_policy_file(str(guard_file))
            policies.append(policy)
            logger.info(
                f"[GUARD] Parsed {guard_file.name}: {len(policy.resources)} resources"
            )

        # Validate all policies together
        result = validate_policies(policies)
        result.raise_if_invalid()

        # Compile and register
        for policy in policies:
            self._policies.append(policy)
            for resource_ast in policy.resources:
                compiled_resource = _compile_resource(resource_ast)
                self._resources[resource_ast.name] = compiled_resource
                zig_count = sum(
                    1 for r in compiled_resource.rules if not r.needs_python
                )
                py_count = sum(1 for r in compiled_resource.rules if r.needs_python)
                logger.info(
                    f"[GUARD]   {resource_ast.name}: "
                    f"{len(compiled_resource.rules)} rules "
                    f"({zig_count} Zig-fast, {py_count} Python-complex)"
                )

    def load_string(self, source: str, path: str = "<string>") -> None:
        """Load a policy from a string (for testing)."""
        policy = parse_policy(source, path)
        result = validate_policies([policy])
        result.raise_if_invalid()
        self._policies.append(policy)
        for resource_ast in policy.resources:
            compiled_resource = _compile_resource(resource_ast)
            self._resources[resource_ast.name] = compiled_resource

    def get_resource(self, resource_name: str) -> CompiledResource | None:
        """Look up a compiled resource by name."""
        return self._resources.get(resource_name)

    def get_rules(self, resource_name: str, action: str) -> list[CompiledRule]:
        """Get all rules for a resource+action pair."""
        resource = self._resources.get(resource_name)
        if resource is None:
            return []
        return [r for r in resource.rules if r.action == action]

    def evaluate(
        self,
        resource_name: str,
        action: str,
        user_dict: dict[str, object],
        resource_dict: dict[str, object],
    ) -> bool:
        """Evaluate a policy for a resource+action against user and resource data.

        For simple (Zig-compiled) rules, uses bytecode evaluation.
        Returns True if any allow rule passes and no deny rule triggers.

        Note: Rules with needs_python=True are skipped by this method.
        Use the full @guard() decorator for those (async DB lookups).
        """
        rules = self.get_rules(resource_name, action)
        if not rules:
            return False  # No rules = deny by default

        # Safety: if ANY deny rule requires Python (relation checks, DB queries),
        # we cannot safely evaluate synchronously — deny by default.
        # This prevents a banned user bypassing a "deny where user is banned"
        # rule just because it couldn't be compiled to bytecode.
        for rule in rules:
            if rule.effect == RuleEffect.DENY and rule.needs_python:
                return False  # Cannot safely evaluate complex deny rules

        # Check deny rules first (any deny = rejected)
        for rule in rules:
            if rule.effect == RuleEffect.DENY and rule.compiled is not None:
                if rule.compiled.evaluate(user_dict, resource_dict):
                    return False  # Explicitly denied

        # Check allow rules — only bytecode-compiled rules evaluated here.
        # Rules with needs_python are skipped (require async @guard evaluation).
        for rule in rules:
            if rule.effect == RuleEffect.ALLOW and rule.compiled is not None:
                if rule.compiled.evaluate(user_dict, resource_dict):
                    return True

        return False  # No allow matched

    @property
    def resource_names(self) -> list[str]:
        return list(self._resources)

    @property
    def resource_count(self) -> int:
        return len(self._resources)


def _compile_resource(resource_ast: ResourceAST) -> CompiledResource:
    """Compile all rules in a resource AST to bytecode."""
    compiled_rules: list[CompiledRule] = []
    actions: set[str] = set()

    for rule_ast in resource_ast.rules:
        actions.add(rule_ast.action)
        compiled_rules.append(_compile_rule(rule_ast))

    return CompiledResource(
        name=resource_ast.name,
        rules=tuple(compiled_rules),
        actions=frozenset(actions),
    )


def _compile_rule(rule_ast: RuleAST) -> CompiledRule:
    """Compile a single rule AST to bytecode (if possible)."""
    # Separate bytecode-compilable conditions from Python-only conditions
    bytecode_conditions: list[Condition | CrossFieldCondition] = []
    python_conditions: list[RelationConditionAST | ResourceRefConditionAST] = []
    needs_python = False

    for cond in rule_ast.conditions:
        if isinstance(cond, FieldConditionAST):
            # All value types (int, bool, str) supported natively by Zig VM
            bytecode_conditions.append(
                Condition(
                    source=_SOURCE_MAP[cond.source],
                    field=cond.field,
                    op=_OP_MAP[cond.op],
                    value=cond.value,
                )
            )

        elif isinstance(cond, CrossFieldConditionAST):
            if cond.left_source == "resource" and cond.right_source == "user":
                bytecode_conditions.append(
                    CrossFieldCondition(
                        resource_field=cond.left_field,
                        op=_OP_MAP[cond.op],
                        user_field=cond.right_field,
                    )
                )
            elif cond.left_source == "user" and cond.right_source == "resource":
                # Flip operands: user.karma > resource.min → resource.min < user.karma
                # Symmetric ops (EQ, NE) don't change. Ordered ops flip.
                flipped_op = _flip_op(_OP_MAP[cond.op])
                bytecode_conditions.append(
                    CrossFieldCondition(
                        resource_field=cond.right_field,
                        op=flipped_op,
                        user_field=cond.left_field,
                    )
                )
            else:
                needs_python = True

        elif isinstance(cond, (RelationConditionAST, ResourceRefConditionAST)):
            needs_python = True
            python_conditions.append(cond)

    # Compile bytecode conditions.
    #
    # compile_conditions() supports a SINGLE flat combine mode (all-AND or
    # all-OR). A rule is only bytecode-safe when its OR positions form one of
    # those two shapes:
    #   pure-AND ⇔ or_indices == {}
    #   pure-OR  ⇔ or_indices == {1, …, n-1}
    # The old code used `bool(or_indices)` → ANY OR flipped the WHOLE rule to
    # OR, so a genuinely mixed rule like `(a AND b) OR c` compiled (and emitted
    # SQL) as `a OR b OR c` — a silent over-allow. For a mixed rule we now refuse
    # to emit bytecode/SQL and mark it needs_python so it is evaluated safely (or
    # excluded from the SQL filter, which fails closed to deny) rather than with
    # a wrong flat combine.
    compiled: CompiledGuard | None = None
    n_conditions = len(rule_ast.conditions)
    or_indices = set(rule_ast.or_indices)
    pure_and = not or_indices
    pure_or = or_indices == set(range(1, n_conditions))
    if bytecode_conditions and (pure_and or pure_or):
        combine = CombineMode.OR if pure_or else CombineMode.AND
        compiled = compile_conditions(bytecode_conditions, combine=combine)
    elif bytecode_conditions:
        # Genuinely mixed AND/OR — not expressible as a flat combine. Fail safe.
        needs_python = True

    return CompiledRule(
        action=rule_ast.action,
        effect=rule_ast.effect,
        compiled=compiled,
        needs_python=needs_python,
        python_conditions=tuple(python_conditions),
        or_indices=rule_ast.or_indices,
    )
