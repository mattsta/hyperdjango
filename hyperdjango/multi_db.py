"""
Multi-database routing — manage multiple named database connections.

Supports read replicas, per-model database binding, and explicit database
selection via QuerySet.using().

Usage:
    from hyperdjango.multi_db import ConnectionManager, DatabaseRouter

    # Register named connections
    connections = ConnectionManager()
    await connections.configure({
        "default": "postgres://localhost/myapp",
        "replica": "postgres://replica-host/myapp",
        "analytics": "postgres://analytics-host/warehouse",
    })

    # Route reads to replica, writes to primary
    class MyRouter(DatabaseRouter):
        def db_for_read(self, model):
            return "replica"
        def db_for_write(self, model):
            return "default"

    connections.router = MyRouter()

    # Per-model binding
    class AnalyticsEvent(Model):
        class Meta:
            table = "events"
            database = "analytics"

    # Explicit selection
    users = await User.objects.using("replica").filter(active=True).all()
"""

import contextlib
import threading

from hyperdjango.database import Database, set_db

# ---------------------------------------------------------------------------
# Connection Manager — named database pool registry
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages multiple named database connections.

    Each connection is a Database instance with its own pool.
    The "default" connection is used when no explicit database is specified.
    """

    def __init__(self):
        self._databases: dict[str, Database] = {}
        self._configs: dict[str, str] = {}
        self.router: DatabaseRouter | None = None

    async def configure(self, databases: dict[str, str | dict]):
        """Configure and connect to multiple databases.

        Args:
            databases: Dict of name -> URL string or config dict.
                Config dict keys: url, min_size, max_size
        """
        # Open every pool first, tracking what THIS call opened. If any
        # connect() fails, close the pools we already opened before re-raising
        # — otherwise a partial failure leaks live connections that no longer
        # have any owner. Registration is all-or-nothing for this batch.
        opened: list[tuple[str, str, Database]] = []
        try:
            for name, config in databases.items():
                if isinstance(config, str):
                    url = config
                    db = Database(url)
                else:
                    url = config["url"]
                    db = Database(
                        url,
                        min_size=config.get("min_size", 2),
                        max_size=config.get("max_size", 10),
                    )
                await db.connect()
                opened.append((name, url, db))
        except Exception:
            for _name, _url, db in opened:
                with contextlib.suppress(Exception):
                    await db.disconnect()
            raise

        for name, url, db in opened:
            self._databases[name] = db
            self._configs[name] = url

        # Set the default global db
        if "default" in self._databases:
            set_db(self._databases["default"])

    def __getitem__(self, name: str) -> Database:
        """Get a database connection by name."""
        if name not in self._databases:
            raise KeyError(
                f"Database '{name}' not configured. "
                f"Available: {', '.join(self._databases.keys())}"
            )
        return self._databases[name]

    def __contains__(self, name: str) -> bool:
        return name in self._databases

    @property
    def databases(self) -> dict[str, Database]:
        """All configured databases."""
        return dict(self._databases)

    def get(self, name: str, default: Database | None = None) -> Database | None:
        """Get a database connection by name, or default."""
        return self._databases.get(name, default)

    def resolve_for_read(self, model_class=None) -> Database:
        """Resolve which database to use for reads.

        Priority:
        1. model_class.Meta.database (if set)
        2. router.db_for_read(model_class) (if router configured)
        3. "default"
        """
        # Check per-model binding
        if model_class is not None:
            # dynamic-attr: probing an arbitrary caller-supplied ``model_class`` for an optional ``_meta`` (it may be a non-Model object)
            meta = getattr(model_class, "_meta", None)
            if meta and hasattr(meta, "database") and meta.database:
                return self[meta.database]

        # Check router
        if self.router is not None and model_class is not None:
            db_name = self.router.db_for_read(model_class)
            if db_name is not None:
                return self[db_name]

        return self["default"]

    def resolve_for_write(self, model_class=None) -> Database:
        """Resolve which database to use for writes.

        Priority:
        1. model_class.Meta.database (if set)
        2. router.db_for_write(model_class) (if router configured)
        3. "default"
        """
        if model_class is not None:
            # dynamic-attr: probing an arbitrary caller-supplied ``model_class`` for an optional ``_meta`` (it may be a non-Model object)
            meta = getattr(model_class, "_meta", None)
            if meta and hasattr(meta, "database") and meta.database:
                return self[meta.database]

        if self.router is not None and model_class is not None:
            db_name = self.router.db_for_write(model_class)
            if db_name is not None:
                return self[db_name]

        return self["default"]

    async def close_all(self):
        """Close all database connections."""
        for db in self._databases.values():
            await db.disconnect()
        self._databases.clear()
        self._configs.clear()


# ---------------------------------------------------------------------------
# Database Router — route reads/writes to different databases
# ---------------------------------------------------------------------------


class DatabaseRouter:
    """Base class for database routers.

    Override db_for_read() and db_for_write() to control routing.
    Return None to fall through to the next router or default.
    """

    def db_for_read(self, model) -> str | None:
        """Return the database alias to use for reads, or None for default."""
        return None

    def db_for_write(self, model) -> str | None:
        """Return the database alias to use for writes, or None for default."""
        return None

    def allow_relation(self, obj1, obj2) -> bool | None:
        """Return True/False to allow/deny cross-db relations, None to defer."""
        return None

    def allow_migrate(self, db: str, model) -> bool | None:
        """Return True/False to allow/deny migration, None to defer."""
        return None


# ---------------------------------------------------------------------------
# Built-in routers
# ---------------------------------------------------------------------------


class PrimaryReplicaRouter(DatabaseRouter):
    """Routes all reads to a replica, all writes to primary (default).

    Args:
        replica: Name of the replica database (default: "replica")
        primary: Name of the primary database (default: "default")
    """

    def __init__(self, replica: str = "replica", primary: str = "default"):
        self.replica = replica
        self.primary = primary

    def db_for_read(self, model) -> str:
        return self.replica

    def db_for_write(self, model) -> str:
        return self.primary


# ---------------------------------------------------------------------------
# Global connection manager (singleton)
# ---------------------------------------------------------------------------

_connections: ConnectionManager | None = None
_connections_lock = threading.Lock()


def get_connections() -> ConnectionManager:
    """Get the global connection manager."""
    global _connections
    # Double-checked locking: a racing first-call on free-threaded Python would
    # otherwise build two managers, and callers holding different registries
    # would route to different pools. The lock publishes exactly one.
    conns = _connections
    if conns is not None:
        return conns
    with _connections_lock:
        if _connections is None:
            _connections = ConnectionManager()
        return _connections


def set_connections(manager: ConnectionManager):
    """Set the global connection manager."""
    global _connections
    _connections = manager
