"""
Production data pattern mixins for Model.

Abstract model mixins that add common production patterns:

- TimestampMixin: created_at/updated_at auto-managed fields
- SoftDeleteMixin: is_deleted flag + deleted_at, auto-filtered QuerySet
- OwnershipMixin: created_by/updated_by user tracking
- VersionedMixin: append-only versioning, every update creates a new version

Usage:
    from hyperdjango import Model, Field
    from hyperdjango.mixins import TimestampMixin, SoftDeleteMixin

    class Article(TimestampMixin, SoftDeleteMixin, Model):
        class Meta:
            table = "articles"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field()

    # TimestampMixin auto-sets created_at on INSERT, updated_at on every save
    article = Article(title="Hello")
    await article.save()
    print(article.created_at)  # set automatically
    print(article.updated_at)  # set automatically

    # SoftDeleteMixin: .delete() sets is_deleted=True instead of DELETE
    await article.delete()                # soft delete
    await article.hard_delete()           # actual DELETE
    articles = await Article.objects.all()  # excludes soft-deleted
    all_articles = await Article.objects.with_deleted().all()  # includes deleted

    # OwnershipMixin: tracks who created/modified
    article = Article(title="Hello")
    await article.save_as(user)  # sets created_by/updated_by

    # VersionedMixin: append-only audit trail
    article = VersionedArticle(title="v1")
    await article.save()  # version=1
    article.title = "v2"
    await article.save()  # version=2 (new row, old row preserved)
"""

import contextlib
import logging
from datetime import UTC, datetime

from hyperdjango.models import Field, Model, _resolve_instance_db
from hyperdjango.multi_db import get_connections
from hyperdjango.query import QuerySet
from hyperdjango.signals import (
    log_robust_responses,
    post_delete,
    post_save,
    pre_delete,
    pre_save,
)
from hyperdjango.validation.core.fields import FieldInfo
from hyperdjango.where import WhereNode

_logger = logging.getLogger("hyperdjango.mixins")

# ---------------------------------------------------------------------------
# TimestampMixin
# ---------------------------------------------------------------------------


class TimestampMixin(Model):
    """Adds created_at and updated_at fields, auto-managed on save().

    created_at: Set on first save (INSERT), never changed after.
    updated_at: Set on every save (INSERT and UPDATE).
    """

    class Meta:
        abstract = True

    created_at: datetime | None = Field(default=None)
    updated_at: datetime | None = Field(default=None)

    async def save(self, db=None, *, _using=None):
        """Save with automatic timestamp management.

        ``_using`` mirrors the base ``Model.save`` signature so a QuerySet that
        constructs-then-saves (``create``/``get_or_create``/``update_or_create``
        forward ``_using=self._using``) keeps its bound connection on timestamped
        models; dropping it here made ``get_or_create`` raise on any model that
        mixes in ``TimestampMixin``.
        """
        now = datetime.now(UTC)

        # Check if created_at is unset (None or unresolved FieldInfo default)
        if self.created_at is None or isinstance(self.created_at, FieldInfo):
            self.created_at = now
        self.updated_at = now

        return await super().save(db=db, _using=_using)


# ---------------------------------------------------------------------------
# SoftDeleteMixin
# ---------------------------------------------------------------------------


