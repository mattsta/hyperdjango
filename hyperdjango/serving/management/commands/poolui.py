"""
manage.py poolui — Web-based connection pool dashboard with live SSE updates.

Starts a lightweight HyperApp on a separate port serving a self-contained
HTML dashboard that shows real-time pool stats via Server-Sent Events.

Usage:
    python manage.py poolui              # Dashboard on http://localhost:9876
    python manage.py poolui --port 8888  # Custom port
"""

import django
from django.core.management.base import BaseCommand
from django.db import connection

from hyperdjango._hyperdjango_native import (
    HyperServer,
    _db_pool_stats,
)
from hyperdjango.native import fast_json_dumps

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<title>HyperDjango Pool Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 2rem; }
  h1 { color: #58a6ff; margin-bottom: 1rem; font-size: 1.5rem; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
  .stat { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1.2rem; }
  .stat .label { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em; }
  .stat .value { font-size: 2rem; font-weight: 700; color: #f0f6fc; margin-top: 0.3rem; }
  .stat .value.green { color: #3fb950; }
  .stat .value.yellow { color: #d29922; }
  .stat .value.red { color: #f85149; }
  .bar-container { background: #21262d; border-radius: 4px; height: 24px; margin-top: 0.5rem; overflow: hidden; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #3fb950, #58a6ff); transition: width 0.3s; border-radius: 4px; }
  .meta { color: #8b949e; font-size: 0.85rem; margin-top: 1rem; }
  #status { position: fixed; top: 1rem; right: 1rem; padding: 0.4rem 0.8rem; border-radius: 4px; font-size: 0.75rem; }
  #status.connected { background: #1a3a2a; color: #3fb950; }
  #status.disconnected { background: #3a1a1a; color: #f85149; }
</style>
</head>
<body>
<h1>HyperDjango Connection Pool</h1>
<div id="status" class="disconnected">disconnected</div>
<div class="stats">
  <div class="stat"><div class="label">Total Connections</div><div class="value" id="total">-</div></div>
  <div class="stat"><div class="label">Available</div><div class="value green" id="available">-</div></div>
  <div class="stat"><div class="label">In Use</div><div class="value yellow" id="in_use">-</div></div>
  <div class="stat"><div class="label">Thread-Owned</div><div class="value" id="thread_owned">-</div></div>
  <div class="stat"><div class="label">Missing</div><div class="value" id="missing">-</div></div>
  <div class="stat"><div class="label">Database</div><div class="value" id="database" style="font-size:1rem">-</div></div>
</div>
<div class="stat" style="max-width:600px">
  <div class="label">Utilization</div>
  <div class="bar-container"><div class="bar-fill" id="bar" style="width:0%"></div></div>
  <div class="meta" id="utilization">-</div>
</div>
<div class="meta" style="margin-top:2rem">
  Pool handle: <span id="handle">-</span> |
  Pools registered: <span id="pools">-</span> |
  Active handle: <span id="active">-</span> |
  Updated: <span id="updated">-</span>
</div>
<script>
const es = new EventSource('/__pool_stats');
es.onopen = () => { document.getElementById('status').className = 'connected'; document.getElementById('status').textContent = 'live'; };
es.onerror = () => { document.getElementById('status').className = 'disconnected'; document.getElementById('status').textContent = 'disconnected'; };
es.onmessage = (e) => {
  const d = JSON.parse(e.data);
  document.getElementById('total').textContent = d.total ?? '-';
  document.getElementById('available').textContent = d.available ?? '-';
  document.getElementById('in_use').textContent = d.in_use ?? '-';
  document.getElementById('thread_owned').textContent = d.thread_owned ?? '-';
  document.getElementById('missing').textContent = d.missing ?? '-';
  document.getElementById('database').textContent = d.database ?? '-';
  document.getElementById('handle').textContent = d.pool_handle ?? '-';
  document.getElementById('pools').textContent = d.pools_registered ?? '-';
  document.getElementById('active').textContent = d.active_handle ?? '-';
  document.getElementById('updated').textContent = new Date().toLocaleTimeString();
  const total = d.total || 1;
  const pct = ((d.in_use || 0) / total * 100).toFixed(0);
  document.getElementById('bar').style.width = pct + '%';
  document.getElementById('utilization').textContent = pct + '% (' + (d.in_use||0) + '/' + total + ')';
  const el = document.getElementById('missing');
  el.className = 'value' + ((d.missing > 0) ? ' red' : '');
};
</script>
</body>
</html>"""


class Command(BaseCommand):
    help = "Web-based connection pool dashboard with live SSE updates"

    def add_arguments(self, parser):
        parser.add_argument(
            "--port", type=int, default=9876, help="Dashboard port (default: 9876)"
        )
        parser.add_argument(
            "--pool", type=int, default=-1, help="Pool handle to monitor"
        )

    def handle(self, *args, **options):
        django.setup()

        port = options["port"]
        pool_handle = options["pool"]

        if pool_handle < 0:
            try:
                connection.ensure_connection()
                if hasattr(connection, "connection") and hasattr(
                    connection.connection, "_pool_handle"
                ):
                    pool_handle = connection.connection._pool_handle or 0
                else:
                    pool_handle = 0
            # blind-except: auto-detecting the active pool handle is best-effort; if the DB can't connect, fall back to default pool 0 for the dashboard.
            except Exception:
                pool_handle = 0

        server = HyperServer("0.0.0.0", port)

        # Dashboard HTML route
        server.add_route(
            "GET",
            "/",
            lambda **kw: {
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "content": DASHBOARD_HTML,
            },
        )

        # SSE stats endpoint
        def sse_handler(**kwargs):
            stats = _db_pool_stats(pool_handle)
            stats["pool_handle"] = pool_handle
            data = fast_json_dumps(stats).decode()
            # Return SSE-formatted response
            return {
                "status_code": 200,
                "content_type": "text/event-stream",
                "content": f"data: {data}\n\n",
            }

        server.add_route("GET", "/__pool_stats", sse_handler)

        self.stdout.write(
            self.style.SUCCESS(f"\nPool Dashboard: http://localhost:{port}/")
        )
        self.stdout.write(f"  Monitoring pool #{pool_handle}")
        self.stdout.write("  Press Ctrl+C to stop\n")

        try:
            server.run()
        except KeyboardInterrupt:
            self.stdout.write("\nDashboard stopped.")
