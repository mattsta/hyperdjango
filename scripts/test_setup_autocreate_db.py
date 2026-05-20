#!/usr/bin/env python3
"""ensure_database_exists: hyper setup's missing-database auto-provisioning.

Covers the DDL-authority extension that lets `hyper setup` (and any harness
built on it) run against a database that does not exist yet — no secret
`createdb` step. Uses a unique throwaway database name and drops it after.

Usage:
    uv run hyper-test setup_autocreate_db
"""

# hyper-test: db_isolated

import asyncio
import uuid
from urllib.parse import urlsplit, urlunsplit

from hyperdjango.conf import fill_url_auth, resolve_database_url
from hyperdjango.database import Database, ensure_database_exists
from hyperdjango.testkit import check, finish, run_main


async def _run_checks() -> None:
    base_url = fill_url_auth(resolve_database_url())
    parts = urlsplit(base_url)
    name = f"hd_autocreate_{uuid.uuid4().hex[:12]}"
    target_url = urlunsplit(parts._replace(path=f"/{name}"))

    created = await ensure_database_exists(target_url)
    check("missing database gets created", created)

    db = Database(target_url, min_size=1, max_size=2)
    await db.connect()
    one = await db.query_val("SELECT 1")
    check("created database is connectable", one == 1)
    await db.disconnect()

    created_again = await ensure_database_exists(target_url)
    check("existing database reports already-existed", not created_again)

    no_db_url = urlunsplit(parts._replace(path=""))
    check(
        "URL without a database name is a no-op",
        not await ensure_database_exists(no_db_url),
    )

    maint = Database(
        urlunsplit(parts._replace(path="/postgres")), min_size=1, max_size=2
    )
    await maint.connect()
    await maint.execute(f'DROP DATABASE "{name}"')
    gone = await maint.query_val("SELECT 1 FROM pg_database WHERE datname = $1", name)
    check("throwaway database dropped after test", gone is None)
    await maint.disconnect()


def main() -> bool:
    asyncio.run(_run_checks())
    return finish()


if __name__ == "__main__":
    run_main(main)
