"""
pg.zig database backend for Django.

Drop-in replacement for django.db.backends.postgresql.
Uses native Zig PostgreSQL driver with psycopg fallback.

Usage:
    DATABASES = {
        'default': {
            'ENGINE': 'hyperdjango.db',
            'NAME': 'mydb',
            'USER': 'myuser',
            'PASSWORD': 'mypass',
            'HOST': 'localhost',
            'PORT': '5432',
        }
    }
"""

from hyperdjango.db.pgzig_connection import IntegrityError, is_unique_violation

__all__ = ["IntegrityError", "is_unique_violation"]
