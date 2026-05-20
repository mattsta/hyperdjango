"""
manage.py dbpool -- Show connection pool status.

Displays real-time statistics for all pg.zig connection pools including
connection counts, thread-owned slots, and database names.

Usage:
    python manage.py dbpool           # One-shot status
    python manage.py dbpool --json    # Machine-readable JSON
    python manage.py dbpool --watch   # Live updating (1s interval)
"""

import json as _stdlib_json
import time

import django
from django.core.management.base import BaseCommand
from django.db import connection

from hyperdjango._hyperdjango_native import _db_pool_stats
from hyperdjango.native import fast_json_dumps


class Command(BaseCommand):
    help = "Show pg.zig connection pool status"

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output in JSON format",
        )
        parser.add_argument(
            "--watch",
            action="store_true",
            help="Live updating (poll every 1s)",
        )
        parser.add_argument(
            "--pool",
            type=int,
            default=-1,
            help="Specific pool handle to inspect (default: active pool)",
        )

    def handle(self, *args, **options):
        # Ensure Django DB is connected
        django.setup()

        pool_handle = options["pool"]

        # If no specific pool, use the active one (try to connect first)
        if pool_handle < 0:
            try:
                connection.ensure_connection()
                # Get pool handle from the connection wrapper
                if hasattr(connection, "connection") and hasattr(
                    connection.connection, "_pool_handle"
                ):
                    pool_handle = connection.connection._pool_handle or 0
                else:
                    pool_handle = 0
            # blind-except: auto-detecting the active pool handle is best-effort; if the DB can't connect, fall back to default pool 0 for inspection.
            except Exception:
                pool_handle = 0

        if options["watch"]:
            self._watch(pool_handle, _db_pool_stats, options["json"])
        else:
            stats = _db_pool_stats(pool_handle)
            self._display(stats, pool_handle, options["json"])

    def _display(self, stats, pool_handle, as_json):
        if as_json:
            stats["pool_handle"] = pool_handle
            self.stdout.write(_stdlib_json.dumps(stats, indent=2))
            return

        self.stdout.write(self.style.SUCCESS(f"\n  Pool #{pool_handle} Status"))
        self.stdout.write(f"  {'---' * 14}")

        if "database" in stats:
            self.stdout.write(f"  Database:      {stats['database']}")

        total = stats.get("total", 0)
        available = stats.get("available", 0)
        in_use = stats.get("in_use", 0)
        missing = stats.get("missing", 0)
        thread_owned = stats.get("thread_owned", 0)

        self.stdout.write(f"  Total conns:   {total}")
        self.stdout.write(f"  Available:     {available}")
        self.stdout.write(f"  In use:        {in_use}")
        self.stdout.write(f"  Thread-owned:  {thread_owned}")
        if missing > 0:
            self.stdout.write(
                self.style.WARNING(f"  Missing:       {missing} (reconnecting)")
            )

        # Utilization bar
        if total > 0:
            pct = (in_use / total) * 100
            bar_len = 20
            filled = int(bar_len * in_use / total)
            bar = "#" * filled + "." * (bar_len - filled)
            self.stdout.write(f"  Utilization:   [{bar}] {pct:.0f}%")

        pools_reg = stats.get("pools_registered", 0)
        active = stats.get("active_handle", -1)
        self.stdout.write(f"\n  Pools registered: {pools_reg}, active: #{active}")
        self.stdout.write("")

    def _watch(self, pool_handle, stats_fn, as_json):
        self.stdout.write("Watching pool stats (Ctrl+C to stop)...\n")
        try:
            while True:
                stats = stats_fn(pool_handle)
                if as_json:
                    stats["pool_handle"] = pool_handle
                    stats["timestamp"] = time.time()
                    self.stdout.write(fast_json_dumps(stats).decode())
                else:
                    # Clear screen and redisplay
                    self.stdout.write("\033[2J\033[H", ending="")
                    self._display(stats, pool_handle, False)
                    self.stdout.write(f"  (watching, {time.strftime('%H:%M:%S')})")
                time.sleep(1)
        except KeyboardInterrupt:
            self.stdout.write("\nStopped.")
