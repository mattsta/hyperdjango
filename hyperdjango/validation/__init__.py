# HyperForm/HyperSerializer pull in Django's forms/exceptions compat layer
# (~30ms import time) — an opt-in Django-migration feature, not needed by
# hyperdjango's own native validation path (which every app using models.py
# transitively imports). HyperModel requires Django apps to be ready (it
# inherits models.Model), so it's lazy for a different reason (avoiding
# ImproperlyConfigured errors) but the same __getattr__ mechanism covers
# both cases.


def __getattr__(name):
    if name == "HyperForm":
        from hyperdjango.validation.forms import HyperForm

        return HyperForm
    if name == "HyperSerializer":
        from hyperdjango.validation.serializers import HyperSerializer

        return HyperSerializer
    if name == "HyperModel":
        from hyperdjango.validation.model_validation import HyperModel

        return HyperModel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["HyperForm", "HyperSerializer", "HyperModel"]
