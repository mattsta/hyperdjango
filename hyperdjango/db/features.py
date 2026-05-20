"""Database features — inherits from Django's PostgreSQL features.

pg.zig returns JSONB values as parsed Python objects (dict/list) rather than
raw JSON strings. We set a flag so Django's JSONField.from_db_value() knows
not to double-parse them.
"""

from django.db.backends.postgresql.features import (
    DatabaseFeatures as PgFeatures,
)


class DatabaseFeatures(PgFeatures):
    # pg.zig binary protocol returns JSONB as parsed Python dicts/lists.
    # Django's JSONField.from_db_value() must handle this.
    has_native_json_field = True
