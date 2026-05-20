"""
Model layer — dhi BaseModel + database integration.

Models are dhi BaseModel subclasses with database metadata.
Validation is built into the model layer — no separate form/serializer needed.

No Django dependency.

Usage:
    from hyperdjango import Model, Field
    from hyperdjango.models import ManyToManyField
    from hyperdjango.validation.core import EmailStr

    class User(Model):
        class Meta:
            table = "users"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field()
        email: EmailStr = Field(unique=True)
        age: int = Field(ge=0, le=150, default=0)

    class Tag(Model):
        class Meta:
            table = "tags"
        id: int = Field(primary_key=True, auto=True)
        name: str = Field()

    class Article(Model):
        class Meta:
            table = "articles"
        id: int = Field(primary_key=True, auto=True)
        title: str = Field()
        author_id: int = Field(foreign_key=User)       # FK to User model
        tags: ClassVar = ManyToManyField(Tag)           # M2M via junction table
"""

import contextlib
import enum
import logging
import re
import types
import typing
from dataclasses import dataclass, field

from hyperdjango.database import get_db
from hyperdjango.multi_db import get_connections
from hyperdjango.query import (
    QuerySet,
    _get_model_by_table,
    _model_registry,
    _register_model,
)
from hyperdjango.query_cache import get_query_cache
from hyperdjango.signals import (
    log_robust_responses,
    post_delete,
    post_save,
    pre_delete,
    pre_save,
)
from hyperdjango.validation.core import BaseModel as _BaseModel
from hyperdjango.validation.core import Field as _DhiField
from hyperdjango.validation.core import FieldInfo
from hyperdjango.validation.core.fields import _MISSING
from hyperdjango.where import WhereNode

_logger = logging.getLogger("hyperdjango.models")


def _resolve_instance_db(model_cls, *, for_write: bool):
    """Resolve the database connection for an instance-level operation.

    Mirrors ``QuerySet._get_db``: honors ``Meta.database`` per-model binding
    and the configured read/write router via the live connection manager, so
    instance ``save()``/``delete()``/``refresh_from_db()`` route to the SAME
    connection the QuerySet path would — never silently to the global default
    (which caused a read-here/write-there split-brain for models bound to a
    non-default database). Falls back to ``get_db()`` when no multi-database
    manager is configured.
    """
    try:
        conns = get_connections()
        if conns is not None and conns._databases:
            if for_write:
                return conns.resolve_for_write(model_cls)
            return conns.resolve_for_read(model_cls)
    except ImportError, KeyError:
        pass
    return get_db()


@dataclass(frozen=True, slots=True)
class DatabaseDefault:
    """Marker for a DATABASE-side column default expressed as raw SQL.

    Pass an instance as ``Field(db_default=...)`` when the default must be
    evaluated by PostgreSQL on INSERT rather than by Python. The wrapped
    string is emitted verbatim into the column's ``DEFAULT <expr>`` clause,
    so it can be any valid SQL expression — a function call, a constant, or
    a more complex expression.

    Examples:
        id: str = Field(primary_key=True,
                        db_default=DatabaseDefault("gen_random_uuid()"))
        created_at: datetime = Field(db_default=DatabaseDefault("now()"))
        region: str = Field(db_default=DatabaseDefault("current_setting('app.region')"))

    A plain Python literal (``db_default=0``, ``db_default="active"``) is
    SQL-quoted/escaped automatically and does NOT need this wrapper — use
    ``DatabaseDefault`` only when you want raw, un-quoted SQL.
    """

    sql: str


def _singularize(word: str) -> str:
    """Simple English singularization for table names.

    Handles common plural patterns:
    - addresses → address (drop -es after -ss, -sh, -ch, -x, -z)
    - categories → category (-ies → -y)
    - statuses → status (-ses → -s for -us words)
    - books → book (drop -s)
    """
    if len(word) <= 1:
        return word
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("ses") and word[:-2].endswith(("s", "u")):
        return word[:-2]
    if word.endswith("es") and word[:-2].endswith(("ss", "sh", "ch", "x", "z")):
        return word[:-2]
    if word.endswith("es") and len(word) > 3:
        return word[:-1]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _table_to_fk_col(table_name: str) -> str:
    """Derive a FK column name from a table name.

    "test_books" -> "book_id"
    "users" -> "user_id"
    "addresses" -> "address_id"
    "categories" -> "category_id"
    """
    parts = table_name.split("_")
    base = parts[-1] if len(parts) > 1 else parts[0]
    return f"{_singularize(base)}_id"


# Registry: maps id(FieldInfo) -> db metadata dict


def Field(
    default=_MISSING,
    *,
    primary_key=False,
    auto=False,
    unique=False,
    index=False,
    editable=True,
    foreign_key=None,
    related_name=None,
    on_delete=None,
    db_default=_MISSING,
    **kwargs,
):
    """Create a FieldInfo with optional database metadata.

    All metadata is stored directly on the FieldInfo dataclass — no external
    registries or wrappers.

    Special handling for default="now()":
        - DB side: generates DEFAULT now() in CREATE TABLE (auto-set on INSERT)
        - Python side: uses datetime.now(UTC) as the default value (not the string)
        The value is set once when the row is created and never updated.

    Args:
        primary_key: This field is the primary key.
        auto: Auto-increment (SERIAL).
        unique: UNIQUE constraint.
        index: Create an index.
        editable: When False, the field is NEVER writable through a
            ModelSerializer — always read-only, even under ``fields="__all__"``.
            Privileged code may still assign it directly on the instance. Use it
            to protect security-sensitive columns (e.g. ``is_staff``,
            ``is_superuser``, ``password_hash``) from mass assignment via a
            request body.
        foreign_key: Table name this FK points to (e.g., "users").
        related_name: Name for the reverse relation on the target model.
            Defaults to "{model_name}s" (lowercase + "s").
        on_delete: FK action on parent delete: "CASCADE", "SET NULL", "RESTRICT".
        db_default: A DATABASE-level column default, evaluated by PostgreSQL on
            INSERT (distinct from the Python-side ``default``). Accepts either a
            Python literal (e.g. ``0``, ``"active"``, ``True``) which is
            SQL-quoted/escaped automatically, or a :class:`DatabaseDefault`
            wrapping a raw SQL expression (e.g.
            ``DatabaseDefault("gen_random_uuid()")``,
            ``DatabaseDefault("now()")``). A column with a ``db_default`` (PK
            or not) is omitted from INSERT when no explicit value is supplied,
            letting the DB fill it; the generated value is read back after save.
    """
    # Handle default="now()" — split into Python default + DB default
    resolved_db_default = _MISSING
    if default == "now()":
        from datetime import UTC
        from datetime import datetime as _datetime

        resolved_db_default = DatabaseDefault("now()")  # for DDL generation
        default = _MISSING  # let default_factory handle Python side
        kwargs["default_factory"] = lambda: _datetime.now(UTC)

    # An explicit db_default= always wins over the inferred "now()" default.
    if db_default is not _MISSING:
        resolved_db_default = db_default

    field_info = _DhiField(
        default,
        primary_key=primary_key,
        auto=auto,
        unique=unique,
        index=index,
        editable=editable,
        foreign_key=foreign_key,
        related_name=related_name,
        on_delete=on_delete,
        **kwargs,
    )

    # Store db_default for DDL generation (read by generate_ddl_for_model and
    # cli.py). The FieldInfo slot is typed str | None, but at runtime it
    # carries either a DatabaseDefault marker or a Python literal
    # — _db_default_to_sql() below normalizes both into a DEFAULT clause.
    if resolved_db_default is not _MISSING:
        field_info.db_default = resolved_db_default

    return field_info


def VectorField(
    dimensions: int = 1536,
    *,
    index_type: str = "hnsw",
    index_ops: str = "vector_cosine_ops",
    index_params: dict[str, int] | None = None,
    index: bool = True,
    **kwargs,
):
    """Create a vector field for pgvector embeddings.

    Stores fixed-dimension float32 vectors in PostgreSQL using the pgvector
    extension. Supports HNSW and IVFFlat indexing for sub-millisecond
    approximate nearest neighbor search.

    Args:
        dimensions: Fixed vector dimension (e.g., 1536 for OpenAI ada-002,
            768 for sentence-transformers, 3072 for text-embedding-3-large).
        index_type: Index algorithm — "hnsw" (default, faster queries) or
            "ivfflat" (faster builds, lower memory).
        index_ops: PostgreSQL operator class for distance metric:
            - "vector_cosine_ops" (default) — cosine distance (<=>)
            - "vector_l2_ops" — Euclidean/L2 distance (<->)
            - "vector_ip_ops" — inner product (<#>)
        index_params: Index tuning parameters for WITH clause.
            HNSW: {"m": 16, "ef_construction": 64}
            IVFFlat: {"lists": 100}
        index: Create a vector index (default True).

    Usage:
        class Document(Model):
            class Meta:
                table = "documents"
            id: int = Field(primary_key=True, auto=True)
            title: str = Field()
            embedding: list[float] = VectorField(
                dimensions=1536,
                index_params={"m": 16, "ef_construction": 64},
            )

        # Query by similarity:
        docs = await Document.objects.filter(
            embedding__cosine_distance=(query_vec, 0.2)
        ).all()
    """
    return _DhiField(
        default=_MISSING,
        index=index,
        vector_dimensions=dimensions,
        vector_index_type=index_type,
        vector_index_ops=index_ops,
        vector_index_params=index_params,
        **kwargs,
    )


def _resolve_fk(fk: str | type | None) -> str | None:
    """Resolve a foreign_key value to a table name string.

    Accepts:
      - None → None
      - Model class (with _meta.table) → table name string
      - "table_name" or "table_name.column" → passed through
      - "ClassName" (PascalCase, no dots/underscores) → resolved via model registry
    """
    if fk is None:
        return None
    if isinstance(fk, type):
        meta = fk.__dict__.get("_meta")
        if meta is None:
            msg = f"foreign_key={fk.__name__} is not a Model class (no _meta)"
            raise TypeError(msg)
        return meta.table
    # String: check if it's a forward class name reference (PascalCase)
    if fk and fk[0].isupper() and "_" not in fk and "." not in fk:
        for table_name, model_cls in _model_registry.items():
            if model_cls.__name__ == fk:
                return table_name
    return fk


def _get_db_meta(field_info):
    """Get db metadata from the FieldInfo directly.

    Resolves class-based FK references (e.g., foreign_key=User) to table
    name strings at metaclass time. All downstream consumers receive plain
    string table names.
    """
    return {
        "primary_key": field_info.primary_key,
        "auto": field_info.auto,
        "unique": field_info.unique,
        "index": field_info.index,
        "editable": field_info.editable,
        "foreign_key": _resolve_fk(field_info.foreign_key),
        "related_name": field_info.related_name,
        "one_to_one": field_info.one_to_one,
        "on_delete": field_info.on_delete,
        "has_db_default": field_info.db_default is not None,
    }


