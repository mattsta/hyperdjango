"""
HyperGuard .guard policy file tests — parser, validator, registry, end-to-end.

Tests the full pipeline: .guard source → lexer → parser → AST → validator →
bytecode compiler → Zig evaluation → allow/deny.
"""

# hyper-test: unit

from hyperdjango.guard.parser import (
    ConditionOp,
    CrossFieldConditionAST,
    FieldConditionAST,
    ParseError,
    RelationConditionAST,
    ResourceRefConditionAST,
    RuleEffect,
    parse_policy,
)
from hyperdjango.guard.registry import PolicyRegistry
from hyperdjango.guard.validator import validate_policies, validate_policy

_PASS = 0
_FAIL = 0


def check(condition: bool, msg: str) -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  FAIL: {msg}")


# ── Test: Lexer + Parser basics ──────────────────────────────────────────────


def test_parse_simple_resource():
    """Parse a resource with one rule."""
    print("test_parse_simple_resource")
    ast = parse_policy("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)
    check(len(ast.resources) == 1, f"1 resource, got {len(ast.resources)}")
    r = ast.resources[0]
    check(r.name == "Forum", f"name: {r.name}")
    check(len(r.rules) == 1, f"1 rule, got {len(r.rules)}")
    rule = r.rules[0]
    check(rule.effect == RuleEffect.ALLOW, f"allow, got {rule.effect}")
    check(rule.action == "read", f"action: {rule.action}")
    check(len(rule.conditions) == 1, f"1 condition, got {len(rule.conditions)}")
    cond = rule.conditions[0]
    check(isinstance(cond, FieldConditionAST), "field condition")
    check(cond.source == "resource", f"source: {cond.source}")
    check(cond.field == "is_public", f"field: {cond.field}")
    check(cond.op == ConditionOp.EQ, f"op: {cond.op}")
    check(cond.value is True, f"value: {cond.value}")


def test_parse_multiple_conditions():
    """Parse a rule with multiple AND conditions."""
    print("test_parse_multiple_conditions")
    ast = parse_policy("""
    resource Forum {
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
            resource.is_public = true
        }
    }
    """)
    rule = ast.resources[0].rules[0]
    check(len(rule.conditions) == 3, f"3 conditions, got {len(rule.conditions)}")
    check(rule.conditions[0].field == "is_archived", "first field")
    check(rule.conditions[1].field == "is_locked", "second field")
    check(rule.conditions[2].field == "is_public", "third field")


def test_parse_cross_field():
    """Parse cross-field condition: resource.author_id = user.id."""
    print("test_parse_cross_field")
    ast = parse_policy("""
    resource Post {
        allow edit where {
            resource.author_id = user.id
        }
    }
    """)
    cond = ast.resources[0].rules[0].conditions[0]
    check(isinstance(cond, CrossFieldConditionAST), "cross-field")
    check(cond.left_source == "resource", f"left: {cond.left_source}")
    check(cond.left_field == "author_id", f"left field: {cond.left_field}")
    check(cond.right_source == "user", f"right: {cond.right_source}")
    check(cond.right_field == "id", f"right field: {cond.right_field}")


def test_parse_relation():
    """Parse relation condition: user is member of resource."""
    print("test_parse_relation")
    ast = parse_policy("""
    resource Forum {
        allow read where {
            user is member of resource
        }
    }
    """)
    cond = ast.resources[0].rules[0].conditions[0]
    check(isinstance(cond, RelationConditionAST), "relation")
    check(cond.role == "member", f"role: {cond.role}")


def test_parse_resource_ref():
    """Parse cross-resource ref: resource.forum allows read."""
    print("test_parse_resource_ref")
    ast = parse_policy("""
    resource Post {
        allow read where {
            resource.forum allows read
        }
    }
    """)
    cond = ast.resources[0].rules[0].conditions[0]
    check(isinstance(cond, ResourceRefConditionAST), "resource ref")
    check(cond.relation == "forum", f"relation: {cond.relation}")
    check(cond.action == "read", f"action: {cond.action}")


def test_parse_or_conditions():
    """Parse OR-prefixed conditions."""
    print("test_parse_or_conditions")
    ast = parse_policy("""
    resource Forum {
        allow admin where {
            user.is_staff = true
            OR user.is_superuser = true
        }
    }
    """)
    rule = ast.resources[0].rules[0]
    check(len(rule.conditions) == 2, f"2 conditions, got {len(rule.conditions)}")
    check(1 in rule.or_indices, f"OR at index 1, got {rule.or_indices}")
    check(0 not in rule.or_indices, "first condition not OR")


def test_parse_deny_rule():
    """Parse deny rules."""
    print("test_parse_deny_rule")
    ast = parse_policy("""
    resource Forum {
        deny write_post where {
            resource.is_archived = true
        }
    }
    """)
    rule = ast.resources[0].rules[0]
    check(rule.effect == RuleEffect.DENY, f"deny, got {rule.effect}")


def test_parse_integer_value():
    """Parse integer comparison value."""
    print("test_parse_integer_value")
    ast = parse_policy("""
    resource Forum {
        allow create where {
            user.karma >= 50
        }
    }
    """)
    cond = ast.resources[0].rules[0].conditions[0]
    check(isinstance(cond, FieldConditionAST), "field condition")
    check(cond.value == 50, f"value: {cond.value}")
    check(cond.op == ConditionOp.GE, f"op: {cond.op}")


def test_parse_string_value():
    """Parse string comparison value."""
    print("test_parse_string_value")
    ast = parse_policy("""
    resource Post {
        allow read where {
            resource.status = "published"
        }
    }
    """)
    cond = ast.resources[0].rules[0].conditions[0]
    check(isinstance(cond, FieldConditionAST), "field condition")
    check(cond.value == "published", f"value: {cond.value}")


def test_parse_multiple_resources():
    """Parse file with multiple resource blocks."""
    print("test_parse_multiple_resources")
    ast = parse_policy("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    resource Post {
        allow read where {
            resource.author_id = user.id
        }
    }
    """)
    check(len(ast.resources) == 2, f"2 resources, got {len(ast.resources)}")
    check(ast.resources[0].name == "Forum", "first is Forum")
    check(ast.resources[1].name == "Post", "second is Post")


def test_parse_comments():
    """Comments are ignored."""
    print("test_parse_comments")
    ast = parse_policy("""
    # This is a comment
    resource Forum {
        # Rule comment
        allow read where {
            resource.is_public = true  # inline comment
        }
    }
    """)
    check(len(ast.resources) == 1, "parsed despite comments")


def test_parse_all_operators():
    """All comparison operators parse correctly."""
    print("test_parse_all_operators")
    ast = parse_policy("""
    resource Test {
        allow check where {
            user.a = 1
            user.b != 2
            user.c > 3
            user.d >= 4
            user.e < 5
            user.f <= 6
        }
    }
    """)
    conds = ast.resources[0].rules[0].conditions
    check(conds[0].op == ConditionOp.EQ, "=")
    check(conds[1].op == ConditionOp.NE, "!=")
    check(conds[2].op == ConditionOp.GT, ">")
    check(conds[3].op == ConditionOp.GE, ">=")
    check(conds[4].op == ConditionOp.LT, "<")
    check(conds[5].op == ConditionOp.LE, "<=")


# ── Test: Parser errors ──────────────────────────────────────────────────────


def test_parse_error_missing_brace():
    """Missing closing brace raises ParseError."""
    print("test_parse_error_missing_brace")
    try:
        parse_policy("resource Forum { allow read where { resource.x = true }")
        check(False, "should raise")
    except ParseError as e:
        check("Expected" in str(e), f"error: {e}")


def test_parse_error_bad_operator():
    """Invalid operator raises ParseError."""
    print("test_parse_error_bad_operator")
    try:
        parse_policy("""
        resource Forum {
            allow read where {
                resource.x ~ true
            }
        }
        """)
        check(False, "should raise")
    except ParseError as e:
        check(True, f"error: {e}")


def test_parse_error_unterminated_string():
    """Unterminated string raises ParseError."""
    print("test_parse_error_unterminated_string")
    try:
        parse_policy("""
        resource Post {
            allow read where {
                resource.status = "published
            }
        }
        """)
        check(False, "should raise")
    except ParseError as e:
        check("Unterminated" in str(e), f"error: {e}")


# ── Test: Validator ──────────────────────────────────────────────────────────


def test_validate_valid_policy():
    """Valid policy passes validation."""
    print("test_validate_valid_policy")
    ast = parse_policy("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)
    result = validate_policy(ast)
    check(result.is_valid, f"should be valid, errors: {result.errors}")


def test_validate_duplicate_resource_across_files():
    """Same resource in multiple policies fails cross-file validation."""
    print("test_validate_duplicate_resource_across_files")
    ast1 = parse_policy(
        """
    resource Forum {
        allow read where { resource.is_public = true }
    }
    """,
        path="a.guard",
    )
    ast2 = parse_policy(
        """
    resource Forum {
        allow write where { resource.is_public = true }
    }
    """,
        path="b.guard",
    )
    result = validate_policies([ast1, ast2])
    check(not result.is_valid, "should fail")
    check(
        any("multiple policy files" in str(e) for e in result.errors),
        "mentions multiple files",
    )


def test_validate_invalid_source():
    """Invalid field source fails validation."""
    print("test_validate_invalid_source")
    ast = parse_policy("""
    resource Forum {
        allow read where {
            request.is_public = true
        }
    }
    """)
    result = validate_policy(ast)
    check(not result.is_valid, "should fail")
    check(
        any("Invalid source" in str(e) for e in result.errors),
        "mentions invalid source",
    )


def test_validate_empty_rules():
    """Resource with no rules fails validation."""
    print("test_validate_empty_rules")
    ast = parse_policy("""
    resource Forum {
    }
    """)
    result = validate_policy(ast)
    check(not result.is_valid, "should fail")
    check(any("no rules" in str(e) for e in result.errors), "mentions no rules")


def test_validate_mixed_and_or():
    """Mixed AND/OR conditions fail validation."""
    print("test_validate_mixed_and_or")
    ast = parse_policy("""
    resource Forum {
        allow write where {
            resource.is_archived = false
            resource.is_locked = false
            OR user.is_staff = true
        }
    }
    """)
    result = validate_policy(ast)
    check(not result.is_valid, "mixed AND/OR should fail")
    check(any("Mixed AND/OR" in str(e) for e in result.errors), "mentions mixed AND/OR")


def test_validate_same_source_cross_field():
    """Same-source cross-field condition fails validation."""
    print("test_validate_same_source_cross_field")
    ast = parse_policy("""
    resource Forum {
        allow read where {
            resource.x = resource.y
        }
    }
    """)
    result = validate_policy(ast)
    check(not result.is_valid, "same-source cross-field should fail")
    check(any("same source" in str(e) for e in result.errors), "mentions same source")


def test_validate_raise_if_invalid():
    """raise_if_invalid raises ValueError."""
    print("test_validate_raise_if_invalid")
    ast = parse_policy("""
    resource Forum {
    }
    """)
    result = validate_policy(ast)
    try:
        result.raise_if_invalid()
        check(False, "should raise")
    except ValueError as e:
        check("validation failed" in str(e), f"error: {e}")


# ── Test: Registry end-to-end ────────────────────────────────────────────────


def test_registry_load_and_evaluate():
    """Registry loads policy string and evaluates via Zig bytecode."""
    print("test_registry_load_and_evaluate")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
        }
    }
    """)
    check(registry.resource_count == 1, f"1 resource, got {registry.resource_count}")
    check("Forum" in registry.resource_names, "Forum registered")

    # Public forum → allow read
    check(
        registry.evaluate("Forum", "read", {}, {"is_public": True}) is True,
        "public forum read allowed",
    )
    # Private forum → deny read
    check(
        registry.evaluate("Forum", "read", {}, {"is_public": False}) is False,
        "private forum read denied",
    )
    # Non-archived, non-locked → allow write
    check(
        registry.evaluate(
            "Forum", "write_post", {}, {"is_archived": False, "is_locked": False}
        )
        is True,
        "writable forum allows write",
    )
    # Archived → deny write
    check(
        registry.evaluate(
            "Forum", "write_post", {}, {"is_archived": True, "is_locked": False}
        )
        is False,
        "archived forum denies write",
    )


