"""Admin registration for test models."""

from django.contrib import admin

from tests.admin_app.models import Article, Category


class ArticleInline(admin.TabularInline):
    model = Article
    extra = 0
    fields = ["title", "status", "is_featured"]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "description"]
    search_fields = ["name"]
    inlines = [ArticleInline]


@admin.register(Article)
class ArticleAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "category",
        "status",
        "is_featured",
        "view_count",
        "created_at",
    ]
    list_filter = ["status", "is_featured", "category"]
    search_fields = ["title", "content"]
    list_editable = ["status", "is_featured"]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = [
        (None, {"fields": ["title", "content", "category"]}),
        ("Status", {"fields": ["status", "is_featured", "view_count"]}),
        ("Metadata", {"fields": ["metadata"], "classes": ["collapse"]}),
        ("Timestamps", {"fields": ["created_at", "updated_at"]}),
    ]
