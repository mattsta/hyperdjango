"""Tests for standalone Model and QuerySet SQL generation."""

from hyperdjango.models import Field, Model
from hyperdjango.validation import core as _vc


class TestModelDefinition:
    def test_basic_model(self):
        class User(Model):
            class Meta:
                table = "users"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field(max_length=100)
            email: str = Field()

        assert User._meta.table == "users"
        assert User._meta.pk_field == "id"
        assert User._meta.auto_field == "id"
        assert "name" in User._meta.fields
        assert "email" in User._meta.fields

    def test_default_table_name(self):
        class Article(Model):
            id: int = Field(primary_key=True, auto=True)
            title: str = Field()

        assert Article._meta.table == "articles"

    def test_field_metadata(self):
        class Product(Model):
            class Meta:
                table = "products"

            id: int = Field(primary_key=True, auto=True)
            sku: str = Field(unique=True, max_length=50)
            name: str = Field(index=True)

        assert Product._meta.fields["sku"].unique is True
        assert Product._meta.fields["name"].index is True

    def test_writable_columns(self):
        class Item(Model):
            class Meta:
                table = "items"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field()
            price: float = Field()

        writable = Item._meta.writable_columns
        assert "id" not in writable
        assert "name" in writable
        assert "price" in writable

    def test_model_is_dhi_basemodel(self):
        class Thing(Model):
            name: str = Field()

        assert issubclass(Thing, _vc.BaseModel)

    def test_model_validation(self):
        class User(Model):
            class Meta:
                table = "users"

            name: str = Field(max_length=10)

        # Valid
        user = User(name="Alice")
        assert user.name == "Alice"

    def test_model_from_record(self):
        class User(Model):
            class Meta:
                table = "users"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field()

        # Simulate asyncpg Record as dict
        class FakeRecord(dict):
            pass

        record = FakeRecord(id=1, name="Alice")
        user = User.from_record(record)
        assert user.id == 1
        assert user.name == "Alice"

    def test_model_dump(self):
        class User(Model):
            class Meta:
                table = "users"

            name: str = Field()
            age: int = Field(default=0)

        user = User(name="Bob", age=30)
        data = user.model_dump()
        assert data == {"name": "Bob", "age": 30}

    def test_objects_queryset(self):
        class User(Model):
            class Meta:
                table = "users"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field()

        # objects should be a QuerySet
        qs = User.objects
        assert hasattr(qs, "filter")
        assert hasattr(qs, "all")


class TestQuerySetSQLGeneration:
    """Test SQL generation without hitting a real database."""

    def _make_model(self):
        class User(Model):
            class Meta:
                table = "users"

            id: int = Field(primary_key=True, auto=True)
            name: str = Field(max_length=100)
            email: str = Field()
            age: int = Field(default=0)

        return User

    def test_select_all(self):
        User = self._make_model()
        sql, params = User.objects._build_select()
        assert "SELECT" in sql
        assert "FROM users" in sql
        assert params == []

    def test_select_with_filter(self):
        User = self._make_model()
        sql, params = User.objects.filter(name="Alice")._build_select()
        assert "WHERE name = $1" in sql
        assert params == ["Alice"]

    def test_select_with_multiple_filters(self):
        User = self._make_model()
        sql, params = User.objects.filter(name="Alice", age=25)._build_select()
        assert "name = $1" in sql
        assert "age = $2" in sql
        assert params == ["Alice", 25]

    def test_select_with_lookup(self):
        User = self._make_model()
        sql, params = User.objects.filter(age__gte=18)._build_select()
        assert "age >= $1" in sql
        assert params == [18]

    def test_select_with_ordering(self):
        User = self._make_model()
        sql, params = User.objects.order_by("-name")._build_select()
        assert "ORDER BY" in sql and "name DESC" in sql

    def test_select_with_limit_offset(self):
        User = self._make_model()
        sql, params = User.objects.limit(10).offset(20)._build_select()
        # LIMIT/OFFSET are bound params (so all pages share one compiled SQL).
        assert "LIMIT $1" in sql
        assert "OFFSET $2" in sql
        assert params == [10, 20]

    def test_pagination_collapses_to_one_sql(self):
        # Every page of a query must compile to the SAME SQL string (only the
        # bound LIMIT/OFFSET values differ) — otherwise each page is a distinct
        # SQL that bloats the compiled-SQL cache and the fixed native registry.
        User = self._make_model()
        seen = set()
        for page in range(500):
            sql, params = (
                User.objects.filter(age__gte=18)
                .order_by("id")
                .limit(20)
                .offset(page * 20)
                ._build_select()
            )
            seen.add(sql)
            assert params == [18, 20, page * 20]  # where param, LIMIT, OFFSET
        assert len(seen) == 1, f"pagination produced {len(seen)} distinct SQL strings"

    def test_count_query(self):
        User = self._make_model()
        sql, params = User.objects.filter(age__gte=18)._build_count()
        assert "SELECT COUNT(*)" in sql
        assert "FROM users" in sql
        assert "WHERE age >= $1" in sql

    def test_update_query(self):
        User = self._make_model()
        sql, params = User.objects.filter(id=1)._build_update({"name": "Bob"})
        assert "UPDATE users SET name = $1" in sql
        assert "WHERE id = $2" in sql
        assert params == ["Bob", 1]

    def test_delete_query(self):
        User = self._make_model()
        sql, params = User.objects.filter(id=1)._build_delete()
        assert "DELETE FROM users" in sql
        assert "WHERE id = $1" in sql
        assert params == [1]

    def test_chaining(self):
        User = self._make_model()
        sql, params = (
            User.objects.filter(age__gte=18)
            .filter(name="Alice")
            .order_by("name")
            .limit(5)
            ._build_select()
        )
        assert "age >= $1" in sql
        assert "name = $2" in sql
        assert "ORDER BY" in sql and "name ASC" in sql
        # LIMIT is a trailing bound param appended after the WHERE params.
        assert "LIMIT $3" in sql
        assert params == [18, "Alice", 5]

    def test_exclude(self):
        User = self._make_model()
        sql, params = User.objects.exclude(name="admin")._build_select()
        assert "NOT (name = $1)" in sql
        assert params == ["admin"]