def OneToOneField(
    foreign_key: str | type,
    *,
    related_name: str | None = None,
    default=_MISSING,
    index: bool = True,
    **kwargs,
):
    """A ForeignKey with UNIQUE constraint — one-to-one relationship.

    The column stores an integer FK, and the UNIQUE constraint ensures at most
    one row can reference each target row.

    Usage:
        class UserProfile(Model):
            class Meta:
                table = "profiles"
            id: int = Field(primary_key=True, auto=True)
            user_id: int = OneToOneField("users", related_name="profile")
            bio: str = Field(default="")

        # Forward access (standard FK):
        profile = await UserProfile.objects.filter(user_id=user.id).first()

        # Reverse access via select_related or manual query:
        user_with_profile = await User.objects.select_related("profile").first()
    """
    return _DhiField(
        default,
        unique=True,
        index=index,
        foreign_key=foreign_key,
        related_name=related_name,
        one_to_one=True,
        **kwargs,
    )


# ── File / Image field registry ──────────────────────────────────────────


def FileField(
    upload_to: str = "uploads/",
    default: str = "",
    **kwargs,
):
    """A string field that stores a file path relative to the storage root.

    All metadata stored directly on the FieldInfo via its upload_to and
    file_field_type attributes — no external registries or wrappers.

    Usage:
        class Product(Model):
            class Meta:
                table = "products"
            id: int = Field(primary_key=True, auto=True)
            image: str = FileField(upload_to="products/")
            document: str = FileField(upload_to="docs/")

        # Save an uploaded file:
        from hyperdjango.models import save_uploaded_file
        path = await save_uploaded_file(product, "image", content, "photo.jpg", storage)
        await db.execute("UPDATE products SET image = $1 WHERE id = $2", path, product.id)
    """
    return _DhiField(
        default,
        upload_to=upload_to.rstrip("/") + "/",
        file_field_type="file",
        **kwargs,
    )


def ImageField(
    upload_to: str = "images/",
    default: str = "",
    allowed_extensions: tuple[str, ...] = (
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
    ),
    **kwargs,
):
    """A FileField that validates image file extensions.

    ``.svg`` is NOT allowed by default: SVG is active content (it can carry
    ``<script>``) and served as ``image/svg+xml`` is a stored-XSS vector. Only
    add it to ``allowed_extensions`` if you serve user SVGs as an attachment or
    behind a restrictive CSP.

    Usage:
        class Product(Model):
            photo: str = ImageField(upload_to="products/photos/")
    """
    return _DhiField(
        default,
        upload_to=upload_to.rstrip("/") + "/",
        file_field_type="image",
        allowed_extensions=allowed_extensions,
        **kwargs,
    )


def _get_file_meta(field_info):
    """Get file field metadata from the FieldInfo directly."""
    if field_info.file_field_type is None:
        return None
    return {
        "upload_to": field_info.upload_to,
        "field_type": field_info.file_field_type,
        "allowed_extensions": field_info.allowed_extensions,
    }


