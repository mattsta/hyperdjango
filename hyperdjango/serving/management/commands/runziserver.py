"""
manage.py runziserver — Run Django with the native Zig HTTP server.

Alternative to 'runserver' that uses hyperdjango's high-performance Zig HTTP
server for dramatically faster request handling. Django's full middleware chain
(SecurityMiddleware, SessionMiddleware, CSRF, Auth, etc.) runs for every request.

Static files are served directly from Zig when STATIC_ROOT is configured.
Hot reload is enabled automatically when DEBUG=True.
"""

import mimetypes
import os
from pathlib import Path

import django
from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand

from hyperdjango._hyperdjango_native import (
    HyperServer,
    _server_add_file_route,
    _server_set_django_handler,
)
from hyperdjango.hot_reload import setup_hot_reload
from hyperdjango.serving.handler import ZigHandler


class Command(BaseCommand):
    help = "Run Django with hyperdjango's native Zig HTTP server"

    def add_arguments(self, parser):
        parser.add_argument(
            "addrport",
            nargs="?",
            default="127.0.0.1:8000",
            help="Address and port to bind to (default: 127.0.0.1:8000)",
        )
        parser.add_argument(
            "--threads",
            type=int,
            default=0,
            help="Number of server threads (0 = auto, default: CPU cores * 2)",
        )
        parser.add_argument(
            "--no-reload",
            action="store_true",
            help="Disable auto-reload even in DEBUG mode",
        )

    def handle(self, *args, **options):
        addrport = options["addrport"]
        if ":" in addrport:
            host, port_str = addrport.rsplit(":", 1)
            port = int(port_str)
        else:
            host = "127.0.0.1"
            port = int(addrport)

        # Initialize Django
        django.setup()

        # Create the Zig HTTP server
        server = HyperServer(host, port)

        # Register Django WSGI handler as the catch-all
        handler = ZigHandler(server_name=host, server_port=str(port))
        _server_set_django_handler(handler)

        # Serve static files directly from Zig (zero-copy sendfile)
        static_count = 0
        # dynamic-attr: STATIC_URL is an optional Django setting; may be unset on a minimal settings object
        static_url = getattr(settings, "STATIC_URL", "/static/") or "/static/"

        # 1. STATIC_ROOT (collectstatic output)
        # dynamic-attr: STATIC_ROOT is an optional Django setting; unset until collectstatic is configured
        static_root = getattr(settings, "STATIC_ROOT", None)
        if static_root:
            static_count += self._register_static_files(
                server, static_url, str(static_root)
            )

        # 2. In DEBUG mode, serve from each app's static/ directory
        #    (mimics Django's staticfiles finders without collectstatic)
        if settings.DEBUG and static_count == 0:
            static_count += self._register_app_static_files(server, static_url)

        # Hot reload in DEBUG mode
        if settings.DEBUG and not options.get("no_reload"):
            try:

                class _FakeApp:
                    debug = True

                    def route(self, *a, **kw):
                        def decorator(fn):
                            return fn

                        return decorator

                setup_hot_reload(_FakeApp())
                self.stdout.write("  Hot reload: enabled (native file watcher)")
            # blind-except: hot reload is a DEBUG-only dev convenience; if the file watcher can't be set up the server must still start without it.
            except Exception:
                pass

        # Print startup banner
        self.stdout.write(
            self.style.SUCCESS(
                f"\nHyperDjango Zig server running on http://{host}:{port}/"
            )
        )
        self.stdout.write("  Django middleware: full chain active")
        if static_count:
            self.stdout.write(
                f"  Static files: {static_count} files from {settings.STATIC_ROOT}"
            )
        self.stdout.write("  Press Ctrl+C to stop\n")

        try:
            server.run()
        except KeyboardInterrupt:
            self.stdout.write("\nShutting down...")

    def _register_static_files(self, server, static_url, static_root):
        """Register static files as Zig file routes for zero-copy serving."""
        count = 0
        if not Path(static_root).is_dir():
            return 0

        for dirpath, _, filenames in os.walk(static_root):
            for filename in filenames:
                filepath = str(Path(dirpath) / filename)
                relpath = os.path.relpath(filepath, static_root)
                url = static_url.rstrip("/") + "/" + relpath.replace(os.sep, "/")
                content_type = (
                    mimetypes.guess_type(filename)[0] or "application/octet-stream"
                )
                try:
                    _server_add_file_route("GET", url, filepath, content_type)
                    count += 1
                # blind-except: one unregisterable static file must not abort registration of the remaining static routes.
                except Exception:
                    pass

        return count

    def _register_app_static_files(self, server, static_url):
        """Register static files from each installed app's static/ directory.

        In DEBUG mode, serves static files directly without collectstatic.
        This includes Django Admin's CSS/JS/images for zero-copy Zig serving.
        """

        count = 0
        for app_config in apps.get_app_configs():
            static_dir = str(Path(app_config.path) / "static")
            if not Path(static_dir).is_dir():
                continue

            for dirpath, _, filenames in os.walk(static_dir):
                for filename in filenames:
                    filepath = str(Path(dirpath) / filename)
                    relpath = os.path.relpath(filepath, static_dir)
                    url = static_url.rstrip("/") + "/" + relpath.replace(os.sep, "/")
                    content_type = (
                        mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    )
                    try:
                        _server_add_file_route("GET", url, filepath, content_type)
                        count += 1
                    # blind-except: one unregisterable app static file must not abort registration of the remaining app static routes.
                    except Exception:
                        pass

        return count
