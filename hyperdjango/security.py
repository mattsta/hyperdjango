"""
Security event audit log.

Tracks security-relevant events separate from admin CRUD audit log:
- Authentication: login success/failure, logout, password change
- Authorization: permission denied, CSRF violation
- Rate limiting: rate limit exceeded
- Sessions: session created/destroyed, fixation attempt
- Suspicious: SQL injection attempt, XSS attempt, path traversal

Uses PostgreSQL UNLOGGED table for fast writes with multi-server visibility.

Usage:
    from hyperdjango.security import SecurityLog, SecurityEvent

    log = SecurityLog(db)
    await log.ensure_table()

    # Log events
    await log.log(SecurityEvent.LOGIN_SUCCESS, user_id=42, ip="1.2.3.4")
    await log.log(SecurityEvent.LOGIN_FAILED, ip="1.2.3.4", detail="invalid password")
    await log.log(SecurityEvent.PERMISSION_DENIED, user_id=42, detail="missing edit_product")
    await log.log(SecurityEvent.RATE_LIMIT_HIT, ip="1.2.3.4", detail="100/min exceeded")

    # Query
    events = await log.get_recent(limit=50)
    user_events = await log.get_for_user(42)
    ip_events = await log.get_for_ip("1.2.3.4")
    failed_logins = await log.get_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)
"""

import contextlib
import enum
import threading


class SecurityEvent(enum.Enum):
    """Security event types."""

    # Authentication
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"

    # Authorization
    PERMISSION_DENIED = "permission_denied"
    CSRF_VIOLATION = "csrf_violation"
    AUTH_REQUIRED = "auth_required"

    # Rate limiting
    RATE_LIMIT_HIT = "rate_limit_hit"

    # Sessions
    SESSION_CREATED = "session_created"
    SESSION_DESTROYED = "session_destroyed"
    SESSION_EXPIRED = "session_expired"
    SESSION_FIXATION_ATTEMPT = "session_fixation_attempt"

    # Suspicious activity
    SUSPICIOUS_INPUT = "suspicious_input"
    PATH_TRAVERSAL_ATTEMPT = "path_traversal_attempt"
    INVALID_TOKEN = "invalid_token"


