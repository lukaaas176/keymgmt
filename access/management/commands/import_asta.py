"""Rebuild the database from an LSM CSV matrix export and its native PDF twin.

    python manage.py import_asta ASTA-2026.csv ASTA-2026.pdf

The CSV supplies real lock serials, full door names and room/floor metadata;
the PDF supplies the active-vs-planned (bold-vs-thin ×) split the CSV lacks.
Both tables are wiped and repopulated, so pass --wipe to confirm you mean to
replace the live data.
"""

import os

from django.core.management.base import BaseCommand, CommandError

from access.csv_import import import_asta_csv


class Command(BaseCommand):
    help = ("Rebuild transponders/locks/grants from a CSV matrix export + its "
            "native PDF (CSV metadata, PDF active/planned split).")

    def add_arguments(self, parser):
        parser.add_argument("csv", help="LSM CSV export (UTF-16, ';'-separated)")
        parser.add_argument("pdf", help="the same matrix as a native PDF")
        parser.add_argument(
            "--wipe", action="store_true",
            help="required: this replaces ALL transponders and locks")

    def handle(self, *args, **opts):
        if not opts["wipe"]:
            raise CommandError(
                "this replaces the whole database; re-run with --wipe to "
                "confirm.")
        try:
            r = import_asta_csv(opts["csv"], opts["pdf"],
                                source_name=os.path.basename(opts["csv"]))
        except Exception as exc:  # noqa: BLE001 - surface cleanly to the CLI
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(
            f"Imported {r['transponders']} transponders, {r['locks']} locks, "
            f"{r['marks']} grants ({r['active']} active, {r['planned']} "
            f"planned)."))
