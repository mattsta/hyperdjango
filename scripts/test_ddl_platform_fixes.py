"""
Test DDL generation platform fixes: on_delete, unique_together, NOT NULL inference.

# hyper-test: unit
"""

import sys

from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f": {detail}"
        print(msg)
        ERRORS.append(msg)


def get_ddl(model_cls) -> str:
    """Extract the DDL that create_table_for_model would generate, without executing."""

    from hyperdjango.models import (
        FieldInfo,
        _annotation_is_nullable,
        _field_to_sql_type,
        _python_default_to_sql,
    )

    meta = model_cls._meta
    table = meta.table

    annotations = {}
    for klass in reversed(model_cls.__mro__):
        annotations.update(getattr(klass, "__annotations__", {}))

    columns: list[str] = []
    for field_name, field_meta in meta.fields.items():
        if field_meta.foreign_key:
            col_type = "INTEGER"
        elif field_meta.primary_key and field_meta.auto:
            col_type = "SERIAL"
        else:
            col_type = _field_to_sql_type(model_cls, field_name)

        parts = [f"    {field_name} {col_type}"]
        if field_meta.primary_key and not meta.is_composite_pk:
            parts.append("PRIMARY KEY")

        if not field_meta.primary_key and not field_meta.auto:
            ann = annotations.get(field_name)
            is_nullable = _annotation_is_nullable(ann)
            field_obj = model_cls.__dict__.get(field_name)
            has_none_default = (
                field_obj is not None
                and isinstance(field_obj, FieldInfo)
                and field_obj.default is None
            )
            if not is_nullable and not has_none_default:
                parts.append("NOT NULL")

        if field_meta.unique:
            parts.append("UNIQUE")

        if not field_meta.primary_key:
            field_obj = model_cls.__dict__.get(field_name)
            if field_obj is not None:
                if field_obj.db_default is not None:
                    default_sql = _python_default_to_sql(field_obj.db_default)
                else:
                    default_sql = _python_default_to_sql(field_obj.default)
                if default_sql is not None:
                    parts.append(f"DEFAULT {default_sql}")

        if field_meta.foreign_key:
            fk_target = field_meta.foreign_key
            if "." not in fk_target:
                fk_target = f"{fk_target}(id)"
            else:
                tbl, col = fk_target.rsplit(".", 1)
                fk_target = f"{tbl}({col})"
            fk_clause = f"REFERENCES {fk_target}"
            if field_meta.on_delete:
                fk_clause += f" ON DELETE {field_meta.on_delete}"
            parts.append(fk_clause)

        columns.append(" ".join(parts))

    if meta.is_composite_pk:
        pk_cols = ", ".join(meta.pk_fields)
        columns.append(f"    PRIMARY KEY ({pk_cols})")

    for ut in meta.unique_together:
        ut_cols = ", ".join(ut)
        columns.append(f"    UNIQUE({ut_cols})")

    unlogged = "UNLOGGED " if meta.unlogged else ""
    ddl = f"CREATE {unlogged}TABLE IF NOT EXISTS {table} (\n"
    ddl += ",\n".join(columns)
    ddl += "\n)"
    return ddl


