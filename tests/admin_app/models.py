"""Test models for admin E2E testing.

These models exercise Django ORM field types that admin must introspect:
- CharField, TextField, IntegerField, BooleanField, DateTimeField
- ForeignKey (many-to-one)
- JSONField
- TextChoices (enum)
"""

from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default="")

    class Meta:
        app_label = "admin_app"
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Article(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    title = models.CharField(max_length=200)
    content = models.TextField()
    category = models.ForeignKey(
        Category, on_delete=models.CASCADE, related_name="articles"
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    is_featured = models.BooleanField(default=False)
    view_count = models.IntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "admin_app"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
