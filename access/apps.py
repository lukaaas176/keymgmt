from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_pragmas(sender, connection, **kwargs):
    """Put SQLite in WAL mode on every connection so a writer no longer locks
    out concurrent readers (the dev server is multi-threaded) — the fix for
    'database is locked'. WAL is persistent on the file; the rest are
    per-connection."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cur:
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "access"

    def ready(self):
        connection_created.connect(_sqlite_pragmas)
