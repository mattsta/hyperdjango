"""PostgreSQL extension manager — declarative registry + apply.

Some hyperdjango features need PostgreSQL extensions that aren't in the
default postgres install (pgvector for similarity search) or need to
be CREATE EXTENSION'd before use even though the binary ships with
postgres (pg_trgm, hstore).

This module holds the canonical declaration of which extensions are
needed, where the binary comes from on each platform, and a helper to
apply them against a live database.

Used by:
    `hyper db extensions list`            — show what's declared
    `hyper db extensions ensure`          — CREATE EXTENSION IF NOT EXISTS
                                            for each declared extension
                                            against DATABASE_URL
    `hyper doctor`                        — checks each declared extension
                                            is available in the live DB
    `.github/workflows/ci.yml`            — runs `ensure` after PG starts
                                            so CI doesn't have to know
                                            extension details

When you add a feature that needs a new extension, add it here. CI,
docs, doctor, and the local `hyper db extensions ensure` command all
pick it up automatically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from hyperdjango.database import Database
from hyperdjango.db.pgzig_connection import DatabaseError


@dataclass(slots=True, frozen=True)
class PgExtension:
    """A PostgreSQL extension this framework can use."""

    name: str
    """The CREATE EXTENSION name (lowercase, matches pg_extension.extname)."""

    purpose: str
    """One-line human description — shown by `db extensions list` and doctor."""

    bundled_with_postgres: bool = False
    """True if the .so ships with stock postgres (only CREATE EXTENSION needed)."""

    docker_image: str | None = None
    """Recommended docker image that bundles the binary, when not stock."""

    apt_package: str | None = None
    """Debian/Ubuntu package name that installs the binary, when not stock."""

    required_by: tuple[str, ...] = field(default_factory=tuple)
    """hyperdjango features that need this extension. Used in error messages."""


# Canonical list. Add to this when a feature needs a new extension.
REGISTRY: tuple[PgExtension, ...] = (
    PgExtension(
        name="vector",
        purpose="pgvector — vector similarity search (HNSW, IVFFlat, cosine/L2/inner-product)",
        bundled_with_postgres=False,
        docker_image="pgvector/pgvector:pg18",
        apt_package="postgresql-18-pgvector",
        required_by=("VectorField", "services/semantic_search"),
    ),
    PgExtension(
        name="pg_trgm",
        purpose="trigram similarity matching (TrigramSimilarity expression, ILIKE acceleration)",
        bundled_with_postgres=True,
        required_by=("postgres.TrigramSimilarity", "admin search indexes"),
    ),
    PgExtension(
        name="hstore",
        purpose="key-value store column type",
        bundled_with_postgres=True,
        required_by=("postgres.HStoreField",),
    ),
)


@dataclass(slots=True, frozen=True)
class ExtensionStatus:
    """Result of checking one extension against a live DB."""

    extension: PgExtension
    binary_available: bool  # the .so is installed in the postgres server
    enabled_in_db: bool  # CREATE EXTENSION has been run on this database
    installed_version: str | None = None


async def check_extension(db: Database, ext: PgExtension) -> ExtensionStatus:
    """Inspect one extension against a connected Database."""
    avail_rows = await db.query(
        "SELECT default_version FROM pg_available_extensions WHERE name = $1",
        ext.name,
    )
    binary_available = bool(avail_rows)

    if not binary_available:
        return ExtensionStatus(
            extension=ext, binary_available=False, enabled_in_db=False
        )

    enabled_rows = await db.query(
        "SELECT extversion FROM pg_extension WHERE extname = $1", ext.name
    )
    return ExtensionStatus(
        extension=ext,
        binary_available=True,
        enabled_in_db=bool(enabled_rows),
        installed_version=(enabled_rows[0]["extversion"] if enabled_rows else None),
    )


async def ensure_extension(db: Database, ext: PgExtension) -> ExtensionStatus:
    """CREATE EXTENSION IF NOT EXISTS, return status afterwards.

    Raises RuntimeError with an actionable message if the binary isn't
    available — the user needs to install the apt package or use the
    bundled docker image; CREATE EXTENSION can't conjure it.
    """
    status = await check_extension(db, ext)
    if not status.binary_available:
        hint_parts: list[str] = [
            f"PostgreSQL extension '{ext.name}' is not installed on the server.",
            f"Required by: {', '.join(ext.required_by) or '(unspecified)'}",
        ]
        if ext.docker_image:
            hint_parts.append(f"Docker image with this extension: {ext.docker_image}")
        if ext.apt_package:
            hint_parts.append(
                f"Debian/Ubuntu package: apt-get install {ext.apt_package}"
            )
        raise RuntimeError("\n  ".join(hint_parts))
    if status.enabled_in_db:
        return status
    # Quote the identifier defensively even though our names are constants.
    await db.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext.name}"')
    return await check_extension(db, ext)


async def ensure_all(
    db: Database, *, only: tuple[str, ...] | None = None
) -> list[ExtensionStatus]:
    """Ensure every declared (or filtered) extension is enabled."""
    targets = REGISTRY if only is None else tuple(e for e in REGISTRY if e.name in only)
    return [await ensure_extension(db, ext) for ext in targets]


def cli_list() -> int:
    """Print the registry — no DB connection needed."""
    print("PostgreSQL extensions used by hyperdjango:")
    print()
    for ext in REGISTRY:
        bundled = (
            "bundled with postgres" if ext.bundled_with_postgres else "external binary"
        )
        print(f"  • {ext.name} ({bundled})")
        print(f"      {ext.purpose}")
        if ext.required_by:
            print(f"      Required by: {', '.join(ext.required_by)}")
        if ext.docker_image:
            print(f"      Docker image: {ext.docker_image}")
        if ext.apt_package:
            print(f"      Debian package: {ext.apt_package}")
        print()
    return 0


def cli_ensure(database_url: str | None, only: tuple[str, ...] | None = None) -> int:
    """Apply CREATE EXTENSION IF NOT EXISTS for each declared extension.

    Returns 0 on success, 1 if any extension's binary isn't installed.
    """
    if database_url is None:
        from hyperdjango.conf import get_setting

        database_url = get_setting("DATABASE_URL")
    if not database_url:
        print("error: no DATABASE_URL provided (--database or env)")
        return 1

    async def _run() -> int:
        db = Database(database_url, max_size=1)
        await db.connect()
        try:
            failures: list[tuple[PgExtension, str]] = []
            for ext in REGISTRY:
                if only is not None and ext.name not in only:
                    continue
                try:
                    status = await ensure_extension(db, ext)
                    state = (
                        "ENABLED"
                        if status.enabled_in_db
                        else "available but not enabled"
                    )
                    version = (
                        f" (v{status.installed_version})"
                        if status.installed_version
                        else ""
                    )
                    print(f"  ✓ {ext.name}: {state}{version}")
                except (DatabaseError, RuntimeError) as err:
                    # RuntimeError: the actionable "binary not installed" message
                    # ensure_extension raises up front. DatabaseError: a genuine
                    # CREATE EXTENSION failure now surfaces typed (permission
                    # denied, etc.) — record it as a failure rather than crashing.
                    failures.append((ext, str(err)))
                    print(f"  ✗ {ext.name}: BINARY MISSING")
                    for line in str(err).splitlines():
                        print(f"      {line}")
            return 1 if failures else 0
        finally:
            await db.disconnect()

    return asyncio.run(_run())
