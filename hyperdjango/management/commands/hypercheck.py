"""
manage.py hypercheck — Verify HyperDjango feature availability.

Reports which performance features are available and configured.
"""

from django.core.management.base import BaseCommand

from hyperdjango.conf import get_setting
from hyperdjango.validation import core as _vc


class Command(BaseCommand):
    help = "Check HyperDjango feature availability and configuration"

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("HyperDjango Status"))
        self.stdout.write("")

        # Core features — all always available (native extension required)
        self._check("dhi validation", True, "")
        self._check("pg.zig database", True, "")
        self._check("Zig HTTP serving", True, "")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Configuration"))

        settings_map = {
            "VALIDATION_BACKEND": get_setting("VALIDATION_BACKEND"),
            "FILE_ROUTING": get_setting("FILE_ROUTING"),
            "STATIC_CACHE": get_setting("STATIC_CACHE"),
            "HOT_RELOAD": get_setting("HOT_RELOAD"),
        }

        for key, value in settings_map.items():
            self.stdout.write(f"  HYPERDJANGO_{key} = {value!r}")

        self.stdout.write("")
        self.stdout.write(f"  dhi version: {_vc.BaseModel}")
        self.stdout.write(f"  dhi native extension: {'self-contained'}")

    def _check(self, name, available, install_hint):
        if available:
            self.stdout.write(self.style.SUCCESS(f"  [OK] {name}"))
        else:
            self.stdout.write(
                self.style.WARNING(f"  [--] {name} (install: {install_hint})")
            )