def test_registry_ownership_rule():
    """Registry evaluates cross-field ownership condition."""
    print("test_registry_ownership_rule")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Post {
        allow edit where {
            resource.author_id = user.id
        }
    }
    """)
    check(
        registry.evaluate("Post", "edit", {"id": 42}, {"author_id": 42}) is True,
        "owner can edit",
    )
    check(
        registry.evaluate("Post", "edit", {"id": 42}, {"author_id": 99}) is False,
        "non-owner denied",
    )


def test_registry_deny_overrides_allow():
    """Deny rules are checked before allow rules."""
    print("test_registry_deny_overrides_allow")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        deny write_post where {
            resource.is_archived = true
        }
        allow write_post where {
            resource.is_public = true
        }
    }
    """)
    # Public but archived → deny wins
    check(
        registry.evaluate(
            "Forum", "write_post", {}, {"is_public": True, "is_archived": True}
        )
        is False,
        "deny overrides allow",
    )
    # Public and not archived → allow
    check(
        registry.evaluate(
            "Forum", "write_post", {}, {"is_public": True, "is_archived": False}
        )
        is True,
        "allow when not denied",
    )


def test_registry_or_conditions():
    """OR conditions compiled with OR combine mode."""
    print("test_registry_or_conditions")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow admin where {
            user.is_staff = true
            OR user.is_superuser = true
        }
    }
    """)
    check(
        registry.evaluate(
            "Forum", "admin", {"is_staff": True, "is_superuser": False}, {}
        )
        is True,
        "staff passes",
    )
    check(
        registry.evaluate(
            "Forum", "admin", {"is_staff": False, "is_superuser": True}, {}
        )
        is True,
        "superuser passes",
    )
    check(
        registry.evaluate(
            "Forum", "admin", {"is_staff": False, "is_superuser": False}, {}
        )
        is False,
        "neither fails",
    )


def test_registry_relation_needs_python():
    """Relation conditions flag rule as needs_python."""
    print("test_registry_relation_needs_python")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            user is member of resource
        }
    }
    """)
    rules = registry.get_rules("Forum", "read")
    check(len(rules) == 1, f"1 rule, got {len(rules)}")
    check(rules[0].needs_python is True, "needs python for relation")
    check(len(rules[0].python_conditions) == 1, "1 python condition")


