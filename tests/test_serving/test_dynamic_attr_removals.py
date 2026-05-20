"""Guardrails for the getattr/setattr enforcement sweep (SERVING/REST/WEB group).

Several defensive ``getattr(obj, "attr", default)`` sites were replaced with
direct attribute access because the attribute is provably always present:

- ``hyperdjango.rest.ViewSet`` / ``GenericAPIView`` declare
  ``field_permissions_model`` as a class attribute (rest.py), so
  ``self.field_permissions_model`` is safe.
- Django's ``ModelAdmin`` declares ``list_select_related``, ``raw_id_fields``,
  ``autocomplete_fields`` and ``inlines`` as class-level defaults, and
  ``InlineModelAdmin`` declares ``model = None`` — so ``admin_instance.<attr>``
  in ``hyperdjango.serving.admin`` never raises ``AttributeError``.

These tests fail loudly if a future refactor drops any of those declarations,
which would silently break the now-direct attribute access.
"""

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        INSTALLED_APPS=[
            "django.contrib.admin",
            "django.contrib.auth",
            "django.contrib.contenttypes",
        ],
        DATABASES={},
    )
    django.setup()


def test_view_classes_declare_field_permissions_model():
    """rest.py removed getattr(self/view, "field_permissions_model", "").

    Both ViewSet and GenericAPIView must declare the attribute (default "")
    for the direct-access refactor to be correct.
    """
    from hyperdjango.rest import GenericAPIView, ViewSet

    assert ViewSet.field_permissions_model == ""
    assert GenericAPIView.field_permissions_model == ""
    # Instances resolve it without AttributeError (what the direct access relies on).
    assert ViewSet().field_permissions_model == ""
    assert GenericAPIView().field_permissions_model == ""


def test_django_modeladmin_class_attr_defaults_present():
    """admin.py removed getattr on these Django ModelAdmin/InlineModelAdmin attrs.

    The direct-access refactor assumes Django ships them as class-level
    defaults; assert the exact defaults the removed getattr() calls fell back to.
    """
    from django.contrib.admin import ModelAdmin
    from django.contrib.admin.options import InlineModelAdmin

    assert ModelAdmin.list_select_related is False
    assert ModelAdmin.raw_id_fields == ()
    assert ModelAdmin.autocomplete_fields == ()
    assert ModelAdmin.inlines == ()
    assert InlineModelAdmin.model is None


def test_hyper_model_admin_direct_attr_access():
    """A HyperModelAdmin subclass exposes the admin config attrs by direct access.

    Mirrors what _collect_admin_relations / get_list_select_related now do
    (admin_instance.list_select_related, .raw_id_fields, .inlines, ...).
    """
    from hyperdjango.serving.admin import HyperModelAdmin

    class BareAdmin(HyperModelAdmin):
        pass

    # Access without getattr defaults — must not raise and must match Django's.
    assert BareAdmin.list_select_related is False
    assert tuple(BareAdmin.raw_id_fields) == ()
    assert tuple(BareAdmin.autocomplete_fields) == ()
    assert tuple(BareAdmin.inlines) == ()
