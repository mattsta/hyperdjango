"""
HyperField wrappers — Django form fields with dhi constraint hints.

These are thin wrappers around Django form fields that carry explicit
dhi constraint metadata for optimal validation performance.

Usage:
    from hyperdjango.validation.fields import HyperCharField, HyperIntField

    class MyForm(HyperForm):
        name = HyperCharField(max_length=100, min_length=1)
        age = HyperIntField(min_value=0, max_value=150)
"""

from django import forms


class HyperCharField(forms.CharField):
    """CharField with explicit dhi constraint hints."""

    pass


class HyperIntField(forms.IntegerField):
    """IntegerField with explicit dhi constraint hints."""

    pass


class HyperFloatField(forms.FloatField):
    """FloatField with explicit dhi constraint hints."""

    pass


class HyperEmailField(forms.EmailField):
    """EmailField — maps directly to dhi.EmailStr."""

    pass


class HyperDecimalField(forms.DecimalField):
    """DecimalField with dhi max_digits/decimal_places constraints."""

    pass


class HyperURLField(forms.URLField):
    """URLField with dhi string constraints."""

    pass


class HyperSlugField(forms.SlugField):
    """SlugField with dhi pattern constraint."""

    pass


class HyperUUIDField(forms.UUIDField):
    """UUIDField — validated as string by dhi."""

    pass
