"""Seed each transponder's desired ("Soll") state from its configured state.

    python manage.py seed_desired

Sets desired_locks = active ∪ planned for every transponder — a starting
point for the wish, to then adjust in the Django admin. Idempotent. Refuses
to touch transponders that already have a curated wish unless --overwrite is given.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from access.models import Transponder


class Command(BaseCommand):
    help = "Seed desired_locks = active ∪ planned for every transponder."

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite", action="store_true",
            help="also reset transponders that already have a curated wish")

    @transaction.atomic
    def handle(self, *args, **opts):
        seeded = skipped = 0
        for tp in Transponder.objects.prefetch_related(
                "locks", "planned_locks", "desired_locks"):
            if tp.desired_locks.exists() and not opts["overwrite"]:
                skipped += 1
                continue
            tp.desired_locks.set(list(tp.locks.all()) + list(tp.planned_locks.all()))
            seeded += 1
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {seeded} transponder(s); skipped {skipped} with an "
            f"existing wish (use --overwrite to reset those)."))
