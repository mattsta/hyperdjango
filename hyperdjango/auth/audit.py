"""
Audit log for HyperApp — tracks all create/update/delete operations.

Stores JSON diffs of changes, user attribution, and timestamps.
Queryable history per object with dashboard integration.

Usage:
    from hyperdjango.auth.audit import AuditLog

    audit = AuditLog(db)
    await audit.ensure_table()

    # Log operations
    await audit.log_add(user_id=1, model="product", object_id="42", object_repr="Widget Pro")
    await audit.log_change(user_id=1, model="product", object_id="42", object_repr="Widget Pro",
                           changes={"price": {"old": 9.99, "new": 14.99}})
    await audit.log_delete(user_id=1, model="product", object_id="42", object_repr="Widget Pro")

    # Query history
    history = await audit.get_object_history("product", "42")
    recent = await audit.get_recent(limit=50)
    user_activity = await audit.get_user_activity(user_id=1, limit=20)
"""

from hyperdjango.native import fast_json_dumps

CREATE_AUDIT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hyper_audit_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER,
    username TEXT DEFAULT '',
    model_name TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_repr TEXT DEFAULT '',
    action TEXT NOT NULL,
    changes TEXT DEFAULT '',
    timestamp TIMESTAMPTZ DEFAULT NOW()
)
"""

CREATE_AUDIT_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_audit_model_obj ON hyper_audit_log (model_name, object_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_user ON hyper_audit_log (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON hyper_audit_log (timestamp DESC)",
]


class AuditLog:
    """Records create/update/delete operations with JSON diffs."""

    def __init__(self, db):
        self.db = db

    async def ensure_table(self):
        """Create the audit log table and indexes if they don't exist."""
        await self.db.execute(CREATE_AUDIT_TABLE_SQL)
        for sql in CREATE_AUDIT_INDEX_SQL:
            await self.db.execute(sql)

    # ── Logging operations ────────────────────────────────────────────────

    async def log_add(self, user_id, model, object_id, object_repr="", username=""):
        """Log a create operation."""
        await self.db.execute(
            "INSERT INTO hyper_audit_log (user_id, username, model_name, object_id, object_repr, action) "
            "VALUES ($1, $2, $3, $4, $5, 'add')",
            user_id,
            username,
            model,
            str(object_id),
            object_repr,
        )

    async def log_change(
        self, user_id, model, object_id, object_repr="", changes=None, username=""
    ):
        """Log an update operation with a JSON diff of changed fields."""
        changes_json = ""
        if changes:
            result = fast_json_dumps(changes)
            changes_json = (
                result.decode("utf-8") if isinstance(result, bytes) else result
            )
        await self.db.execute(
            "INSERT INTO hyper_audit_log (user_id, username, model_name, object_id, object_repr, action, changes) "
            "VALUES ($1, $2, $3, $4, $5, 'change', $6)",
            user_id,
            username,
            model,
            str(object_id),
            object_repr,
            changes_json,
        )

    async def log_delete(self, user_id, model, object_id, object_repr="", username=""):
        """Log a delete operation."""
        await self.db.execute(
            "INSERT INTO hyper_audit_log (user_id, username, model_name, object_id, object_repr, action) "
            "VALUES ($1, $2, $3, $4, $5, 'delete')",
            user_id,
            username,
            model,
            str(object_id),
            object_repr,
        )

    # ── Diff computation ──────────────────────────────────────────────────

    @staticmethod
    def compute_diff(old_values, new_values):
        """Compute a field-level diff between old and new values.

        Returns dict of changed fields: {field_name: {"old": ..., "new": ...}}
        """
        diff = {}
        all_keys = set(old_values) | set(new_values)
        for key in all_keys:
            old = old_values.get(key)
            new = new_values.get(key)
            if old != new:
                diff[key] = {"old": old, "new": new}
        return diff

    # ── Query operations ──────────────────────────────────────────────────

    async def get_object_history(self, model, object_id, limit=50):
        """Get the audit trail for a specific object."""
        rows = await self.db.query(
            "SELECT id, user_id, username, action, changes, timestamp "
            "FROM hyper_audit_log WHERE model_name = $1 AND object_id = $2 "
            "ORDER BY timestamp DESC LIMIT $3",
            model,
            str(object_id),
            limit,
        )
        cols = ["id", "user_id", "username", "action", "changes", "timestamp"]
        return [dict(zip(cols, r)) if not isinstance(r, dict) else r for r in rows]

    async def get_recent(self, limit=50):
        """Get the most recent audit log entries across all models."""
        rows = await self.db.query(
            "SELECT id, user_id, username, model_name, object_id, object_repr, action, changes, timestamp "
            "FROM hyper_audit_log ORDER BY timestamp DESC LIMIT $1",
            limit,
        )
        cols = [
            "id",
            "user_id",
            "username",
            "model_name",
            "object_id",
            "object_repr",
            "action",
            "changes",
            "timestamp",
        ]
        return [dict(zip(cols, r)) if not isinstance(r, dict) else r for r in rows]

    async def get_user_activity(self, user_id, limit=50):
        """Get audit entries for a specific user."""
        rows = await self.db.query(
            "SELECT id, model_name, object_id, object_repr, action, changes, timestamp "
            "FROM hyper_audit_log WHERE user_id = $1 ORDER BY timestamp DESC LIMIT $2",
            user_id,
            limit,
        )
        cols = [
            "id",
            "model_name",
            "object_id",
            "object_repr",
            "action",
            "changes",
            "timestamp",
        ]
        return [dict(zip(cols, r)) if not isinstance(r, dict) else r for r in rows]

    async def get_model_activity(self, model, limit=50):
        """Get audit entries for a specific model type."""
        rows = await self.db.query(
            "SELECT id, user_id, username, object_id, object_repr, action, changes, timestamp "
            "FROM hyper_audit_log WHERE model_name = $1 ORDER BY timestamp DESC LIMIT $2",
            model,
            limit,
        )
        cols = [
            "id",
            "user_id",
            "username",
            "object_id",
            "object_repr",
            "action",
            "changes",
            "timestamp",
        ]
        return [dict(zip(cols, r)) if not isinstance(r, dict) else r for r in rows]

    async def count(self, model=None):
        """Count total audit log entries, optionally filtered by model."""
        if model:
            return await self.db.query_val(
                "SELECT COUNT(*) FROM hyper_audit_log WHERE model_name = $1", model
            )
        return await self.db.query_val("SELECT COUNT(*) FROM hyper_audit_log")
