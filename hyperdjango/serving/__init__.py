"""
HyperDjango serving layer — Django integration bridges.

Provides drop-in Django-compatible wrappers for HyperDjango's native components:

Template Backend:
    TEMPLATES = [{'BACKEND': 'hyperdjango.serving.template_backend.ZigTemplates', ...}]

Middleware:
    MIDDLEWARE = [
        'hyperdjango.serving.django_middleware.HyperCORSMiddleware',
        'hyperdjango.serving.django_middleware.HyperSecurityMiddleware',
        'hyperdjango.serving.django_middleware.HyperTimingMiddleware',
        'hyperdjango.serving.django_middleware.HyperRateLimitMiddleware',
        'hyperdjango.serving.django_middleware.HyperPerformanceMiddleware',
    ]

Auth Backend:
    AUTHENTICATION_BACKENDS = ['hyperdjango.serving.auth_backends.OAuth2Backend', ...]

Model Manager:
    from hyperdjango.serving.django_managers import HyperManager
    class MyModel(models.Model):
        objects = HyperManager()
"""