def main():
    print("=" * 60)
    print("DDL Platform Fixes Tests")
    print("=" * 60)

    # ── 1. on_delete support ──
    print("\n--- on_delete support ---")

    class Parent(TimestampMixin, Model):
        class Meta:
            table = "test_parent"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()

    class ChildCascade(TimestampMixin, Model):
        class Meta:
            table = "test_child_cascade"

        id: int = Field(primary_key=True, auto=True)
        parent_id: int = Field(foreign_key=Parent, on_delete="CASCADE")

    class ChildSetNull(TimestampMixin, Model):
        class Meta:
            table = "test_child_setnull"

        id: int = Field(primary_key=True, auto=True)
        parent_id: int | None = Field(
            default=None, foreign_key=Parent, on_delete="SET NULL"
        )

    ddl_cascade = get_ddl(ChildCascade)
    check("CASCADE in DDL", "ON DELETE CASCADE" in ddl_cascade, ddl_cascade)

    ddl_setnull = get_ddl(ChildSetNull)
    check("SET NULL in DDL", "ON DELETE SET NULL" in ddl_setnull, ddl_setnull)

    class ChildNoAction(TimestampMixin, Model):
        class Meta:
            table = "test_child_noaction"

        id: int = Field(primary_key=True, auto=True)
        parent_id: int = Field(foreign_key=Parent)

    ddl_noaction = get_ddl(ChildNoAction)
    check(
        "No on_delete = no ON DELETE clause",
        "ON DELETE" not in ddl_noaction,
        ddl_noaction,
    )

    # ── 2. unique_together support ──
    print("\n--- unique_together support ---")

    class JunctionTable(Model):
        class Meta:
            table = "test_junction"
            unique_together = [("user_id", "group_id")]

        id: int = Field(primary_key=True, auto=True)
        user_id: int = Field()
        group_id: int = Field()

    ddl_junction = get_ddl(JunctionTable)
    check(
        "UNIQUE(user_id, group_id) in DDL",
        "UNIQUE(user_id, group_id)" in ddl_junction,
        ddl_junction,
    )

    class MultiUnique(Model):
        class Meta:
            table = "test_multi_unique"
            unique_together = [("codename", "model_name"), ("tenant_id", "name")]

        id: int = Field(primary_key=True, auto=True)
        codename: str = Field()
        model_name: str = Field()
        tenant_id: int = Field(default=0)
        name: str = Field()

    ddl_multi = get_ddl(MultiUnique)
    check(
        "First composite UNIQUE", "UNIQUE(codename, model_name)" in ddl_multi, ddl_multi
    )
    check("Second composite UNIQUE", "UNIQUE(tenant_id, name)" in ddl_multi, ddl_multi)

    # ── 3. NOT NULL inference ──
    print("\n--- NOT NULL inference ---")

    class NullTest(TimestampMixin, Model):
        class Meta:
            table = "test_null"

        id: int = Field(primary_key=True, auto=True)
        required_str: str = Field()  # NOT NULL (str, no default)
        required_int: int = Field()  # NOT NULL (int, no default)
        optional_str: str | None = Field(default=None)  # nullable
        defaulted_str: str = Field(default="")  # NOT NULL with DEFAULT
        required_bool: bool = Field(default=False)  # NOT NULL with DEFAULT

    ddl_null = get_ddl(NullTest)
    check("required_str NOT NULL", "required_str TEXT NOT NULL" in ddl_null, ddl_null)
    check(
        "required_int NOT NULL", "required_int INTEGER NOT NULL" in ddl_null, ddl_null
    )
    check(
        "optional_str no NOT NULL",
        "optional_str TEXT NOT NULL" not in ddl_null,
        ddl_null,
    )
    check(
        "defaulted_str NOT NULL + DEFAULT",
        "defaulted_str TEXT NOT NULL DEFAULT ''" in ddl_null,
        ddl_null,
    )
    check(
        "required_bool NOT NULL + DEFAULT",
        "required_bool BOOLEAN NOT NULL DEFAULT FALSE" in ddl_null,
        ddl_null,
    )

    # PK and auto fields should NOT get extra NOT NULL (PK implies NOT NULL)
    check(
        "PK no extra NOT NULL",
        "id SERIAL PRIMARY KEY" in ddl_null and "id SERIAL NOT NULL" not in ddl_null,
        ddl_null,
    )

    # ── 4. RBAC models produce correct DDL ──
    print("\n--- RBAC model DDL correctness ---")

    from hyperdjango.auth.user import (
        Group,
        GroupPermission,
        Permission,
        User,
        UserGroup,
        UserPermission,
    )

    ddl_user = get_ddl(User)
    check("User.username UNIQUE", "username TEXT NOT NULL UNIQUE" in ddl_user, ddl_user)

    ddl_perm = get_ddl(Permission)
    check(
        "Permission unique_together",
        "UNIQUE(codename, model_name)" in ddl_perm,
        ddl_perm,
    )

    ddl_group = get_ddl(Group)
    check("Group.name UNIQUE", "name TEXT NOT NULL UNIQUE" in ddl_group, ddl_group)
    check("Group.parent_id SET NULL", "ON DELETE SET NULL" in ddl_group, ddl_group)

    ddl_ug = get_ddl(UserGroup)
    check("UserGroup.user_id CASCADE", "ON DELETE CASCADE" in ddl_ug, ddl_ug)
    check("UserGroup unique_together", "UNIQUE(user_id, group_id)" in ddl_ug, ddl_ug)

    ddl_up = get_ddl(UserPermission)
    check("UserPermission CASCADE", "ON DELETE CASCADE" in ddl_up, ddl_up)
    check(
        "UserPermission unique_together",
        "UNIQUE(user_id, permission_id)" in ddl_up,
        ddl_up,
    )

    ddl_gp = get_ddl(GroupPermission)
    check("GroupPermission CASCADE", "ON DELETE CASCADE" in ddl_gp, ddl_gp)
    check(
        "GroupPermission unique_together",
        "UNIQUE(group_id, permission_id)" in ddl_gp,
        ddl_gp,
    )

    # ── 5. Model.to_dict() ──
    print("\n--- Model.to_dict() ---")

    from hyperdjango.auth.user import User

    user = User(
        id=1,
        username="testuser",
        email="test@example.com",
        password_hash="$argon2id$secret",
        first_name="Test",
        last_name="User",
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )

    d = user.to_dict()
    check("to_dict includes id", d.get("id") == 1, f"got {d.get('id')}")
    check("to_dict includes username", d.get("username") == "testuser", str(d))
    check("to_dict includes email", d.get("email") == "test@example.com", str(d))
    check(
        "to_dict excludes password_hash (Field exclude=True)",
        "password_hash" not in d,
        f"keys: {list(d.keys())}",
    )
    check("to_dict includes is_staff", "is_staff" in d, f"keys: {list(d.keys())}")

    # Explicit exclude parameter
    d2 = user.to_dict(exclude={"email", "first_name"})
    check("exclude param removes email", "email" not in d2, str(d2))
    check("exclude param removes first_name", "first_name" not in d2, str(d2))
    check("exclude param keeps username", d2.get("username") == "testuser", str(d2))

    # Explicit include parameter (allowlist overrides all exclusions)
    d3 = user.to_dict(include={"id", "username", "password_hash"})
    check("include allowlist: has id", d3.get("id") == 1, str(d3))
    check("include allowlist: has username", d3.get("username") == "testuser", str(d3))
    check(
        "include allowlist: overrides exclude — has password_hash",
        d3.get("password_hash") == "$argon2id$secret",
        str(d3),
    )
    check(
        "include allowlist: only 3 keys",
        len(d3) == 3,
        f"got {len(d3)} keys: {list(d3.keys())}",
    )

    # Model without exclude fields
    class SimpleModel(TimestampMixin, Model):
        class Meta:
            table = "test_simple_todict"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        value: int = Field(default=0)

    obj = SimpleModel(id=1, name="hello", value=42)
    sd = obj.to_dict()
    check("simple to_dict has all fields", "name" in sd and "value" in sd, str(sd))
    check(
        "simple to_dict has timestamps", "created_at" in sd, f"keys: {list(sd.keys())}"
    )

    # ── Summary ──
    print(f"\n{'=' * 60}")
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(f"  {e}")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