CREATE_SECURITY_LOG_SQL = """
CREATE UNLOGGED TABLE IF NOT EXISTS hyper_security_log (
    id SERIAL PRIMARY KEY,
    event TEXT NOT NULL,
    user_id INTEGER,
    ip_address TEXT,
    user_agent TEXT,
    path TEXT,
    detail TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

CREATE_SECURITY_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_seclog_event ON hyper_security_log (event)",
    "CREATE INDEX IF NOT EXISTS idx_seclog_user ON hyper_security_log (user_id) WHERE user_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_seclog_ip ON hyper_security_log (ip_address) WHERE ip_address IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_seclog_ts ON hyper_security_log (timestamp DESC)",
]


class SecurityLog:
    """Security event audit log backed by PostgreSQL UNLOGGED table.

    UNLOGGED = no WAL writes, fast for high-volume security events.
    Survives process restarts but not crashes (acceptable for logs).

    Write batching (opt-in):
        ``SecurityLog(db, batch_size=N)`` buffers events and writes them in a
        single multi-row INSERT once ``N`` have accumulated. This cuts write
        amplification during a brute-force burst (one INSERT per failed login
        becomes one INSERT per N). ``batch_size=1`` (the default) preserves the
        original write-through behaviour.

        Buffered events live in memory until flushed, so they share the same
        durability envelope as the UNLOGGED table (lost on crash). Call
        :meth:`flush` on shutdown / periodically to bound that window.
    """

    def __init__(self, db, *, batch_size: int = 1):
        self.db = db
        self._batch_size = max(1, batch_size)
        # Pending (event, user_id, ip, user_agent, path, detail) tuples.
        self._buffer: list[tuple] = []
        self._buffer_lock = threading.Lock()

    async def ensure_table(self):
        """Create the security log table if it doesn't exist."""
        try:
            await self.db.execute(CREATE_SECURITY_LOG_SQL)
        # blind-except: UNLOGGED unsupported on older PostgreSQL, so retry as a regular table; a genuine error (permissions/connectivity) re-raises from that retry rather than being swallowed.
        except Exception:
            # Fall back to regular table if UNLOGGED not supported
            await self.db.execute(
                CREATE_SECURITY_LOG_SQL.replace("UNLOGGED TABLE", "TABLE")
            )
        for sql in CREATE_SECURITY_INDEXES_SQL:
            with contextlib.suppress(Exception):
                await self.db.execute(sql)

    async def log(
        self,
        event: SecurityEvent,
        *,
        user_id: int | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        path: str | None = None,
        detail: str | None = None,
    ):
        """Log a security event.

        With ``batch_size == 1`` this writes immediately. With a larger batch
        size the event is buffered and flushed as one multi-row INSERT once the
        buffer fills.
        """
        row = (event.value, user_id, ip, user_agent, path, detail)

        if self._batch_size <= 1:
            await self._write_rows([row])
            return

        to_flush: list[tuple] | None = None
        with self._buffer_lock:
            self._buffer.append(row)
            if len(self._buffer) >= self._batch_size:
                to_flush = self._buffer
                self._buffer = []
        if to_flush:
            await self._write_rows(to_flush)

    async def flush(self):
        """Write any buffered events immediately. Safe to call when empty."""
        with self._buffer_lock:
            to_flush = self._buffer
            self._buffer = []
        if to_flush:
            await self._write_rows(to_flush)

    async def _write_rows(self, rows: list[tuple]):
        """Insert one or more event rows in a single statement."""
        if not rows:
            return
        cols = "(event, user_id, ip_address, user_agent, path, detail)"
        placeholders = []
        params: list = []
        for i, row in enumerate(rows):
            base = i * 6
            placeholders.append(
                f"(${base + 1}, ${base + 2}, ${base + 3}, "
                f"${base + 4}, ${base + 5}, ${base + 6})"
            )
            params.extend(row)
        sql = f"INSERT INTO hyper_security_log {cols} VALUES " + ", ".join(placeholders)
        await self.db.execute(sql, *params)

    async def log_from_request(
        self,
        event: SecurityEvent,
        request,
        *,
        user_id: int | None = None,
        detail: str | None = None,
    ):
        """Log a security event, extracting IP/UA/path from a request object."""
        ip = request.client_ip
        user_agent = request.headers.get("user-agent", "")[:500]
        path = request.path

        # Auto-detect user_id from request if not provided
        if user_id is None:
            user = request.user
            if user is not None:
                user_id = user.id

        await self.log(
            event,
            user_id=user_id,
            ip=ip,
            user_agent=user_agent,
            path=path,
            detail=detail,
        )

    # --- Query methods ---

    async def get_recent(self, limit: int = 100) -> list[dict[str, int | str | None]]:
        """Get the most recent security events."""
        rows = await self.db.query(
            "SELECT id, event, user_id, ip_address, user_agent, path, detail, timestamp "
            "FROM hyper_security_log ORDER BY timestamp DESC LIMIT $1",
            limit,
        )
        return [self._row_to_dict(row) for row in rows]

    async def get_for_user(
        self, user_id: int, limit: int = 100
    ) -> list[dict[str, int | str | None]]:
        """Get security events for a specific user."""
        rows = await self.db.query(
            "SELECT id, event, user_id, ip_address, user_agent, path, detail, timestamp "
            "FROM hyper_security_log WHERE user_id = $1 ORDER BY timestamp DESC LIMIT $2",
            user_id,
            limit,
        )
        return [self._row_to_dict(row) for row in rows]

    async def get_for_ip(
        self, ip: str, limit: int = 100
    ) -> list[dict[str, int | str | None]]:
        """Get security events from a specific IP address."""
        rows = await self.db.query(
            "SELECT id, event, user_id, ip_address, user_agent, path, detail, timestamp "
            "FROM hyper_security_log WHERE ip_address = $1 ORDER BY timestamp DESC LIMIT $2",
            ip,
            limit,
        )
        return [self._row_to_dict(row) for row in rows]

    async def get_by_event(
        self,
        event: SecurityEvent,
        since_hours: int = 24,
        limit: int = 100,
    ) -> list[dict[str, int | str | None]]:
        """Get events of a specific type within a time window."""
        rows = await self.db.query(
            "SELECT id, event, user_id, ip_address, user_agent, path, detail, timestamp "
            "FROM hyper_security_log "
            "WHERE event = $1 AND timestamp > NOW() - $2 * INTERVAL '1 hour' "
            "ORDER BY timestamp DESC LIMIT $3",
            event.value,
            int(since_hours),
            limit,
        )
        return [self._row_to_dict(row) for row in rows]

    async def count_by_event(self, event: SecurityEvent, since_hours: int = 1) -> int:
        """Count events of a specific type within a time window.

        Useful for detecting brute-force attacks:
            count = await log.count_by_event(SecurityEvent.LOGIN_FAILED, since_hours=1)
            if count > 100:
                # Lock out or alert
        """
        return await self.db.query_val(
            "SELECT COUNT(*) FROM hyper_security_log "
            "WHERE event = $1 AND timestamp > NOW() - $2 * INTERVAL '1 hour'",
            event.value,
            int(since_hours),
        )

    async def count_by_ip(
        self, ip: str, event: SecurityEvent, since_hours: int = 1
    ) -> int:
        """Count events from a specific IP within a time window.

        Useful for IP-level brute-force detection.
        """
        return await self.db.query_val(
            "SELECT COUNT(*) FROM hyper_security_log "
            "WHERE ip_address = $1 AND event = $2 "
            "AND timestamp > NOW() - $3 * INTERVAL '1 hour'",
            ip,
            event.value,
            int(since_hours),
        )

    async def cleanup(self, days: int = 90):
        """Delete security log entries older than N days."""
        await self.db.execute(
            "DELETE FROM hyper_security_log "
            "WHERE timestamp < NOW() - $1 * INTERVAL '1 day'",
            int(days),
        )

    @staticmethod
    def _row_to_dict(row) -> dict[str, int | str | None]:
        cols = [
            "id",
            "event",
            "user_id",
            "ip_address",
            "user_agent",
            "path",
            "detail",
            "timestamp",
        ]
        if isinstance(row, dict):
            return {c: row.get(c) for c in cols}
        return dict(zip(cols, row))


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_security_log: SecurityLog | None = None


def get_security_log() -> SecurityLog | None:
    """Get the global security log instance."""
    return _security_log


def set_security_log(log: SecurityLog):
    """Set the global security log instance."""
    global _security_log
    _security_log = log