class SoftDeleteQuerySet(QuerySet):
    """QuerySet that auto-excludes soft-deleted rows."""

    def __init__(self, *args, include_deleted=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._include_deleted = include_deleted

    def _clone(self, **kwargs):
        qs = super()._clone(**kwargs)
        qs._include_deleted = self._include_deleted
        return qs

    def with_deleted(self):
        """Include soft-deleted rows in results."""
        qs = self._clone()
        qs._include_deleted = True
        return qs

    def only_deleted(self):
        """Return only soft-deleted rows."""
        return self.with_deleted().filter(is_deleted=True)

    def _build_where_tree(self, table_alias=None, join_aliases=None):
        """Add is_deleted=FALSE filter as a WhereNode child."""
        root = super()._build_where_tree(table_alias, join_aliases)
        if not self._include_deleted:
            col = f"{table_alias}.is_deleted" if table_alias else "is_deleted"
            root.children.append(WhereNode(template=f"{col} = FALSE"))
        return root

    def _mixin_cache_key(self):
        return ("sd", self._include_deleted) + super()._mixin_cache_key()

    # No _collect_mixin_params override — is_deleted=FALSE has no bind params


class SoftDeleteMixin(Model):
    """Adds soft-delete: .delete() marks as deleted instead of removing.

    Auto-filtered: QuerySet.all() excludes soft-deleted rows by default.
    Use .with_deleted() to include them, .only_deleted() for just deleted.
    Use .hard_delete() for actual database DELETE.
    """

    class Meta:
        abstract = True

    _queryset_class = SoftDeleteQuerySet

    is_deleted: bool = Field(default=False)
    deleted_at: datetime | None = Field(default=None)

    async def delete(self, db=None):
        """Soft delete: set is_deleted=True and deleted_at timestamp."""
        if db is None:
            # Route to the model's bound DB (Meta.database) / router — the same
            # connection base Model.save/_resolve_instance_db picks — never the
            # bare global default, which for a routed model would land the write
            # (or a read-your-writes lookup) on the wrong database.
            db = _resolve_instance_db(type(self), for_write=True)

        await pre_delete.send(sender=type(self), instance=self)

        now = datetime.now(UTC)
        self.is_deleted = True
        self.deleted_at = now

        meta = self._meta
        # dynamic-attr: reading this user model instance's PK column, whose name (meta.pk_field) is only known at runtime
        pk_value = getattr(self, meta.pk_field)
        await db.execute(
            f"UPDATE {meta.table} SET is_deleted = TRUE, deleted_at = $1 "
            f"WHERE {meta.pk_field} = $2",
            now,
            pk_value,
        )

        # Post-commit: the soft-delete UPDATE already ran. Dispatch robustly so
        # a failing receiver cannot abort it; log any failure loudly.
        responses = await post_delete.send_robust(sender=type(self), instance=self)
        log_robust_responses(responses, _logger, "post_delete")

    async def hard_delete(self, db=None):
        """Permanent delete: actually removes the row from the database."""
        if db is None:
            # Route to the model's bound DB (Meta.database) / router — the same
            # connection base Model.save/_resolve_instance_db picks — never the
            # bare global default, which for a routed model would land the write
            # (or a read-your-writes lookup) on the wrong database.
            db = _resolve_instance_db(type(self), for_write=True)

        meta = self._meta
        # dynamic-attr: reading this user model instance's PK column, whose name (meta.pk_field) is only known at runtime
        pk_value = getattr(self, meta.pk_field)
        await db.execute(
            f"DELETE FROM {meta.table} WHERE {meta.pk_field} = $1",
            pk_value,
        )

    async def restore(self, db=None):
        """Restore a soft-deleted row."""
        if db is None:
            # Route to the model's bound DB (Meta.database) / router — the same
            # connection base Model.save/_resolve_instance_db picks — never the
            # bare global default, which for a routed model would land the write
            # (or a read-your-writes lookup) on the wrong database.
            db = _resolve_instance_db(type(self), for_write=True)

        self.is_deleted = False
        self.deleted_at = None

        meta = self._meta
        # dynamic-attr: reading this user model instance's PK column, whose name (meta.pk_field) is only known at runtime
        pk_value = getattr(self, meta.pk_field)
        await db.execute(
            f"UPDATE {meta.table} SET is_deleted = FALSE, deleted_at = NULL "
            f"WHERE {meta.pk_field} = $1",
            pk_value,
        )


# ---------------------------------------------------------------------------
# OwnershipMixin
# ---------------------------------------------------------------------------


class OwnershipMixin(Model):
    """Tracks who created and last modified a record.

    Fields:
        created_by: User ID set on first save
        updated_by: User ID set on every save

    Use save_as(user) to set ownership automatically.
    """

    class Meta:
        abstract = True

    # editable=False: ownership is set only by save_as()/privileged code (direct
    # assignment), never mass-assigned from a request body — otherwise a user
    # could POST {"created_by": <other_id>} to forge ownership and defeat the
    # owner-based ObjectPermission checks.
    created_by: int | None = Field(default=None, editable=False)
    updated_by: int | None = Field(default=None, editable=False)

    async def save_as(self, user, db=None):
        """Save with ownership tracking.

        Args:
            user: User instance (must have .id or .pk) or user ID (int).
        """
        user_id = (
            user
            if isinstance(user, int)
            # dynamic-attr: ``user`` is an arbitrary caller-supplied object; duck-typed probe for a ``pk`` then ``id`` attribute
            else getattr(user, "pk", getattr(user, "id", None))
        )

        if self.created_by is None or isinstance(self.created_by, FieldInfo):
            self.created_by = user_id
        self.updated_by = user_id

        return await self.save(db=db)


# ---------------------------------------------------------------------------
# VersionedMixin (QuerySet defined first to avoid forward reference)
# ---------------------------------------------------------------------------


class VersionedQuerySet(QuerySet):
    """QuerySet that auto-filters to is_current=TRUE."""

    def __init__(self, *args, include_versions=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._include_versions = include_versions

    def _clone(self, **kwargs):
        qs = super()._clone(**kwargs)
        qs._include_versions = self._include_versions
        return qs

    def with_versions(self):
        """Include all versions (not just current)."""
        qs = self._clone()
        qs._include_versions = True
        return qs

    def _build_where_tree(self, table_alias=None, join_aliases=None):
        """Add is_current=TRUE filter as a WhereNode child."""
        root = super()._build_where_tree(table_alias, join_aliases)
        if not self._include_versions:
            col = f"{table_alias}.is_current" if table_alias else "is_current"
            root.children.append(WhereNode(template=f"{col} = TRUE"))
        return root

    def _mixin_cache_key(self):
        return ("ver", self._include_versions) + super()._mixin_cache_key()

    # No _collect_mixin_params override — is_current=TRUE has no bind params


class VersionedMixin(Model):
    """Append-only versioning: every save creates a new version.

    On save, if the record already exists:
    1. Existing row is preserved (becomes historical version)
    2. New row is inserted with incremented version number
    3. is_current=True only on the latest version

    Querying:
        # Get current versions only (default)
        items = await MyModel.objects.all()
        # Get all versions
        items = await MyModel.objects.with_versions().all()
        # Get history for a specific entity
        history = await item.get_history()
    """

    class Meta:
        abstract = True

    _queryset_class = VersionedQuerySet

    version: int = Field(default=1)
    is_current: bool = Field(default=True)
    entity_id: int | None = Field(default=None)

    async def save(self, db=None, *, _using=None):
        """Versioned save: creates new version row, marks old as non-current.

        ``_using`` mirrors the base ``Model.save`` signature so a QuerySet that
        constructs-then-saves (``create``/``get_or_create``/``update_or_create``
        forward ``_using=self._using``) keeps its bound connection; dropping it
        here made ``get_or_create`` raise on any versioned model. This save owns
        its INSERT chain (it never delegates to ``super().save``), so the binding
        is resolved to a ``Database`` here rather than forwarded.
        """
        if db is None:
            if _using is not None:
                db = get_connections()[_using] if isinstance(_using, str) else _using
            else:
                # No explicit binding: resolve the write connection via
                # Meta.database / the router, exactly as base Model.save does —
                # not the global default, or a routed model's version rows would
                # be written to the wrong database.
                db = _resolve_instance_db(type(self), for_write=True)

        meta = self._meta
        pk_value = self._resolve_value(
            # dynamic-attr: reading this user model instance's PK column, whose name (meta.pk_field) is only known at runtime
            getattr(self, meta.pk_field, None)
        )
        entity_id = self._resolve_value(self.entity_id)

        if pk_value is not None and entity_id is not None:
            # Existing entity — append a new version. The FOR UPDATE lock, the
            # MAX(version) read, the is_current=FALSE demotion, and the INSERT of
            # the new version must be ONE atomic unit. A SELECT ... FOR UPDATE
            # only holds its row lock for the life of the enclosing transaction,
            # so in autocommit the lock releases at statement end and two
            # concurrent saves can both read the same MAX(version) — minting a
            # duplicate version or a second is_current=TRUE row. Open a
            # transaction when the caller has none; reuse the caller's open one
            # (nullcontext, which also supports ``async with``) when one exists,
            # so the section is a single atomic unit either way.
            tx = contextlib.nullcontext() if db.in_transaction() else db.transaction()
            async with tx:
                # Lock existing rows to prevent concurrent version conflicts
                await db.execute(
                    f"SELECT 1 FROM {meta.table} WHERE entity_id = $1 FOR UPDATE",
                    entity_id,
                )
                result = await db.query_val(
                    f"SELECT MAX(version) FROM {meta.table} WHERE entity_id = $1",
                    entity_id,
                )
                new_version = (result or 0) + 1

                # Mark all existing versions as non-current
                await db.execute(
                    f"UPDATE {meta.table} SET is_current = FALSE WHERE entity_id = $1",
                    entity_id,
                )

                # Insert new version
                self.version = new_version
                self.is_current = True
                self.entity_id = entity_id
                # Clear PK so _insert creates a new row
                # dynamic-attr: clearing this user model instance's PK column, whose name (meta.pk_field) is only known at runtime
                setattr(self, meta.pk_field, None)

                await pre_save.send(sender=type(self), instance=self, created=False)
                result = await self._insert(db, meta)
                if meta.auto_field and result is not None:
                    # dynamic-attr: assigning the DB-generated value onto this user model instance's auto column, whose name (meta.auto_field) is only known at runtime
                    setattr(self, meta.auto_field, result)
            # Post-commit: the new version row is durably inserted.
            responses = await post_save.send_robust(
                sender=type(self), instance=self, created=False
            )
            log_robust_responses(responses, _logger, "post_save")
        else:
            # New entity — first version
            self.version = 1
            self.is_current = True

            await pre_save.send(sender=type(self), instance=self, created=True)
            result = await self._insert(db, meta)
            if meta.auto_field and result is not None:
                # dynamic-attr: assigning the DB-generated value onto this user model instance's auto column, whose name (meta.auto_field) is only known at runtime
                setattr(self, meta.auto_field, result)

            # Set entity_id = pk for tracking across versions
            eid = self._resolve_value(self.entity_id)
            if eid is None:
                self.entity_id = self.pk
                await db.execute(
                    f"UPDATE {meta.table} SET entity_id = $1 WHERE {meta.pk_field} = $1",
                    self.pk,
                )

            # Post-commit: the first-version row is already inserted.
            responses = await post_save.send_robust(
                sender=type(self), instance=self, created=True
            )
            log_robust_responses(responses, _logger, "post_save")

        return self

    async def get_history(self, db=None):
        """Get all versions of this entity, ordered by version."""
        if db is None:
            # Route to the model's bound DB (Meta.database) / router — the same
            # connection base Model.save/_resolve_instance_db picks — never the
            # bare global default, which for a routed model would land the write
            # (or a read-your-writes lookup) on the wrong database.
            db = _resolve_instance_db(type(self), for_write=True)

        meta = self._meta
        entity_id = self._resolve_value(self.entity_id) or self.pk
        cols = ", ".join(meta.column_names)
        rows = await db.query(
            f"SELECT {cols} FROM {meta.table} "
            f"WHERE entity_id = $1 ORDER BY version ASC",
            entity_id,
        )
        return [type(self).from_record(row) for row in rows]