def test_registry_unknown_resource():
    """Evaluating unknown resource returns False (deny by default)."""
    print("test_registry_unknown_resource")
    registry = PolicyRegistry()
    check(registry.evaluate("Nonexistent", "read", {}, {}) is False, "unknown = deny")


def test_registry_unknown_action():
    """Evaluating unknown action returns False (deny by default)."""
    print("test_registry_unknown_action")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow read where {
            resource.is_public = true
        }
    }
    """)
    check(
        registry.evaluate("Forum", "delete", {}, {"is_public": True}) is False,
        "unknown action = deny",
    )


def test_registry_integer_condition():
    """Integer comparison in policy."""
    print("test_registry_integer_condition")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow create where {
            user.karma >= 50
        }
    }
    """)
    check(registry.evaluate("Forum", "create", {"karma": 100}, {}) is True, "100 >= 50")
    check(registry.evaluate("Forum", "create", {"karma": 10}, {}) is False, "10 < 50")


def test_registry_complex_real_world():
    """Real-world HyperNews-like policy with multiple resources and rules."""
    print("test_registry_complex_real_world")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        # Anyone can read public forums
        allow read where {
            resource.is_public = true
        }

        # Writable if not archived and not locked
        allow write_post where {
            resource.is_archived = false
            resource.is_locked = false
        }

        # Deny writes to archived forums (explicit deny)
        deny write_post where {
            resource.is_archived = true
        }

        # Staff can admin any forum
        allow admin where {
            user.is_staff = true
        }
    }

    resource Post {
        # Author can edit own posts
        allow edit where {
            resource.author_id = user.id
        }

        # Staff can edit any post
        allow edit where {
            user.is_staff = true
        }
    }
    """)

    # Forum reads
    check(
        registry.evaluate("Forum", "read", {}, {"is_public": True}) is True,
        "public read",
    )
    check(
        registry.evaluate("Forum", "read", {}, {"is_public": False}) is False,
        "private read",
    )

    # Forum writes
    check(
        registry.evaluate(
            "Forum", "write_post", {}, {"is_archived": False, "is_locked": False}
        )
        is True,
        "writable",
    )
    check(
        registry.evaluate(
            "Forum", "write_post", {}, {"is_archived": True, "is_locked": False}
        )
        is False,
        "archived deny",
    )

    # Forum admin
    check(
        registry.evaluate("Forum", "admin", {"is_staff": True}, {}) is True,
        "staff admin",
    )
    check(
        registry.evaluate("Forum", "admin", {"is_staff": False}, {}) is False,
        "non-staff",
    )

    # Post edit — owner
    check(
        registry.evaluate("Post", "edit", {"id": 1}, {"author_id": 1}) is True,
        "owner edit",
    )
    check(
        registry.evaluate("Post", "edit", {"id": 1}, {"author_id": 2}) is False,
        "non-owner",
    )

    # Post edit — staff override
    check(
        registry.evaluate("Post", "edit", {"is_staff": True}, {"author_id": 999})
        is True,
        "staff edit",
    )


def test_parse_negative_integer():
    """Parse negative integer value."""
    print("test_parse_negative_integer")
    ast = parse_policy("""
    resource Account {
        deny withdraw where {
            user.balance < -100
        }
    }
    """)
    cond = ast.resources[0].rules[0].conditions[0]
    check(isinstance(cond, FieldConditionAST), "field condition")
    check(cond.value == -100, f"value: {cond.value}")
    check(cond.op == ConditionOp.LT, f"op: {cond.op}")


def test_registry_flipped_cross_field_ordered():
    """user.karma > resource.min_karma flips operator correctly."""
    print("test_registry_flipped_cross_field_ordered")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        allow create where {
            user.karma > resource.min_karma
        }
    }
    """)
    # user.karma=100 > resource.min_karma=50 → allow
    check(
        registry.evaluate("Forum", "create", {"karma": 100}, {"min_karma": 50}) is True,
        "100 > 50 allows",
    )
    # user.karma=30 > resource.min_karma=50 → deny
    check(
        registry.evaluate("Forum", "create", {"karma": 30}, {"min_karma": 50}) is False,
        "30 > 50 denies",
    )
    # Boundary: user.karma=50 > resource.min_karma=50 → deny (strict >)
    check(
        registry.evaluate("Forum", "create", {"karma": 50}, {"min_karma": 50}) is False,
        "50 > 50 denies (strict)",
    )


