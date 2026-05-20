"""
HyperGuard policy validator — startup validation of parsed .guard policies.

Validates:
1. No duplicate resource names (within a file)
2. No cross-file resource name collisions
3. Field sources are "user" or "resource" (not typos)
4. No empty resource blocks (must have at least one rule)
5. No empty rule conditions

All validation happens at startup — if a policy has errors, the app fails to start
with clear error messages rather than failing silently at request time.
"""

from dataclasses import dataclass

from hyperdjango.guard.parser import (
    CrossFieldConditionAST,
    FieldConditionAST,
    PolicyAST,
    ResourceAST,
    RuleAST,
)


@dataclass(frozen=True)
class ValidationError:
    """Single validation error with location."""

    message: str
    resource: str
    rule_action: str
    line: int

    def __str__(self) -> str:
        return f"[{self.resource}.{self.rule_action} line {self.line}] {self.message}"


@dataclass
class ValidationResult:
    """Result of validating one or more policies."""

    errors: list[ValidationError]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def raise_if_invalid(self) -> None:
        """Raise ValueError with all errors if validation failed."""
        if self.errors:
            msg_lines = ["Guard policy validation failed:"]
            for err in self.errors:
                msg_lines.append(f"  {err}")
            raise ValueError("\n".join(msg_lines))


_VALID_SOURCES = frozenset({"user", "resource"})


def validate_policy(policy: PolicyAST) -> ValidationResult:
    """Validate a parsed policy AST for semantic correctness.

    Returns ValidationResult with errors (empty if valid).
    """
    errors: list[ValidationError] = []
    resource_names: set[str] = set()

    for resource in policy.resources:
        # Check duplicate resource names
        if resource.name in resource_names:
            errors.append(
                ValidationError(
                    message=f"Duplicate resource name: {resource.name!r}",
                    resource=resource.name,
                    rule_action="",
                    line=resource.line,
                )
            )
        resource_names.add(resource.name)

        _validate_resource(resource, errors)

    return ValidationResult(errors=errors)


def validate_policies(policies: list[PolicyAST]) -> ValidationResult:
    """Validate multiple policies together (cross-file references)."""
    errors: list[ValidationError] = []
    all_resource_names: set[str] = set()

    for policy in policies:
        result = validate_policy(policy)
        errors.extend(result.errors)
        for resource in policy.resources:
            if resource.name in all_resource_names:
                errors.append(
                    ValidationError(
                        message=f"Resource {resource.name!r} defined in multiple policy files",
                        resource=resource.name,
                        rule_action="",
                        line=resource.line,
                    )
                )
            all_resource_names.add(resource.name)

    return ValidationResult(errors=errors)


def _validate_resource(resource: ResourceAST, errors: list[ValidationError]) -> None:
    """Validate a single resource block."""
    if not resource.rules:
        errors.append(
            ValidationError(
                message="Resource has no rules",
                resource=resource.name,
                rule_action="",
                line=resource.line,
            )
        )
        return

    for rule in resource.rules:
        if not rule.conditions:
            errors.append(
                ValidationError(
                    message="Rule has no conditions",
                    resource=resource.name,
                    rule_action=rule.action,
                    line=rule.line,
                )
            )
            continue

        _validate_rule_conditions(resource.name, rule, errors)


def _validate_rule_conditions(
    resource_name: str, rule: RuleAST, errors: list[ValidationError]
) -> None:
    """Validate conditions within a rule."""
    # Mixed AND/OR: if some conditions have OR prefix but not all,
    # the semantics are ambiguous. Require either ALL conditions after the first
    # are OR (pure OR rule) or NONE are (pure AND rule). Validate OR POSITIONS,
    # not just the COUNT: the old count-based check (n_or < n-1) let a rule with
    # OR on conditions {0, 2} of 3 pass (count 2 == n-1) even though position 1
    # is a genuine AND — it then compiled to all-OR (over-allow). Positions make
    # the two legal shapes exact:
    #   pure-AND ⇔ or_indices == {}                (a AND b AND c)
    #   pure-OR  ⇔ or_indices == {1, 2, …, n-1}    (a OR b OR c)
    # Anything else is genuinely mixed — reject and tell the author to split it.
    n_conditions = len(rule.conditions)
    or_indices = set(rule.or_indices)
    pure_and = not or_indices
    pure_or = or_indices == set(range(1, n_conditions))
    if not (pure_and or pure_or):
        errors.append(
            ValidationError(
                message="Mixed AND/OR conditions — use separate rules for each OR branch, "
                "or prefix ALL conditions after the first with OR",
                resource=resource_name,
                rule_action=rule.action,
                line=rule.line,
            )
        )

    for cond in rule.conditions:
        if isinstance(cond, FieldConditionAST):
            if cond.source not in _VALID_SOURCES:
                errors.append(
                    ValidationError(
                        message=f"Invalid source {cond.source!r} — must be 'user' or 'resource'",
                        resource=resource_name,
                        rule_action=rule.action,
                        line=rule.line,
                    )
                )

        elif isinstance(cond, CrossFieldConditionAST):
            if cond.left_source not in _VALID_SOURCES:
                errors.append(
                    ValidationError(
                        message=f"Invalid left source {cond.left_source!r}",
                        resource=resource_name,
                        rule_action=rule.action,
                        line=rule.line,
                    )
                )
            if cond.right_source not in _VALID_SOURCES:
                errors.append(
                    ValidationError(
                        message=f"Invalid right source {cond.right_source!r}",
                        resource=resource_name,
                        rule_action=rule.action,
                        line=rule.line,
                    )
                )
            if cond.left_source == cond.right_source:
                errors.append(
                    ValidationError(
                        message=f"Cross-field condition compares {cond.left_source}.{cond.left_field} "
                        f"with {cond.right_source}.{cond.right_field} — both from same source",
                        resource=resource_name,
                        rule_action=rule.action,
                        line=rule.line,
                    )
                )
