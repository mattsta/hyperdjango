"""Tests for HyperSerializer — dhi-backed model serialization."""

from django.db import models

from hyperdjango.validation.serializers import HyperSerializer


# Test models — defined in-memory for SQLite testing
class SimpleUser(models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    age = models.PositiveIntegerField()
    bio = models.TextField(blank=True, default="")

    class Meta:
        app_label = "tests"


class TestHyperSerializerDefinition:
    """Test serializer class creation."""

    def test_serializer_creates_dhi_model(self):
        class UserSerializer(HyperSerializer):
            class Meta:
                model = SimpleUser
                fields = ["name", "email", "age"]

        assert UserSerializer._dhi_model is not None
        assert UserSerializer._model is SimpleUser
        assert UserSerializer._field_names == ["name", "email", "age"]

    def test_serializer_all_fields(self):
        class UserSerializer(HyperSerializer):
            class Meta:
                model = SimpleUser
                fields = "__all__"

        assert "name" in UserSerializer._field_names
        assert "email" in UserSerializer._field_names


class TestHyperSerializerValidation:
    """Test validation via dhi."""

    def test_valid_data(self):
        class UserSerializer(HyperSerializer):
            class Meta:
                model = SimpleUser
                fields = ["name", "email", "age"]

        s = UserSerializer(
            data={"name": "Alice", "email": "alice@example.com", "age": 25}
        )
        assert s.is_valid(), s.errors
        assert s.validated_data["name"] == "Alice"
        assert s.validated_data["age"] == 25

    def test_invalid_data(self):
        class UserSerializer(HyperSerializer):
            class Meta:
                model = SimpleUser
                fields = ["name", "email", "age"]

        s = UserSerializer(data={"name": "", "email": "not-email", "age": -1})
        assert not s.is_valid()

    def test_no_data(self):
        class UserSerializer(HyperSerializer):
            class Meta:
                model = SimpleUser
                fields = ["name"]

        s = UserSerializer(data=None)
        assert not s.is_valid()
        assert "non_field_errors" in s.errors

    def test_many_validation(self):
        class UserSerializer(HyperSerializer):
            class Meta:
                model = SimpleUser
                fields = ["name", "age"]

        data = [
            {"name": "Alice", "age": 25},
            {"name": "Bob", "age": 30},
        ]
        s = UserSerializer(data=data, many=True)
        assert s.is_valid(), s.errors
        assert len(s.validated_data) == 2


class TestHyperSerializerSerialization:
    """Test output serialization."""

    def test_data_from_validated(self):
        class UserSerializer(HyperSerializer):
            class Meta:
                model = SimpleUser
                fields = ["name", "age"]

        s = UserSerializer(data={"name": "Alice", "age": 25})
        s.is_valid()
        assert s.data == {"name": "Alice", "age": 25}