def test_registry_deny_with_relation_blocks():
    """Deny rule with relation condition blocks all access (safe default)."""
    print("test_registry_deny_with_relation_blocks")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Forum {
        deny write where {
            user is banned of resource
        }
        allow write where {
            resource.is_public = true
        }
    }
    """)
    # The deny rule needs_python (relation check), so evaluate() returns False
    # even though the allow rule would pass — safe by default
    check(
        registry.evaluate("Forum", "write", {}, {"is_public": True}) is False,
        "deny-with-relation blocks all synchronous evaluation",
    )


def test_registry_string_status_condition():
    """String conditions compile to Zig bytecode (no needs_python hack)."""
    print("test_registry_string_status_condition")
    registry = PolicyRegistry()
    registry.load_string("""
    resource Post {
        allow read where {
            resource.status = "published"
        }
        deny read where {
            resource.status = "deleted"
        }
    }
    """)
    rules = registry.get_rules("Post", "read")
    # Both rules should compile to Zig bytecode — no needs_python
    for rule in rules:
        check(rule.needs_python is False, f"rule {rule.effect} should NOT need python")
        check(rule.compiled is not None, f"rule {rule.effect} should have bytecode")

    # Evaluate
    check(
        registry.evaluate("Post", "read", {}, {"status": "published"}) is True,
        "published allows",
    )
    check(
        registry.evaluate("Post", "read", {}, {"status": "draft"}) is False,
        "draft denies (no allow match)",
    )
    check(
        registry.evaluate("Post", "read", {}, {"status": "deleted"}) is False,
        "deleted explicitly denied",
    )


def test_registry_duplicate_resource_same_file():
    """Same resource name twice in one file — validation error."""
    print("test_registry_duplicate_resource_same_file")
    registry = PolicyRegistry()
    try:
        registry.load_string("""
        resource Forum {
            allow read where {
                resource.is_public = true
            }
        }
        resource Forum {
            allow write where {
                user.is_staff = true
            }
        }
        """)
        check(False, "should raise ValueError")
    except ValueError as e:
        check("Duplicate" in str(e), f"mentions duplicate: {e}")


# ── Run all ──────────────────────────────────────────────────────────────────


def main():
    tests = [
        # Parser
        test_parse_simple_resource,
        test_parse_multiple_conditions,
        test_parse_cross_field,
        test_parse_relation,
        test_parse_resource_ref,
        test_parse_or_conditions,
        test_parse_deny_rule,
        test_parse_integer_value,
        test_parse_string_value,
        test_parse_multiple_resources,
        test_parse_comments,
        test_parse_all_operators,
        # Parser errors
        test_parse_error_missing_brace,
        test_parse_error_bad_operator,
        test_parse_error_unterminated_string,
        # Validator
        test_validate_valid_policy,
        test_validate_duplicate_resource_across_files,
        test_validate_invalid_source,
        test_validate_empty_rules,
        test_validate_mixed_and_or,
        test_validate_same_source_cross_field,
        test_validate_raise_if_invalid,
        # Registry end-to-end
        test_registry_deny_with_relation_blocks,
        test_registry_load_and_evaluate,
        test_registry_ownership_rule,
        test_registry_deny_overrides_allow,
        test_registry_or_conditions,
        test_registry_relation_needs_python,
        test_registry_unknown_resource,
        test_registry_unknown_action,
        test_registry_integer_condition,
        test_registry_complex_real_world,
        test_registry_string_status_condition,
        test_parse_negative_integer,
        test_registry_flipped_cross_field_ordered,
        test_registry_duplicate_resource_same_file,
    ]

    for test in tests:
        test()

    total = _PASS + _FAIL
    print(f"\n{'=' * 60}")
    print(f"HyperGuard Policy: {_PASS}/{total} passed, {_FAIL} failed")
    if _FAIL:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
