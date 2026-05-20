from django.apps import AppConfig

from hyperdjango.conf import get_setting
from hyperdjango.logging import logger


class HyperDjangoConfig(AppConfig):
    name = "hyperdjango"
    verbose_name = "HyperDjango"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        features = ["self-contained validation", "pg.zig database", "Zig HTTP serving"]

        if get_setting("FILE_ROUTING"):
            features.append("file-based routing")

        logger.info("HyperDjango ready: {features}", features=", ".join(features))
