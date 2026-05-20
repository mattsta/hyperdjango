import os

# Only set up Django if DJANGO_SETTINGS_MODULE is configured
settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
if settings_module:
    import django

    django.setup()
