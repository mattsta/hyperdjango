"""
Django settings for hyperdjango tests.
"""

SECRET_KEY = "test-secret-key-for-hyperdjango-do-not-use-in-production"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "hyperdjango",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

HYPERDJANGO_VALIDATION_BACKEND = "dhi"
HYPERDJANGO_FILE_ROUTING = False
HYPERDJANGO_STATIC_CACHE = False
HYPERDJANGO_HOT_RELOAD = False
HYPERDJANGO_PRE_VALIDATION = True

DEBUG = True

ROOT_URLCONF = "tests.urls"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ALLOWED_HOSTS = ["*"]
