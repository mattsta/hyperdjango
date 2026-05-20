"""Integration tests for HyperDjango full stack."""


class TestFeatureDetection:
    def test_validation_core_exists(self):
        from hyperdjango.validation.core import BaseModel

        assert BaseModel is not None

    def test_validation_core_version(self):
        """The validation core should be importable."""
        from hyperdjango.validation.core import Field

        assert Field is not None


class TestHyperFormIntegration:
    """End-to-end HyperForm tests."""

    def test_form_creation_and_validation(self):
        from django import forms

        from hyperdjango.validation.forms import HyperForm

        class SignupForm(HyperForm):
            username = forms.CharField(min_length=3, max_length=30)
            email = forms.EmailField()
            password = forms.CharField(min_length=8)
            age = forms.IntegerField(min_value=13)

        # Valid data
        form = SignupForm(
            data={
                "username": "alice",
                "email": "alice@example.com",
                "password": "securepass123",
                "age": "25",
            }
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["username"] == "alice"
        assert form.cleaned_data["age"] == 25

    def test_form_validation_errors(self):
        from django import forms

        from hyperdjango.validation.forms import HyperForm

        class SignupForm(HyperForm):
            username = forms.CharField(min_length=3, max_length=30)
            email = forms.EmailField()

        # Invalid data
        form = SignupForm(
            data={
                "username": "ab",  # Too short
                "email": "not-an-email",
            }
        )
        assert not form.is_valid()


class TestHyperSerializerIntegration:
    """End-to-end HyperSerializer tests."""

    def test_serializer_validation(self):
        from django.db import models

        from hyperdjango.validation.serializers import HyperSerializer

        class Article(models.Model):
            title = models.CharField(max_length=200)
            content = models.TextField()
            published = models.BooleanField(default=False)

            class Meta:
                app_label = "tests"

        class ArticleSerializer(HyperSerializer):
            class Meta:
                model = Article
                fields = ["title", "content", "published"]

        s = ArticleSerializer(
            data={
                "title": "Hello World",
                "content": "This is a test article.",
                "published": True,
            }
        )
        assert s.is_valid(), s.errors


class TestStaticCache:
    """Test in-memory static file cache."""

    def test_cache_operations(self):
        import tempfile
        from pathlib import Path

        from hyperdjango.streaming.static_cache import InMemoryStaticCache

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.css"
            test_file.write_text("body { color: red; }")

            cache = InMemoryStaticCache([tmpdir])
            result = cache.get("test.css")
            assert result is not None
            content, content_type = result
            assert b"body" in content
            assert "css" in content_type
            assert cache.size == 1

    def test_cache_miss(self):
        from hyperdjango.streaming.static_cache import InMemoryStaticCache

        cache = InMemoryStaticCache(["/nonexistent"])
        assert cache.get("missing.css") is None
