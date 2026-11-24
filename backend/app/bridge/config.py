"""The settings devices.py reads, from this app's settings. The gateway passes every device's
serial explicitly, so there is no default serial."""

from ..config import settings as app_settings


class _Settings:
    adb_serial = ""

    @property
    def adb_path(self):
        return app_settings.adb_path


settings = _Settings()