def _is_file_field(field_info) -> bool:
    """Check if a field is a FileField or ImageField."""
    return field_info.file_field_type is not None


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_upload_filename(filename: str) -> str:
    """Reduce a user-supplied upload filename to a safe basename.

    Strips directory components (path traversal via ``/`` or ``\\``), null bytes
    and control characters, replaces any char outside ``[A-Za-z0-9._-]`` with
    ``_``, and removes leading dots (no ``..`` / hidden dotfiles). Falls back to
    ``"upload"`` when nothing safe remains.
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = name.replace("\x00", "")
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)
    name = name.lstrip(".")
    return name or "upload"


async def save_uploaded_file(
    model_instance,
    field_name: str,
    content: bytes,
    filename: str,
    storage,
) -> str:
    """Save an uploaded file and set the model field to the resulting path.

    Returns the stored file path (relative to storage root).
    """
    # Find the field's file metadata directly from the FieldInfo
    field_info = type(model_instance).__dict__.get(field_name)
    upload_to = (
        field_info.upload_to
        if field_info is not None and field_info.upload_to
        else "uploads/"
    )

    # Reduce the user-supplied filename to a safe basename BEFORE any use: strip
    # directory components (path traversal), null bytes/control chars, and keep a
    # conservative allowlist. The storage layer also confines writes to its root,
    # but the stored name must never carry attacker-chosen path structure.
    filename = _sanitize_upload_filename(filename)

    # Validate image extensions
    if field_info is not None and field_info.file_field_type == "image":
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        allowed = field_info.allowed_extensions
        if allowed and ext not in allowed:
            raise ValueError(
                f"File extension '{ext}' not allowed. Allowed: {', '.join(allowed)}"
            )

    # Build path: upload_to/filename
    save_path = upload_to + filename
    stored_path = await storage.save(save_path, content)

    # dynamic-attr: assigning a file path onto an arbitrary user model instance's field named by the runtime ``field_name`` string
    setattr(model_instance, field_name, stored_path)
    return stored_path


async def delete_uploaded_file(
    model_instance,
    field_name: str,
    storage,
):
    """Delete the file referenced by the model field and clear the field value."""
    # dynamic-attr: reading an arbitrary user model instance's file field named by the runtime ``field_name`` string
    current_path = getattr(model_instance, field_name)
    if current_path:
        with contextlib.suppress(FileNotFoundError, OSError):
            await storage.delete(current_path)
    # dynamic-attr: clearing an arbitrary user model instance's file field named by the runtime ``field_name`` string
    setattr(model_instance, field_name, "")


# ---------------------------------------------------------------------------
# ManyToManyField — descriptor for M2M relations
# ---------------------------------------------------------------------------


class ManyToManyField:
    """Descriptor for many-to-many relationships.

    Creates a junction table and provides async add/remove/all/clear/set methods.

    Usage on the class:
        class Article(Model):
            tags: ClassVar = ManyToManyField("tags")

    Usage on instances:
        await article.tags.add(tag1, tag2)
        await article.tags.remove(tag1)
        all_tags = await article.tags.all()
        await article.tags.clear()
        await article.tags.set([tag1, tag2])
    """

    _is_m2m = True

    def __init__(
        self,
        target: str | type,
        junction_table: str | None = None,
        related_name: str | None = None,
    ):
        if isinstance(target, type):
            meta = target.__dict__.get("_meta")
            if meta is None:
                msg = f"ManyToManyField target {target.__name__} is not a Model class"
                raise TypeError(msg)
            self._target_table_name = meta.table
        else:
            self._target_table_name = target
        self._custom_junction_table = junction_table
        self._related_name = related_name
        # Set during model registration
        self._source_table = None
        self._source_model = None
        self._target_model = None
        self._junction_table = None
        self._source_col = None
        self._target_col = None
        self._field_name = None

    def _configure(self, source_model, field_name):
        """Called by ModelMeta to finalize the M2M configuration."""
        self._source_model = source_model
        self._source_table = source_model._meta.table
        self._field_name = field_name

        # Target model resolution (deferred — model might not be registered yet)
        self._target_model = _get_model_by_table(self._target_table_name)

        # Junction table name
        if self._custom_junction_table:
            self._junction_table = self._custom_junction_table
        else:
            tables = sorted([self._source_table, self._target_table_name])
            self._junction_table = f"{tables[0]}_{tables[1]}"

        # Column names in junction table — derive from table name
        # Strip common prefixes like "test_" and pluralization suffix "s"
        self._source_col = _table_to_fk_col(self._source_table)
        self._target_col = _table_to_fk_col(self._target_table_name)

    def _ensure_target(self):
        """Resolve target model lazily (in case it was registered after us)."""
        if self._target_model is None:
            self._target_model = _get_model_by_table(self._target_table_name)
            if self._target_model is None:
                raise ValueError(
                    f"M2M target table '{self._target_table_name}' not found in model registry"
                )

    def __set_name__(self, owner, name):
        """Called when the descriptor is assigned to a class attribute."""
        self._field_name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        # Return a bound M2M manager for this instance
        self._ensure_target()
        return M2MManager(
            instance=obj,
            source_model=self._source_model or objtype,
            target_model=self._target_model,
            junction_table=self._junction_table,
            source_col=self._source_col,
            target_col=self._target_col,
            field_name=self._field_name,
        )

    @property
    def create_table_sql(self) -> str:
        """SQL to create the junction table."""
        return (
            f"CREATE TABLE IF NOT EXISTS {self._junction_table} ("
            f"  {self._source_col} INTEGER NOT NULL,"
            f"  {self._target_col} INTEGER NOT NULL,"
            f"  PRIMARY KEY ({self._source_col}, {self._target_col})"
            f")"
        )


class M2MManager:
    """Bound M2M manager for a specific model instance.

    Provides async CRUD for the M2M junction table.
    """

    def __init__(
        self,
        instance,
        source_model,
        target_model,
        junction_table,
        source_col,
        target_col,
        field_name,
    ):
        self._instance = instance
        self._source_model = source_model
        self._target_model = target_model
        self._junction_table = junction_table
        self._source_col = source_col
        self._target_col = target_col
        self._field_name = field_name

    def _get_source_pk(self):
        # dynamic-attr: reading the source user model instance's PK, whose column name (``pk_field``) is only known at runtime from that model's meta
        return getattr(self._instance, self._source_model._meta.pk_field)

    def _get_db(self):
        # Route the junction-table connection the same way the QuerySet /
        # instance paths do — honoring the source model's Meta.database and the
        # write router — so M2M add/remove/clear/set never write to the global
        # default while the model's own rows live in another database. Reads
        # (all/count) share the write connection: always correct, and it keeps
        # a relationship's reads and writes on the same database.
        return _resolve_instance_db(self._source_model, for_write=True)

    async def all(self) -> list[object]:
        """Get all related objects."""
        # Check prefetch cache first
        # dynamic-attr: framework-injected prefetch cache attribute whose name is built from the runtime M2M field name
        cache = getattr(self._instance, f"_{self._field_name}_cache", None)
        if cache is not None:
            return cache

        db = self._get_db()
        source_pk = self._get_source_pk()
        target_table = self._target_model._meta.table
        target_pk = self._target_model._meta.pk_field
        target_cols = ", ".join(
            f"{target_table}.{c}" for c in self._target_model._meta.column_names
        )

        sql = (
            f"SELECT {target_cols} FROM {self._junction_table} "
            f"JOIN {target_table} ON {self._junction_table}.{self._target_col} = {target_table}.{target_pk} "
            f"WHERE {self._junction_table}.{self._source_col} = $1"
        )
        rows = await db.query(sql, source_pk)
        return [self._target_model.from_record(row) for row in rows]

    async def add(self, *objects):
        """Add objects to the M2M relationship (batch INSERT)."""
        if not objects:
            return
        db = self._get_db()
        source_pk = self._get_source_pk()
        target_pk_field = self._target_model._meta.pk_field

        # Batch INSERT with multiple VALUES rows in one query.
        # SAFETY: No SQL injection risk — table/column names come from Model._meta
        # (developer-defined, not user input). All values use $N parameterized
        # placeholders, never string interpolation. The f-string constructs only
        # the $N placeholder references (e.g., "$1, $2"), not actual values.
        # dynamic-attr: reading each related user model instance's PK, whose column name (``target_pk_field``) comes from the target model's meta at runtime
        target_pks = [getattr(obj, target_pk_field) for obj in objects]
        value_clauses = []
        params = [source_pk]
        for i, tpk in enumerate(target_pks):
            value_clauses.append(f"($1, ${i + 2})")
            params.append(tpk)
        sql = (
            f"INSERT INTO {self._junction_table} ({self._source_col}, {self._target_col}) "
            f"VALUES {', '.join(value_clauses)} ON CONFLICT DO NOTHING"
        )
        await db.execute(sql, *params)

        # Invalidate cache
        if hasattr(self._instance, f"_{self._field_name}_cache"):
            object.__delattr__(self._instance, f"_{self._field_name}_cache")

    async def remove(self, *objects):
        """Remove objects from the M2M relationship."""
        db = self._get_db()
        source_pk = self._get_source_pk()
        target_pk_field = self._target_model._meta.pk_field

        for obj in objects:
            # dynamic-attr: reading each related user model instance's PK, whose column name (``target_pk_field``) comes from the target model's meta at runtime
            target_pk = getattr(obj, target_pk_field)
            sql = (
                f"DELETE FROM {self._junction_table} "
                f"WHERE {self._source_col} = $1 AND {self._target_col} = $2"
            )
            await db.execute(sql, source_pk, target_pk)

        if hasattr(self._instance, f"_{self._field_name}_cache"):
            object.__delattr__(self._instance, f"_{self._field_name}_cache")

    async def clear(self):
        """Remove all M2M relationships for this instance."""
        db = self._get_db()
        source_pk = self._get_source_pk()
        sql = f"DELETE FROM {self._junction_table} WHERE {self._source_col} = $1"
        await db.execute(sql, source_pk)

        if hasattr(self._instance, f"_{self._field_name}_cache"):
            object.__delattr__(self._instance, f"_{self._field_name}_cache")

    async def set(self, objects):
        """Replace all M2M relationships with the given objects.

        Uses a transaction to ensure atomic clear+add — if the process
        crashes between clear and add, the transaction rolls back.
        """
        db = self._get_db()
        async with db.transaction():
            await self.clear()
            if objects:
                await self.add(*objects)

    async def count(self) -> int:
        """Count related objects."""
        db = self._get_db()
        source_pk = self._get_source_pk()
        sql = (
            f"SELECT COUNT(*) FROM {self._junction_table} WHERE {self._source_col} = $1"
        )
        return await db.query_val(sql, source_pk)


# ---------------------------------------------------------------------------
# Metaclass and metadata
# ---------------------------------------------------------------------------


def _setup_from_record_fast_path(new_class: type) -> None:
    """Precompute the from_record() fast-path attributes for a Model class.

    Called from ModelMeta.__new__ after class fields are compiled. Computes:
      - __dhi_from_record_fast__: True when __init__ can be bypassed on DB reads
      - __dhi_enum_coercers__: dict[field_name, enum_cls] for enum coercion
      - __dhi_enum_coercer_items__: tuple of (field_name, enum_cls) for fast iteration
      - __dhi_plain_field_names__: tuple of field names with NO coercion (bulk loop)

    pg.zig already returns natively-typed values (datetime/int/str/float/bool),
    so validator dispatch on DB hydration is pure overhead for simple models.
    The ORM from_record path bypasses __init__ when safe.
    """
    # Fast path eligibility gate
    nested_specs = new_class.__dhi_nested_field_specs__ or ()
    has_nested_model_field = any(spec[7] for spec in nested_specs)
    new_class.__dhi_from_record_fast__ = (
        not new_class.__dhi_has_custom_validators__
        and not new_class.__dhi_needs_post_init__
        and not has_nested_model_field
    )

    # Enum coercer precomputation: fields annotated as Enum or Enum|None must
    # hold enum instances, not raw str/int values returned by pg.zig. This
    # makes `model.status == SomeEnum.X` work correctly.
    enum_coercers: dict[str, type] = {}
    try:
        hints = typing.get_type_hints(new_class)
    # blind-except: best-effort type-hint introspection at class-build time; unresolvable annotations yield no enum coercers rather than failing to define the class.
    except Exception:
        hints = {}
    for fname in new_class.__dhi_field_names__:
        hint = hints.get(fname)
        if hint is None:
            continue
        if isinstance(hint, type) and issubclass(hint, enum.Enum):
            enum_coercers[fname] = hint
            continue
        origin = typing.get_origin(hint)
        if origin is typing.Union or origin is types.UnionType:
            for arg in typing.get_args(hint):
                if (
                    arg is not type(None)
                    and isinstance(arg, type)
                    and issubclass(arg, enum.Enum)
                ):
                    enum_coercers[fname] = arg
                    break
    new_class.__dhi_enum_coercers__ = enum_coercers
    new_class.__dhi_plain_field_names__ = tuple(
        f for f in new_class.__dhi_field_names__ if f not in enum_coercers
    )
    new_class.__dhi_enum_coercer_items__ = tuple(enum_coercers.items())
    # Precomputed frozenset of field names for from_record()'s per-row
    # __pydantic_fields_set__ init. Copying a frozenset (set(frozenset)) reuses
    # the stored element hashes via CPython's set_merge fast path — cheaper than
    # rebuilding the set from the list and re-hashing every field name per row.
    # We still copy (not share) because __setattr__ mutates the per-instance set
    # via .add() when validate_assignment=True.
    new_class.__dhi_field_names_set__ = frozenset(new_class.__dhi_field_names__)


class ModelMeta(type(_BaseModel)):
    """Metaclass that sets up database metadata and the QuerySet manager.

    Supports three inheritance patterns:
    - Abstract models (Meta.abstract = True): share fields without creating a table
    - Proxy models (Meta.proxy = True): same table, different Python class
    - Concrete inheritance: each model gets its own table with its own fields
    """

    def __new__(mcs, name, bases, namespace, **kwargs):
        new_class = super().__new__(mcs, name, bases, namespace, **kwargs)

        # Skip the base Model class
        if name == "Model" and not any(hasattr(b, "_meta") for b in bases):
            return new_class

        # Extract Meta — a user-defined inner class whose every option is optional.
        # dynamic-attr: reflecting over an arbitrary user model's optional inner ``Meta`` class
        meta = namespace.get("Meta") or getattr(new_class, "Meta", None)
        # dynamic-attr: ``abstract`` is an optional flag on the user-defined Meta class
        is_abstract = getattr(meta, "abstract", False)
        # dynamic-attr: ``proxy`` is an optional flag on the user-defined Meta class
        is_proxy = getattr(meta, "proxy", False)
        # dynamic-attr: ``sti`` is an optional flag on the user-defined Meta class
        is_sti = getattr(meta, "sti", False)

        # Abstract models: build field metadata but skip table/QuerySet/registration
        # Fields are inherited by concrete subclasses
        if is_abstract:
            abstract_fields = {}
            abstract_pk = None
            abstract_auto = None

            # Inherit from parent abstract models first
            for base in bases:
                if hasattr(base, "_meta") and base._meta.abstract:
                    abstract_fields.update(base._meta.fields)
                    if base._meta.pk_field and base._meta.pk_field in base._meta.fields:
                        abstract_pk = base._meta.pk_field
                    if base._meta.auto_field:
                        abstract_auto = base._meta.auto_field

            # Add this abstract model's own fields
            for field_name, annotation in new_class.__annotations__.items():
                if field_name.startswith("_"):
                    continue
                field_info = new_class.__dict__.get(field_name)
                if isinstance(field_info, ManyToManyField):
                    continue
                db_meta = (
                    _get_db_meta(field_info)
                    if isinstance(field_info, FieldInfo)
                    else {}
                )
                meta_entry = FieldMeta.from_db_meta(field_name, db_meta)
                abstract_fields[field_name] = meta_entry
                if meta_entry.primary_key:
                    abstract_pk = field_name
                if meta_entry.auto:
                    abstract_auto = field_name

            new_class._meta = TableMeta(
                table="",
                pk_field=abstract_pk or "id",
                auto_field=abstract_auto,
                fields=abstract_fields,
                abstract=True,
                proxy=False,
                parents=[],
            )
            return new_class

        # Find parent models with _meta (for inheritance)
        parent_models = []
        parent_fields = {}
        for base in bases:
            if hasattr(base, "_meta"):
                parent_models.append(base)
                # Abstract parents contribute their fields to the concrete subclass
                if base._meta.abstract:
                    parent_fields.update(base._meta.fields)

        # Proxy models: reuse parent table, inherit all parent fields
        if is_proxy:
            parent = None
            for base in bases:
                if (
                    hasattr(base, "_meta")
                    and not base._meta.abstract
                    and base._meta.table
                ):
                    parent = base
                    break
            if parent is None:
                raise TypeError(
                    f"Proxy model '{name}' must inherit from a concrete Model"
                )

            new_class._meta = TableMeta(
                table=parent._meta.table,
                pk_field=parent._meta.pk_field,
                auto_field=parent._meta.auto_field,
                fields=dict(parent._meta.fields),
                abstract=False,
                proxy=True,
                parents=[parent],
            )
            new_class.objects = QuerySet(new_class)
            # Register proxy under the same table name
            _register_model(parent._meta.table + "__proxy__" + name.lower(), new_class)
            _setup_from_record_fast_path(new_class)
            return new_class

        # Single-Table Inheritance (STI): child shares parent's table
        # with a discriminator column to distinguish row types.
        if is_sti:
            parent = None
            for base in bases:
                if (
                    hasattr(base, "_meta")
                    and not base._meta.abstract
                    and not base._meta.proxy
                ):
                    parent = base
                    break
            if parent is None:
                raise TypeError(
                    f"STI model '{name}' must inherit from a concrete Model"
                )

            # dynamic-attr: ``sti_column`` is an optional override on the user-defined Meta class
            sti_column = getattr(meta, "sti_column", "type")
            # dynamic-attr: ``sti_type`` is an optional override on the user-defined Meta class
            sti_type = getattr(meta, "sti_type", name.lower())

            # Inherit parent fields + add child's own fields
            child_fields = dict(parent._meta.fields)

            # Ensure discriminator column exists in fields
            if sti_column not in child_fields:
                child_fields[sti_column] = FieldMeta(
                    name=sti_column,
                    index=True,
                )

            # Add this child's own fields (nullable columns on shared table)
            for field_name, annotation in new_class.__annotations__.items():
                if field_name.startswith("_"):
                    continue
                field_info = new_class.__dict__.get(field_name)
                if isinstance(field_info, ManyToManyField):
                    continue
                db_meta = (
                    _get_db_meta(field_info)
                    if isinstance(field_info, FieldInfo)
                    else {}
                )
                child_fields[field_name] = FieldMeta.from_db_meta(field_name, db_meta)

            new_class._meta = TableMeta(
                table=parent._meta.table,
                pk_field=parent._meta.pk_field,
                auto_field=parent._meta.auto_field,
                fields=child_fields,
                abstract=False,
                proxy=False,
                parents=[parent],
                sti_column=sti_column,
                sti_type=sti_type,
            )

            # Also set STI info on the parent if not already set
            if parent._meta.sti_column is None:
                parent._meta.sti_column = sti_column
                # dynamic-attr: reflecting over the parent user model's optional inner ``Meta`` class and its optional ``sti_type``
                parent._meta.sti_type = getattr(
                    # dynamic-attr: the parent user model's inner ``Meta`` is optional
                    getattr(parent, "Meta", None),
                    "sti_type",
                    parent.__name__.lower(),
                )
                # Add discriminator to parent fields if missing
                if sti_column not in parent._meta.fields:
                    parent._meta.fields[sti_column] = FieldMeta(
                        name=sti_column,
                        index=True,
                    )
                    # fields dict just changed — drop the parent's memoized
                    # column tuples so column_names/writable_columns rebuild.
                    parent._meta.invalidate_column_cache()

            # Track child fields on parent for DDL generation (hyper setup
            # creates all columns on the shared table). Stored as
            # (child_class, FieldMeta) so the CLI can resolve types
            # from the child's annotations.
            for fname, fmeta in child_fields.items():
                if fname not in parent._meta.fields:
                    parent._meta.sti_child_fields[fname] = (new_class, fmeta)

            # Create a filtered QuerySet that auto-adds WHERE type = 'child_type'
            class STIQuerySet(QuerySet):
                def _build_where_tree(self, table_alias=None, join_aliases=None):
                    root = super()._build_where_tree(table_alias, join_aliases)
                    root.children.append(
                        WhereNode(
                            template=f"{sti_column} = {{}}",
                            bind_values=[sti_type],
                        )
                    )
                    return root

                def _mixin_cache_key(self):
                    return ("sti", sti_column, sti_type) + super()._mixin_cache_key()

                def _collect_mixin_params(self, params):
                    super()._collect_mixin_params(params)
                    params.append(sti_type)

            new_class.objects = STIQuerySet(new_class)
            _register_model(parent._meta.table + "__sti__" + name.lower(), new_class)
            _setup_from_record_fast_path(new_class)
            return new_class

        # Concrete model — build field metadata from annotations
        # Each of these is an optional attribute on the user-defined inner Meta class.
        # dynamic-attr: optional ``table`` override on the user-defined Meta class
        table_name = getattr(meta, "table", None) or name.lower() + "s"
        # dynamic-attr: optional ``database`` routing name on the user-defined Meta class
        database_name = getattr(meta, "database", None)
        # dynamic-attr: optional ``cache_ttl`` on the user-defined Meta class
        cache_ttl = getattr(meta, "cache_ttl", None)
        # dynamic-attr: optional ``unlogged`` flag on the user-defined Meta class
        unlogged = getattr(meta, "unlogged", False)
        # dynamic-attr: optional ``append_only`` flag on the user-defined Meta class
        append_only = getattr(meta, "append_only", False)
        # dynamic-attr: optional ``unique_together`` on the user-defined Meta class
        unique_together = getattr(meta, "unique_together", [])
        # dynamic-attr: optional ``indexes`` on the user-defined Meta class
        indexes = list(getattr(meta, "indexes", []))

        fields_meta = {}
        pk_field = None
        auto_field = None

        # Start with inherited abstract parent fields
        fields_meta.update(parent_fields)
        if parent_fields:
            for f in parent_fields.values():
                if f.primary_key:
                    pk_field = f.name
                if f.auto:
                    auto_field = f.name

        # Add this class's own fields
        for field_name, annotation in new_class.__annotations__.items():
            if field_name.startswith("_"):
                continue

            field_info = new_class.__dict__.get(field_name)

            # Skip M2M fields — they're descriptors, not columns
            if isinstance(field_info, ManyToManyField):
                continue

            db_meta = (
                _get_db_meta(field_info) if isinstance(field_info, FieldInfo) else {}
            )

            meta_entry = FieldMeta.from_db_meta(field_name, db_meta)
            fields_meta[field_name] = meta_entry

            if meta_entry.primary_key:
                pk_field = field_name
            if meta_entry.auto:
                auto_field = field_name

        # Default primary key
        if pk_field is None:
            pk_field = "id"

        # Pre-compute excluded fields (Field(exclude=True)) for to_dict() performance
        _excluded = frozenset(
            fname
            for fname in fields_meta
            if (fobj := new_class.__dict__.get(fname)) is not None
            and isinstance(fobj, FieldInfo)
            and fobj.exclude
        )

        # Pre-compute vector columns (VectorField) so the write path
        # can convert list[float] → pgvector bracket format without a
        # runtime type lookup per INSERT.
        _vector_columns = frozenset(
            fname
            for fname in fields_meta
            if isinstance((fobj := new_class.__dict__.get(fname)), FieldInfo)
            and fobj.vector_dimensions is not None
        )

        new_class._meta = TableMeta(
            table=table_name,
            pk_field=pk_field,
            auto_field=auto_field,
            fields=fields_meta,
            abstract=False,
            proxy=False,
            unlogged=unlogged,
            append_only=append_only,
            parents=parent_models,
            database=database_name,
            cache_ttl=cache_ttl,
            unique_together=unique_together,
            indexes=indexes,
            excluded_fields=_excluded,
            vector_columns=_vector_columns,
        )

        # Attach QuerySet manager — collect ALL custom QuerySet classes from bases
        # and compose them via dynamic multiple inheritance. This ensures mixins
        # like TenantMixin + SoftDeleteMixin both get their _build_where_tree()
        # called via MRO chain (each calls super()._build_where_tree()).
        qs_classes: list[type] = []
        for base in bases:
            # dynamic-attr: probing an arbitrary base class for the optional framework ``_queryset_class`` marker (defined or inherited on mixins like TenantMixin/SoftDeleteMixin)
            qs_cls = getattr(base, "_queryset_class", None)
            if (
                qs_cls is not None
                and qs_cls is not QuerySet
                and qs_cls not in qs_classes
            ):
                qs_classes.append(qs_cls)

        if len(qs_classes) == 0:
            queryset_class = QuerySet
        elif len(qs_classes) == 1:
            queryset_class = qs_classes[0]
        else:
            # Compose: create a class inheriting from all custom QuerySets.
            # MRO order matches mixin declaration order, so _build_where_tree()
            # chains correctly: TenantQS -> SoftDeleteQS -> VersionedQS -> QuerySet
            composed_name = "_".join(c.__name__ for c in qs_classes)
            queryset_class = type(composed_name, tuple(qs_classes), {})

        new_class.objects = queryset_class(new_class)

        # Register in model registry for FK resolution
        _register_model(table_name, new_class)

        # Resolve any forward-reference FKs that may now be resolvable.
        # When Model B references "ModelA" by class name, and ModelA is
        # registered later, this sweep resolves the pending reference.
        for _reg_cls in _model_registry.values():
            if not _reg_cls._meta.fields:
                continue
            for _fmeta in _reg_cls._meta.fields.values():
                fk = _fmeta.foreign_key
                if (
                    isinstance(fk, str)
                    and fk
                    and fk[0].isupper()
                    and "_" not in fk
                    and "." not in fk
                ):
                    if new_class.__name__ == fk:
                        _fmeta.foreign_key = table_name

        # Configure M2M fields
        for attr_name in list(namespace.keys()):
            attr = namespace.get(attr_name)
            if isinstance(attr, ManyToManyField):
                attr._configure(new_class, attr_name)

        # Register FK dependencies for query cache invalidation
        with contextlib.suppress(Exception):
            get_query_cache().register_model(new_class)

        _setup_from_record_fast_path(new_class)
        return new_class


_SAFE_INDEX_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Index:
    """Declarative index definition for Model Meta.indexes.

    Usage in Model Meta::

        class Meta:
            table = "my_table"
            indexes = [
                Index(fields=("tenant_id", "status_id"), where="is_deleted = FALSE"),
                Index(fields=("tenant_id", "-created_at")),  # DESC
                Index(fields=("tenant_id", "ticket_number"), unique=True),
                Index(fields=("title",), using="gin", opclasses=("gin_trgm_ops",)),
                Index(expressions=("to_tsvector('english', title || ' ' || description)",),
                      using="gin", name="ix_my_table_search"),
            ]
    """

    fields: tuple[str, ...] = ()
    name: str | None = None
    unique: bool = False
    using: str = "btree"
    where: str | None = None
    opclasses: tuple[str, ...] = ()
    include: tuple[str, ...] = ()
    params: dict[str, int | str] | None = None
    expressions: tuple[str, ...] = ()


def _index_ddl_name(table: str, idx: Index) -> str:
    """Return the index name ``_generate_index_ddl`` would emit for ``idx``.

    An explicit ``Index(name=...)`` wins; otherwise a deterministic
    ``{idx|uq}_{table}_{cols}`` slug (truncated to Postgres' 63-char limit) is
    derived. Exposed so the migration differ can match a declared Meta index
    against an already-created live index by name and stay idempotent, without
    re-implementing the naming rule.
    """
    if idx.name:
        return idx.name
    prefix = "uq" if idx.unique else "idx"
    col_slug = "_".join(f.lstrip("-") for f in idx.fields) if idx.fields else "expr"
    name = f"{prefix}_{table}_{col_slug}"
    return name[:63] if len(name) > 63 else name


def _generate_index_ddl(table: str, idx: Index) -> str:
    """Generate CREATE INDEX DDL from an Index definition.

    All identifiers (table name, column names) are validated against
    _SAFE_INDEX_IDENT_RE. Expression indexes bypass column validation
    since they contain raw SQL (e.g., to_tsvector calls).
    """
    if not _SAFE_INDEX_IDENT_RE.match(table):
        raise ValueError(f"Unsafe table name in index: {table!r}")

    # Build column list
    if idx.expressions:
        col_parts = list(idx.expressions)
    else:
        col_parts = []
        for i, f in enumerate(idx.fields):
            desc = f.startswith("-")
            col_name = f.lstrip("-")
            if not _SAFE_INDEX_IDENT_RE.match(col_name):
                raise ValueError(f"Unsafe column name in index: {col_name!r}")
            part = col_name
            if i < len(idx.opclasses) and idx.opclasses[i]:
                part += f" {idx.opclasses[i]}"
            if desc:
                part += " DESC"
            col_parts.append(part)

    # Auto-generate name if not provided (single source of truth for the rule).
    name = _index_ddl_name(table, idx)

    # Build SQL
    unique = "UNIQUE " if idx.unique else ""
    using = f" USING {idx.using}" if idx.using != "btree" else ""
    cols = ", ".join(col_parts)
    sql = f"CREATE {unique}INDEX IF NOT EXISTS {name} ON {table}{using} ({cols})"

    # INCLUDE columns
    if idx.include:
        include_cols = ", ".join(idx.include)
        sql += f" INCLUDE ({include_cols})"

    # WITH parameters
    if idx.params:
        with_parts = ", ".join(f"{k} = {v}" for k, v in idx.params.items())
        sql += f" WITH ({with_parts})"

    # WHERE clause
    if idx.where:
        sql += f" WHERE {idx.where}"

    return sql


@dataclass(slots=True)
class FieldMeta:
    """Database metadata for a model field."""

    name: str
    primary_key: bool = False
    auto: bool = False
    unique: bool = False
    index: bool = False
    editable: bool = (
        True  # False → never writable via ModelSerializer (mass-assignment guard)
    )
    foreign_key: str | None = None
    related_name: str | None = None
    one_to_one: bool = False
    on_delete: str | None = None  # CASCADE, SET NULL, RESTRICT, etc.
    has_db_default: bool = False  # column has a DB-side DEFAULT (db_default=)

    @classmethod
    def from_db_meta(cls, name: str, db_meta: dict) -> FieldMeta:
        """Build a FieldMeta from a ``_get_db_meta()`` dict — the SINGLE
        construction point.

        ``_get_db_meta`` produces exactly this dataclass's fields (minus ``name``),
        so a new field attribute is added in ONE place (``_get_db_meta`` + the
        dataclass) and flows to every model path automatically, instead of being
        hand-threaded through each ``FieldMeta(...)`` call site (which silently
        drifts — a missed site is how ``editable`` regressed). An empty dict
        (non-FieldInfo entries) yields all defaults.
        """
        return cls(name=name, **db_meta)


@dataclass(slots=True)
class TableMeta:
    """Database metadata for a model."""

    table: str
    pk_field: str  # First/only PK field (single-PK fast path)
    auto_field: str | None
    fields: dict[str, FieldMeta]
    abstract: bool = False
    proxy: bool = False
    unlogged: bool = False  # CREATE UNLOGGED TABLE (no WAL, fast, lost on crash)
    append_only: bool = False  # DB-enforced append-only: a BEFORE UPDATE/DELETE trigger
    # RAISEs, so audit/consent/outcome rows are immutable even to a stray ORM .update()/
    # .delete() or a hand SQL session (forensic integrity by construction, not convention)
    parents: list[type] = field(default_factory=list)
    database: str | None = None  # Named database for multi-db routing
    cache_ttl: int | None = None  # Default query cache TTL in seconds
    sti_column: str | None = None  # Discriminator column name (STI)
    sti_type: str | None = None  # Discriminator value for this model (STI)
    sti_child_fields: dict = field(
        default_factory=dict
    )  # Child-specific fields for DDL
    unique_together: list[tuple[str, ...]] = field(
        default_factory=list
    )  # Composite UNIQUE constraints
    excluded_fields: frozenset[str] = field(
        default_factory=frozenset
    )  # Fields with Field(exclude=True), pre-computed at class creation
    indexes: list[Index] = field(default_factory=list)  # Meta.indexes definitions
    vector_columns: frozenset[str] = field(
        default_factory=frozenset
    )  # VectorField columns — Model._insert/_update must serialize
    # list[float] values as pgvector bracket-format strings
    # (`[0.1,0.2,...]`), NOT as PG array literals (`{0.1,0.2,...}`)
    # which is what pg.zig's generic list-handling path produces.

    # Memoized immutable column tuples — the hot row-hydration paths
    # (select_related, annotations, from_record) touch these once per row
    # × per related model, so a per-access ``list(fields.keys())`` rebuild
    # is measurable overhead. Computed lazily on first access and cached;
    # ``invalidate_column_cache()`` clears them when ``fields`` mutates
    # (STI discriminator injection). init=False so they stay out of the
    # generated ``__init__`` signature.
    _column_names_cache: tuple[str, ...] | None = field(default=None, init=False)
    _writable_columns_cache: tuple[str, ...] | None = field(default=None, init=False)

    # Memoized write-SQL templates. save()/delete() otherwise rebuild the
    # INSERT/UPDATE/DELETE string (", ".join + f-strings) on every call, unlike
    # QuerySet._build_* which compile once. Each cache is keyed by an immutable
    # tuple that changes whenever the model's columns change (INSERT by the
    # actual per-call column subset, UPDATE by writable_columns), so a column
    # mutation (STI discriminator injection) naturally lands on a fresh key —
    # no stale template is ever served and no explicit invalidation is needed.
    # DELETE depends only on the (fixed) table + PK, so it caches one string.
    # Single dict.get / dict[key]= ops are atomic on this free-threaded build;
    # the memo is idempotent (same key → byte-identical SQL) so a concurrent
    # miss at worst recomputes an identical value — never corrupts.
    _insert_sql_cache: dict[tuple[str, ...], tuple[str, str | None]] = field(
        default_factory=dict, init=False
    )
    _update_sql_cache: dict[tuple[str, ...], tuple[tuple[str, ...], str]] = field(
        default_factory=dict, init=False
    )
    _delete_sql_cache: str | None = field(default=None, init=False)

    @property
    def pk_fields(self) -> list[str]:
        """All primary key fields. For composite PKs, returns multiple entries."""
        pks = [name for name, f in self.fields.items() if f.primary_key]
        return pks or [self.pk_field]

    @property
    def is_composite_pk(self) -> bool:
        """True if this model has a composite (multi-field) primary key."""
        return len(self.pk_fields) > 1

    @property
    def column_names(self) -> tuple[str, ...]:
        """All column names, in field-definition order (immutable, cached)."""
        cache = self._column_names_cache
        if cache is None:
            cache = tuple(self.fields.keys())
            self._column_names_cache = cache
        return cache

    @property
    def writable_columns(self) -> tuple[str, ...]:
        """Non-auto (INSERT/UPDATE-able) column names (immutable, cached)."""
        cache = self._writable_columns_cache
        if cache is None:
            cache = tuple(name for name, f in self.fields.items() if not f.auto)
            self._writable_columns_cache = cache
        return cache

    def invalidate_column_cache(self) -> None:
        """Drop memoized column tuples after a post-construction ``fields``
        mutation (e.g. STI discriminator injection on the parent meta)."""
        self._column_names_cache = None
        self._writable_columns_cache = None

    @property
    def db_default_columns(self) -> frozenset[str]:
        """Columns that carry a DB-side DEFAULT (``Field(db_default=...)``).

        When such a column has no explicit value on INSERT, it is omitted so
        PostgreSQL applies its DEFAULT; the generated value is read back via
        RETURNING. Auto (SERIAL) columns are excluded — they take the existing
        auto_field RETURNING path.
        """
        return frozenset(
            name for name, f in self.fields.items() if f.has_db_default and not f.auto
        )

    def pk_where_clause(self, start_param: int = 1) -> str:
        """Build a WHERE clause for the primary key(s).

        Returns e.g. "id = $1" for single PK or "a = $1 AND b = $2" for composite.
        """
        pks = self.pk_fields
        parts = [f"{pk} = ${start_param + i}" for i, pk in enumerate(pks)]
        return " AND ".join(parts)

    def get_fk_fields(self) -> dict[str, FieldMeta]:
        """Return all FK fields: {field_name: FieldMeta}."""
        return {name: f for name, f in self.fields.items() if f.foreign_key}


class Model(_BaseModel, metaclass=ModelMeta):
    """Base model class with database integration.

    Extends _BaseModel with:
    - Meta class for table configuration
    - objects QuerySet manager for queries
    - save() / delete() instance methods
    - select_related / prefetch_related support
    - ManyToManyField support
    """

    model_config = {"extra": "ignore"}
    _loaded_from_db: bool = False

    class DoesNotExist(Exception):
        """Raised when get() finds no matching row."""

        pass

    class MultipleObjectsReturned(Exception):
        """Raised when get() finds more than one matching row."""

        pass

    class Meta:
        table = None  # Override in subclass

    @property
    def pk(self):
        """Get the primary key value, resolving FieldInfo defaults.

        For composite PKs, returns a tuple of values in pk_fields order.
        For single PKs, returns the scalar value.
        """
        meta = self._meta
        if meta.is_composite_pk:
            return tuple(
                # dynamic-attr: reading this user model instance's PK columns, whose names come from meta.pk_fields at runtime
                self._resolve_value(getattr(self, f, None))
                for f in meta.pk_fields
            )
        # dynamic-attr: reading this user model instance's PK column, whose name (meta.pk_field) is only known at runtime
        val = getattr(self, meta.pk_field, None)
        return self._resolve_value(val)

    @property
    def pk_values(self) -> list[int | str | None]:
        """Get PK values as a flat list (works for both single and composite PKs)."""
        meta = self._meta
        # dynamic-attr: reading this user model instance's PK columns, whose names come from meta.pk_fields at runtime
        return [self._resolve_value(getattr(self, f, None)) for f in meta.pk_fields]

    @property
    def is_persisted(self) -> bool:
        """True if this instance has been saved to the database.

        ``_loaded_from_db`` is the authoritative signal — it is set when a row
        is hydrated from a query and after the first ``save()`` INSERT. Relying
        on it (rather than ``pk is not None``) makes detection correct for
        primary keys supplied by a DB-side default: an instance whose PK comes
        from a ``db_default`` has no pk value until the DB assigns it on INSERT,
        so it must be treated as not-yet-persisted before ``save()`` runs.
        """
        if self._loaded_from_db:
            return True
        # Fallback for instances marked persisted only by carrying PK values
        # (e.g. constructed with explicit PKs then operated on directly).
        if self._meta.is_composite_pk:
            return all(v is not None for v in self.pk)
        return self.pk is not None

    async def refresh_from_db(self, db=None):
        """Reload this instance from the database."""
        if db is None:
            db = _resolve_instance_db(type(self), for_write=False)
        meta = self._meta
        pk_vals = self.pk_values
        if any(v is None for v in pk_vals):
            raise ValueError("Cannot refresh unsaved instance")
        where = meta.pk_where_clause(start_param=1)
        sql = f"SELECT * FROM {meta.table} WHERE {where}"
        row = await db.query_one(sql, *pk_vals)
        if row:
            for col_name in meta.column_names:
                if col_name in row:
                    # dynamic-attr: writing this user model instance's column, whose name comes from meta.column_names at runtime
                    setattr(self, col_name, row[col_name])
        return self

    async def save(self, db=None, *, _using=None):
        """Save this model instance to the database.

        Fires pre_save/post_save signals and invalidates the query cache.

        ``_using`` (internal) carries the originating QuerySet's ``.using()``
        binding — a string alias or ``Database`` instance — so
        ``QuerySet.create()``/``get_or_create()``/``update_or_create()`` don't
        drop the queryset's connection when they construct-then-save. When
        neither ``db`` nor ``_using`` is given, the write connection is resolved
        via the router / ``Meta.database`` (never the bare global default).
        """
        if db is None:
            if _using is not None:
                db = get_connections()[_using] if isinstance(_using, str) else _using
            else:
                db = _resolve_instance_db(type(self), for_write=True)

        meta = self._meta

        # Auto-set STI discriminator column on insert
        if meta.sti_column and meta.sti_type:
            # dynamic-attr: setting this user model instance's STI discriminator column, whose name (meta.sti_column) is only known at runtime
            setattr(self, meta.sti_column, meta.sti_type)

        # Determine insert vs update:
        # _loaded_from_db is the authoritative flag — set when instance comes from a query.
        # New instances (from constructor or first save) always insert.
        # After first save, _loaded_from_db is set, so subsequent saves update.
        is_update = self._loaded_from_db

        created = not is_update

        await pre_save.send(sender=type(self), instance=self, created=created)

        if is_update:
            await self._update(db, meta)
        else:
            result = await self._insert(db, meta)
            if meta.auto_field and result is not None:
                # dynamic-attr: assigning the DB-generated value onto this user model instance's auto column, whose name (meta.auto_field) is only known at runtime
                setattr(self, meta.auto_field, result)
            self._loaded_from_db = True

        # Post-commit: the row is already written. A failing receiver (e.g.
        # cache invalidation) must NOT abort the save — dispatch robustly and
        # log any receiver failure loudly rather than propagate or swallow it.
        responses = await post_save.send_robust(
            sender=type(self), instance=self, created=created
        )
        log_robust_responses(responses, _logger, "post_save")

        return self

    @staticmethod
    def _resolve_value(val):
        """Resolve a field value, unwrapping FieldInfo to its default if needed.

        Also converts Enum instances to their .value for database storage.
        """
        if isinstance(val, FieldInfo):
            if val.default is not _MISSING:
                val = val.default
            elif val.default_factory is not None:
                val = val.default_factory()
            else:
                return None
        if isinstance(val, enum.Enum):
            return val.value
        return val

    @staticmethod
    def _format_vector(val):
        """Format a Python list/tuple of floats as a pgvector bracket
        literal: ``[v1,v2,...]``.

        pg.zig's generic list-to-array path produces PostgreSQL array
        literal format (``{v1,v2,...}``), which pgvector rejects.
        Model._insert/_update call this helper for every value whose
        column name is in ``meta.vector_columns``. Non-list values
        (already-formatted strings, None) pass through unchanged so
        callers who want to hand-format a vector can still do so.
        """
        if val is None:
            return None
        if isinstance(val, (list, tuple)):
            return "[" + ",".join(f"{float(v):.8g}" for v in val) + "]"
        return val

    async def _insert(self, db, meta):
        """INSERT this instance.

        Columns with a DB-side default (``Field(db_default=...)``) whose value
        is unset are omitted so PostgreSQL applies the DEFAULT — including a
        primary key defaulting to a DB expression (e.g. gen_random_uuid()).
        A single-PK supplied by such a default is read back via RETURNING and
        assigned to the instance, mirroring the SERIAL auto_field path.
        """
        # Columns with a db_default that have NO explicit value get dropped so
        # the DB applies its DEFAULT. Columns with an explicit value override
        # the default and are inserted as normal.
        db_default_cols = meta.db_default_columns
        cols: list[str] = []
        for col in meta.writable_columns:
            if (
                col in db_default_cols
                # dynamic-attr: reading this user model instance's column, whose name comes from meta.writable_columns at runtime
                and self._resolve_value(getattr(self, col)) is None
            ):
                continue
            cols.append(col)
        columns = tuple(cols)

        # dynamic-attr: reading this user model instance's columns, whose names come from meta at runtime
        values = [self._resolve_value(getattr(self, col)) for col in columns]
        if meta.vector_columns:
            # Rewrite list[float] vector values to pgvector bracket
            # format. Happens only on tables with a VectorField, so
            # the common case stays zero-cost.
            for i, col in enumerate(columns):
                if col in meta.vector_columns:
                    values[i] = self._format_vector(values[i])

        # The INSERT string + read-back column are a pure function of the column
        # subset present on this row, so compile once per distinct subset and
        # reuse. Only `values` is collected per call.
        cache = meta._insert_sql_cache
        cached = cache.get(columns)
        if cached is None:
            placeholders = ", ".join(
                f"${i + 1}::vector"
                if columns[i] in meta.vector_columns
                else f"${i + 1}"
                for i in range(len(columns))
            )
            col_names = ", ".join(columns)

            if columns:
                built_sql = (
                    f"INSERT INTO {meta.table} ({col_names}) VALUES ({placeholders})"
                )
            else:
                # Every column relies on a DB default — insert an all-defaults row.
                built_sql = f"INSERT INTO {meta.table} DEFAULT VALUES"

            # Pick the column to read back: a SERIAL auto_field, or a single PK
            # that was filled by a db_default (so we recover the server-generated
            # value).
            return_field = meta.auto_field
            if (
                return_field is None
                and not meta.is_composite_pk
                and meta.pk_field in db_default_cols
                and meta.pk_field not in columns
            ):
                return_field = meta.pk_field

            if return_field:
                built_sql += f" RETURNING {return_field}"
            cached = (built_sql, return_field)
            cache[columns] = cached
        sql, return_field = cached

        if return_field:
            result = await db.query_val(sql, *values)
            # Assign the server-generated value back onto the instance so the
            # saved object reflects the DB value (pk + is_persisted correct).
            # dynamic-attr: writing this user model instance's returned column, whose name (``return_field``) is only known at runtime
            setattr(self, return_field, result)
            return result
        await db.execute(sql, *values)
        return None

    async def _update(self, db, meta):
        """UPDATE this instance. Handles both single and composite PKs."""
        # The UPDATE string (SET clause + PK WHERE) is fully determined by the
        # writable-column set, so compile once and reuse — only `values` is
        # gathered per call. The SET-clause params are $1..$N over the non-PK
        # columns and the WHERE params start at $N+1, exactly as the original
        # per-call build (which used start_param=len(values)+1 with values then
        # holding len(columns) entries).
        wcols = meta.writable_columns
        cache = meta._update_sql_cache
        cached = cache.get(wcols)
        if cached is None:
            pk_field_set = set(meta.pk_fields)
            cols = tuple(c for c in wcols if c not in pk_field_set)
            set_clauses = ", ".join(
                f"{col} = ${i + 1}::vector"
                if col in meta.vector_columns
                else f"{col} = ${i + 1}"
                for i, col in enumerate(cols)
            )
            where = meta.pk_where_clause(start_param=len(cols) + 1)
            built_sql = f"UPDATE {meta.table} SET {set_clauses} WHERE {where}"
            cached = (cols, built_sql)
            cache[wcols] = cached
        columns, sql = cached

        # dynamic-attr: reading this user model instance's columns, whose names come from meta at runtime
        values = [self._resolve_value(getattr(self, col)) for col in columns]
        if meta.vector_columns:
            for i, col in enumerate(columns):
                if col in meta.vector_columns:
                    values[i] = self._format_vector(values[i])

        # Append PK values for WHERE clause
        pk_vals = self.pk_values
        values.extend(pk_vals)

        await db.execute(sql, *values)

    async def delete(self, db=None):
        """Delete this model instance from the database.

        Fires pre_delete/post_delete signals and invalidates the query cache.
        Handles both single and composite PKs.
        """
        if db is None:
            db = _resolve_instance_db(type(self), for_write=True)

        meta = self._meta
        pk_vals = self.pk_values

        await pre_delete.send(sender=type(self), instance=self)

        # The DELETE string depends only on the (fixed) table + PK, so compile
        # it once and reuse; only the PK values change per call.
        sql = meta._delete_sql_cache
        if sql is None:
            where = meta.pk_where_clause(start_param=1)
            sql = f"DELETE FROM {meta.table} WHERE {where}"
            meta._delete_sql_cache = sql
        await db.execute(sql, *pk_vals)

        # Post-commit: the row is already deleted. Dispatch robustly so a
        # failing receiver cannot un-delete the operation; log loudly.
        responses = await post_delete.send_robust(sender=type(self), instance=self)
        log_robust_responses(responses, _logger, "post_delete")

    @classmethod
    def from_record(cls, record):
        """Create a model instance from a database record.

        Fast path: for models with no custom validators, post_init hooks,
        or nested BaseModel fields, this bypasses ``__init__`` entirely.
        Since pg.zig returns natively-typed Python values and DB-level
        constraints were enforced on write, re-running validators on
        read is pure overhead.

        **Bulk assignment**: the record dict is copied wholesale into the
        instance's ``__dict__`` via a single C-level ``dict.update`` call —
        one op instead of 25+ Python ``setattr`` calls per hydration.
        Profile evidence: this path went from ~3.1 μs/call (setattr loop)
        to ~1.2 μs/call (wholesale copy) — ~2.6x faster per hydration.
        The callers of ``from_record`` (QuerySet._populate_results, M2M,
        prefetch_related, public_id lookups) always pass records
        containing exactly the model's columns; annotation/joined paths
        go through separate code paths that construct instances via
        ``self._model(**model_data)``.

        Slow path (custom validators or post_init): falls back to
        full ``__init__`` so developer-defined validation still runs.
        """
        if cls.__dhi_from_record_fast__:
            instance = object.__new__(cls)
            d = instance.__dict__
            # Wholesale dict copy — single C-level merge, ~100 ns for 26
            # keys. Fastest possible bulk assignment.
            d.update(record)
            # Pydantic bookkeeping fields
            d["__pydantic_fields_set__"] = set(cls.__dhi_field_names_set__)
            d["__pydantic_private__"] = None
            d["__pydantic_extra__"] = None
            d["_loaded_from_db"] = True
            # Enum coercion: overwrite enum fields with their enum instances
            # so `model.status == SomeEnum.X` comparisons work. Typically
            # 0-3 entries per model.
            for field_name, enum_cls in cls.__dhi_enum_coercer_items__:
                value = d[field_name]
                if value is not None and not isinstance(value, enum_cls):
                    d[field_name] = enum_cls(value)
            return instance

        # Slow path for models with custom validators or post_init hooks
        instance = cls(**dict(record))
        instance._loaded_from_db = True
        return instance

    def to_dict(
        self,
        *,
        exclude: set[str] | None = None,
        include: set[str] | None = None,
    ) -> dict[str, object]:
        """Convert this model instance to a plain dict.

        By default, includes all fields EXCEPT those marked with
        ``Field(exclude=True)`` (e.g., ``password_hash``). Override with
        the ``include`` or ``exclude`` parameters.

        Args:
            exclude: Field names to omit (adds to any Field(exclude=True) fields).
            include: If set, ONLY include these fields (overrides all exclusions).

        Usage:
            user_dict = user.to_dict()                           # all non-excluded fields
            user_dict = user.to_dict(exclude={"password_hash"})  # explicit exclusion
            user_dict = user.to_dict(include={"id", "username"}) # only these fields
        """
        meta = self._meta
        result: dict[str, object] = {}
        # Pre-computed at class creation — no per-call isinstance/getattr checks
        default_excluded = meta.excluded_fields
        for field_name in meta.fields:
            if include is not None:
                if field_name not in include:
                    continue
            else:
                if field_name in default_excluded:
                    continue
            if exclude is not None and field_name in exclude:
                continue
            # dynamic-attr: reading this user model instance's field, whose name comes from meta.fields at runtime
            result[field_name] = self._resolve_value(getattr(self, field_name, None))
        return result


# ── DDL Generation ────────────────────────────────────────────────────────
#
# Single source of truth for generating CREATE TABLE from Model _meta.
# Used by `hyper setup` (cli.py) and directly by tests/code via
# create_table_for_model().


def _python_default_to_sql(default_val) -> str | None:
    """Convert a Python field default to a SQL DEFAULT clause value.

    Returns None if the value has no meaningful default (sentinel or None).
    """
    if type(default_val).__name__ == "object" or default_val is None:
        return None
    if isinstance(default_val, bool):
        return "TRUE" if default_val else "FALSE"
    if isinstance(default_val, (int, float)):
        return str(default_val)
    if isinstance(default_val, str):
        if default_val == "":
            return "''"
        if default_val.endswith("()"):
            return default_val
        escaped = default_val.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(default_val, dict):
        return "'{}'"
    if isinstance(default_val, enum.Enum):
        return f"'{default_val.value}'"
    return None


def _db_default_to_sql(db_default_val) -> str | None:
    """Convert a ``Field(db_default=...)`` value to a SQL DEFAULT expression.

    Two shapes are accepted:
      - ``DatabaseDefault("<raw sql>")`` → the wrapped SQL emitted verbatim
        (e.g. ``gen_random_uuid()``, ``now()``).
      - A Python literal (``0``, ``"active"``, ``True``, ...) → safely
        SQL-quoted/escaped via ``_python_default_to_sql`` (strings quoted,
        ints/floats/bools as SQL literals).

    Returns None when the value carries no usable default.
    """
    if db_default_val is None:
        return None
    if isinstance(db_default_val, DatabaseDefault):
        return db_default_val.sql
    return _python_default_to_sql(db_default_val)


def _annotation_is_nullable(ann) -> bool:
    """Check if a type annotation allows None (e.g., int | None, Optional[str]).

    Handles the same three annotation shapes ``_annotation_pg_key`` resolves: a real
    PEP 604 ``X | None`` union, a ``typing.Union``/``Optional`` form, AND a PEP 563 STRING
    annotation (``"int | None"``, ``"Optional[str]"``) produced by
    ``from __future__ import annotations``. Without the string handling, nullability and
    type inference disagree on future-annotations models — the same two-build-path drift
    the round-1 type-fidelity fix closed for ``_field_to_sql_type``.
    """
    if ann is None:
        return True
    # dynamic-attr: introspecting a typing construct — ``__origin__`` exists only on generic aliases / Union forms, not on plain type annotations
    origin = getattr(ann, "__origin__", None)
    # Handle Union types: int | None has __origin__ = types.UnionType
    if origin is types.UnionType:
        return type(None) in ann.__args__
    # Handle typing.Optional / typing.Union
    if origin is typing.Union:
        return type(None) in ann.__args__
    # PEP 563 string annotation — detect `| None` / Optional[...] / Union[..., None].
    if isinstance(ann, str):
        text = ann.strip()
        # Optional[X] always permits None.
        for prefix in ("typing.Optional[", "Optional["):
            if text.startswith(prefix) and text.endswith("]"):
                return True
        # A `| None` alternative anywhere in a PEP 604 union string.
        if "|" in text:
            parts = [p.strip().strip("'\"") for p in text.split("|")]
            if "None" in parts or "NoneType" in parts:
                return True
        # Union[..., None] / typing.Union[..., None].
        for prefix in ("typing.Union[", "Union["):
            if text.startswith(prefix) and text.endswith("]"):
                inner = text[len(prefix) : -1]
                members = [m.strip().strip("'\"") for m in inner.split(",")]
                if "None" in members or "NoneType" in members:
                    return True
        return False
    return False


def _field_to_sql_type(model_cls, field_name: str) -> str:
    """Map a model field to its PostgreSQL column type using annotations.

    Uses PYTHON_TO_PG from migrations.py as the single source of truth.
    """
    from hyperdjango.migrations import PYTHON_TO_PG

    field_obj = model_cls.__dict__.get(field_name)
    # A class attribute for this field is a FieldInfo only when declared via
    # Field()/VectorField()/create_field(); a plain default value or an M2M
    # descriptor is not, and carries none of the db_type/vector/custom metadata.
    if isinstance(field_obj, FieldInfo):
        # 1. An explicit ``Field(db_type="BIGINT")`` override wins — for columns the
        #    annotation cannot express (e.g. a 64-bit BIGINT, which the int→INTEGER
        #    default would silently narrow to 32-bit).
        explicit = field_obj.db_type
        if explicit:
            return explicit
        # 2. VectorField sets vector_dimensions; regular FieldInfo does not.
        vdim = field_obj.vector_dimensions
        if vdim is not None:
            return f"vector({vdim})"
        # 3. A CustomField (EmailField/UUIDField/JSONField/…) declares its own db_type().
        if field_obj.custom_field is not None:
            from hyperdjango.fields import get_column_type

            custom = get_column_type(field_obj)
            if custom:
                return custom

    annotations: dict[str, type] = {}
    for cls in reversed(model_cls.__mro__):
        # __annotations__ may be set via descriptor (e.g. BaseModel),
        # not always in __dict__. Direct attribute access is correct here.
        if hasattr(cls, "__annotations__"):
            annotations.update(cls.__annotations__)
    python_type = annotations.get(field_name, str)

    # Normalize the annotation to a PYTHON_TO_PG key. This deliberately handles the
    # three shapes the old ``.__name__`` path missed — all of which fell through to
    # TEXT: a PEP 604 ``int | None`` union (no ``__origin__``), a ``typing.Union`` /
    # ``Optional`` form, and — crucially — a PEP 563 STRING annotation, since
    # ``from __future__ import annotations`` turns EVERY annotation into a string
    # (``"int | None"``, ``"datetime | None"``, ``"dict[str, int]"``). Without this a
    # model that uses future-annotations + optional columns produces a schema that
    # disagrees with its hand-written migrations.
    type_name = _annotation_pg_key(python_type)

    pg_type = PYTHON_TO_PG.get(type_name)
    if pg_type is not None:
        # Decimal precision/scale → NUMERIC(p, s) so a
        # Decimal(max_digits=10, decimal_places=2) column keeps its constraint
        # instead of collapsing to an unconstrained NUMERIC (and matches the
        # migrations.py ModelExtractor generator exactly).
        if (
            pg_type == "NUMERIC"
            and isinstance(field_obj, FieldInfo)
            and field_obj.max_digits is not None
        ):
            if field_obj.decimal_places:
                return f"NUMERIC({field_obj.max_digits}, {field_obj.decimal_places})"
            return f"NUMERIC({field_obj.max_digits})"
        return pg_type

    if isinstance(python_type, type) and issubclass(python_type, enum.Enum):
        return "TEXT"

    return "TEXT"


def _annotation_pg_key(annotation) -> str:
    """Best-effort PYTHON_TO_PG lookup key for a field annotation.

    Resolves a real PEP 604 / ``typing`` union to its first non-None member, a
    generic alias (``dict[str, int]``) to its origin name, and a PEP 563 STRING
    annotation (``"datetime | None"``, ``"Optional[int]"``, ``"dict[str, int]"``,
    ``"uuid.UUID | None"``) to the bare base type name. Returns ``str(annotation)``
    verbatim if it cannot do better (so the caller still maps it / falls back to TEXT).
    """
    # Real union — PEP 604 ``X | None`` (types.UnionType) OR typing.Union / Optional —
    # unwrap to the first non-None member. (A generic alias like dict[str,int] also has
    # __args__, so guard on union-ness, not on the mere presence of __args__.)
    # dynamic-attr: introspecting a typing construct — ``__origin__`` exists only on generic aliases / Union forms, not on plain type annotations
    origin = getattr(annotation, "__origin__", None)
    is_union = origin is typing.Union or isinstance(annotation, types.UnionType)
    if is_union:
        non_none = [
            a
            # dynamic-attr: reading a Union's ``__args__`` members — present only on typing/Union constructs
            for a in getattr(annotation, "__args__", ())
            if a is not type(None)
        ]
        if non_none:
            annotation = non_none[0]
            # dynamic-attr: introspecting the unwrapped member's ``__origin__`` — present only if it is itself a generic alias
            origin = getattr(annotation, "__origin__", None)

    if isinstance(annotation, type):
        return annotation.__name__
    if origin is not None and isinstance(origin, type):
        # Generic alias such as dict[str, int] → "dict", list[int] → "list".
        return origin.__name__

    # PEP 563 string annotation (or any other str-able form).
    text = str(annotation).strip()
    for prefix in ("typing.Optional[", "Optional[", "typing.Union[", "Union["):
        if text.startswith(prefix) and text.endswith("]"):
            text = text[len(prefix) : -1]
            break
    if "|" in text:  # PEP 604 "A | None" → first non-None alternative
        for part in text.split("|"):
            part = part.strip()
            if part and part not in ("None", "NoneType"):
                text = part
                break
    if "[" in text:  # drop generic params: "dict[str, int]" → "dict"
        text = text.split("[", 1)[0].strip()
    text = text.strip("'\"")  # drop a quoted forward ref
    if "." in text:  # module qualifier: "datetime.datetime" → "datetime"
        text = text.rsplit(".", 1)[1]
    return text


def _auto_pk_sql_type(field_obj) -> str:
    """SQL type for an ``auto=True`` primary key.

    Default is ``SERIAL`` (int32 identity). Opt into a 64-bit identity column with
    ``Field(primary_key=True, auto=True, big=True)`` → ``BIGSERIAL`` (or an explicit
    ``db_type="BIGSERIAL"`` / ``"BIGINT"``). High-volume append/cursor tables whose row
    count can exceed ~2.1B (the int32 SERIAL ceiling) should set ``big=True``. Additive:
    a plain auto PK is byte-for-byte unchanged (``SERIAL``).
    """
    if isinstance(field_obj, FieldInfo):
        explicit = field_obj.db_type
        if explicit:
            # An explicit BIGINT/BIGSERIAL override on an auto PK means BIGSERIAL.
            up = explicit.strip().upper()
            return "BIGSERIAL" if up in ("BIGINT", "BIGSERIAL") else explicit
        if field_obj.big:
            return "BIGSERIAL"
    return "SERIAL"


def _fk_column_sql_type(fk_target: str, field_obj) -> str:
    """SQL column type for a ``foreign_key`` column.

    Derives the type from the TARGET model's primary key so a FK to a TEXT/UUID/BIGINT
    PK gets a *compatible* column type instead of a hardcoded ``INTEGER`` (which silently
    breaks against a non-int PK). Resolution order:

    1. An explicit ``db_type`` / ``big=True`` on the FK field wins (caller-stated intent).
    2. Otherwise resolve the target table → model in the registry and map its PK type:
       a SERIAL/BIGSERIAL auto PK becomes the matching INTEGER/BIGINT FK column, any other
       PK type (TEXT, UUID, …) is used verbatim.
    3. Fall back to ``INTEGER`` when the target is unresolvable, so an INTEGER-PK
       target yields an INTEGER FK column.
    """
    if isinstance(field_obj, FieldInfo):
        explicit = field_obj.db_type
        if explicit:
            return explicit
        if field_obj.big:
            return "BIGINT"

    # fk_target is a table name, optionally "table.column".
    target_table = fk_target.split(".", 1)[0] if fk_target else fk_target
    target_cls = _model_registry.get(target_table)
    # _model_registry only ever holds concrete Model classes, each registered by
    # ModelMeta.__new__ *after* it assigns ``_meta`` — so a non-None entry always
    # carries a TableMeta.
    if target_cls is not None:
        tmeta = target_cls._meta
        # Single-PK fast path (composite-PK FK targets are not auto-derived; INTEGER fallback).
        pk_names = tmeta.pk_fields
        if len(pk_names) == 1:
            pk_name = pk_names[0]
            pk_meta = tmeta.fields.get(pk_name)
            pk_obj = target_cls.__dict__.get(pk_name)
            if pk_meta is not None and pk_meta.auto:
                # SERIAL/BIGSERIAL target → INTEGER/BIGINT FK column.
                pk_type = _auto_pk_sql_type(pk_obj)
                return "BIGINT" if pk_type == "BIGSERIAL" else "INTEGER"
            return _field_to_sql_type(target_cls, pk_name)
    return "INTEGER"


_APPEND_ONLY_GUARD_FN = "hyper_append_only_guard"


def _append_only_ddl(table: str) -> list[str]:
    """DDL that makes ``table`` append-only via a BEFORE UPDATE OR DELETE trigger.

    Emits (1) a single shared trigger function (``CREATE OR REPLACE``, so cross-table and
    re-run idempotent) that RAISEs on any UPDATE/DELETE, and (2) a per-table trigger,
    dropped-if-exists then created so applying the DDL twice is a no-op. INSERT (and
    SELECT) are unaffected — the table stays writable for appends only.
    """
    trigger = f"{table}_append_only"
    return [
        f"CREATE OR REPLACE FUNCTION {_APPEND_ONLY_GUARD_FN}() RETURNS trigger AS $$\n"
        f"BEGIN\n"
        f"    RAISE EXCEPTION 'append-only table %: % is not permitted', TG_TABLE_NAME, TG_OP\n"
        f"        USING ERRCODE = 'restrict_violation';\n"
        f"END;\n"
        f"$$ LANGUAGE plpgsql",
        f"DROP TRIGGER IF EXISTS {trigger} ON {table}",
        f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {_APPEND_ONLY_GUARD_FN}()",
    ]


def generate_ddl_for_model(model_cls) -> list[str]:
    """Generate DDL SQL statements for a Model class.

    Returns a list of SQL strings: the CREATE TABLE statement followed
    by any CREATE INDEX statements. This is the single source of truth
    for table schema — ``hyper setup``, ``create_table_for_model()``,
    and test scripts all use this.

    Pure function — no DB access, no side effects.
    """
    meta = model_cls._meta
    table = meta.table
    if not table:
        raise ValueError(f"Model {model_cls.__name__} has no Meta.table")

    statements: list[str] = []

    columns: list[str] = []
    annotations = {}
    for klass in reversed(model_cls.__mro__):
        # __annotations__ may be set via descriptor (e.g. BaseModel), not always
        # in __dict__ — direct attribute access is correct here (mirrors
        # _field_to_sql_type's MRO walk above).
        if hasattr(klass, "__annotations__"):
            annotations.update(klass.__annotations__)

    for field_name, field_meta in meta.fields.items():
        field_obj = model_cls.__dict__.get(field_name)
        if field_meta.foreign_key:
            # FK column type is derived from the TARGET model's PK type so a TEXT/UUID/BIGINT
            # PK gets a compatible FK column (not a hardcoded INTEGER). Falls back to INTEGER
            # (or BIGINT when the FK itself is declared big=True) when the target is
            # unresolvable.
            col_type = _fk_column_sql_type(field_meta.foreign_key, field_obj)
        elif field_meta.primary_key and field_meta.auto:
            # Auto-increment PK → SERIAL (int32) by default; big=True (or an explicit
            # db_type) opts into BIGSERIAL (int64) for high-volume cursor/append tables.
            col_type = _auto_pk_sql_type(field_obj)
        else:
            col_type = _field_to_sql_type(model_cls, field_name)

        parts = [f"    {field_name} {col_type}"]
        if field_meta.primary_key and not meta.is_composite_pk:
            parts.append("PRIMARY KEY")

        # NOT NULL: inferred from type annotation — non-optional, non-PK, non-auto
        # fields without a None default are NOT NULL.
        if not field_meta.primary_key and not field_meta.auto:
            ann = annotations.get(field_name)
            is_nullable = _annotation_is_nullable(ann)
            field_obj = model_cls.__dict__.get(field_name)
            has_none_default = (
                field_obj is not None
                and isinstance(field_obj, FieldInfo)
                and field_obj.default is None
            )
            if not is_nullable and not has_none_default:
                parts.append("NOT NULL")

        if field_meta.unique:
            parts.append("UNIQUE")

        # DEFAULT clause — emitted for ANY column (including primary keys) that
        # carries a db_default or a Python literal default. A primary key with
        # a db_default (e.g. a UUID PK defaulting to gen_random_uuid()) gets its
        # DEFAULT just like any other column; SERIAL auto PKs supply their own.
        field_obj = model_cls.__dict__.get(field_name)
        if field_obj is not None and not field_meta.auto:
            if field_obj.db_default is not None:
                default_sql = _db_default_to_sql(field_obj.db_default)
            else:
                default_sql = _python_default_to_sql(field_obj.default)
            if default_sql is not None:
                parts.append(f"DEFAULT {default_sql}")

        if field_meta.foreign_key:
            fk_target = field_meta.foreign_key
            if "." not in fk_target:
                fk_target = f"{fk_target}(id)"
            else:
                tbl, col = fk_target.rsplit(".", 1)
                fk_target = f"{tbl}({col})"
            fk_clause = f"REFERENCES {fk_target}"
            if field_meta.on_delete:
                fk_clause += f" ON DELETE {field_meta.on_delete}"
            parts.append(fk_clause)

        columns.append(" ".join(parts))

    # STI child fields (nullable columns for child-specific data)
    for field_name, entry in meta.sti_child_fields.items():
        child_cls, field_meta = entry
        field_obj = child_cls.__dict__.get(field_name)
        if field_meta.foreign_key:
            col_type = _fk_column_sql_type(field_meta.foreign_key, field_obj)
        elif field_meta.primary_key and field_meta.auto:
            col_type = _auto_pk_sql_type(field_obj)
        else:
            col_type = _field_to_sql_type(child_cls, field_name)
        parts = [f"    {field_name} {col_type}"]
        field_obj = child_cls.__dict__.get(field_name)
        if field_obj is not None and isinstance(field_obj, FieldInfo):
            if field_obj.db_default is not None:
                default_sql = _db_default_to_sql(field_obj.db_default)
            else:
                default_sql = _python_default_to_sql(field_obj.default)
            if default_sql is not None:
                parts.append(f"DEFAULT {default_sql}")
        columns.append(" ".join(parts))

    if meta.is_composite_pk:
        pk_cols = ", ".join(meta.pk_fields)
        columns.append(f"    PRIMARY KEY ({pk_cols})")

    # Composite UNIQUE constraints from Meta.unique_together
    for ut in meta.unique_together:
        ut_cols = ", ".join(ut)
        columns.append(f"    UNIQUE({ut_cols})")

    unlogged = "UNLOGGED " if meta.unlogged else ""
    create_sql = f"CREATE {unlogged}TABLE IF NOT EXISTS {table} (\n"
    create_sql += ",\n".join(columns)
    create_sql += "\n)"
    statements.append(create_sql)

    # Append-only enforcement (Meta.append_only): a BEFORE UPDATE OR DELETE trigger that
    # RAISEs, making rows immutable at the DB tier (not just by app discipline). Idempotent:
    # the shared guard function is CREATE OR REPLACE and the per-table trigger is dropped
    # before (re)create, so re-running setup/DDL is a no-op.
    if meta.append_only:
        statements.extend(_append_only_ddl(table))

    # Index statements (including pgvector HNSW/IVFFlat)
    for field_name, field_meta in meta.fields.items():
        if field_meta.index and not field_meta.primary_key:
            idx_name = f"idx_{table}_{field_name}"
            field_obj = model_cls.__dict__.get(field_name)
            vdim = (
                field_obj.vector_dimensions
                if isinstance(field_obj, FieldInfo)
                else None
            )
            if vdim is not None:
                idx_type = field_obj.vector_index_type or "hnsw"
                idx_ops = field_obj.vector_index_ops or "vector_cosine_ops"
                idx_sql = (
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} "
                    f"USING {idx_type} ({field_name} {idx_ops})"
                )
                idx_params = field_obj.vector_index_params
                if idx_params:
                    with_parts = ", ".join(f"{k} = {v}" for k, v in idx_params.items())
                    idx_sql += f" WITH ({with_parts})"
                statements.append(idx_sql)
            else:
                statements.append(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table}({field_name})"
                )

    # Meta.indexes — composite, partial, GIN, expression, etc.
    for idx in meta.indexes:
        statements.append(_generate_index_ddl(table, idx))

    return statements


async def create_table_for_model(model_cls, *, db=None, drop: bool = False) -> None:
    """Create a database table from a Model class definition.

    Generates DDL from the model's ``_meta`` via ``generate_ddl_for_model()``
    and executes it. The single source of truth for table schema.

    Args:
        model_cls: The Model class to create a table for.
        db: Database instance. Uses ``get_db()`` if not provided.
        drop: If True, drops the table first (CASCADE).
    """
    if db is None:
        db = get_db()

    if drop:
        table = model_cls._meta.table
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

    # A failed statement now raises the typed hierarchy (DatabaseError base) —
    # e.g. DuplicateTable when a table/index already exists — not a bare
    # RuntimeError; suppress both so DDL stays best-effort/idempotent.
    from hyperdjango.db.pgzig_connection import DatabaseError

    for sql in generate_ddl_for_model(model_cls):
        with contextlib.suppress(DatabaseError, RuntimeError):
            await db.execute(sql)


# ── ORM query primitives (the one ORM import namespace) ──────────────────────
# F/Q/aggregates live in hyperdjango.expressions, but a query mixes them with
# Model/Field constantly, so they are also re-exported here. ORM code needs
# ONE import:
#     from hyperdjango.models import Model, Field, F, Q, Count, Sum
# (expressions.py has no back-edge to models, so this is cycle-free.)
# ── Exception → HTTP status (the one mapping authority) ──────────────────────
# A `.get()` miss escaping to the response boundary is a 404, not a 500.
# MultipleObjectsReturned is deliberately NOT a 404 — it is a data anomaly and
# maps to the generic 500 (registering it 404 would mask a real integrity bug).
from hyperdjango.exceptions import register_exception_status as _register_exc_status
from hyperdjango.expressions import (  # noqa: E402, F401  — intentional public re-export
    Avg,
    Case,
    Cast,
    Coalesce,
    Count,
    Exists,
    Expression,
    F,
    Max,
    Min,
    NotExists,
    OuterRef,
    Q,
    StdDev,
    Subquery,
    Sum,
    Value,
    Variance,
    When,
)

_register_exc_status(Model.DoesNotExist, 404)
