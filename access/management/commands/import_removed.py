"""Populate ``Transponder.removed_locks`` from a native matrix PDF.

Hollow outline crosses in a SimonsVoss matrix mark a door the transponder is
still programmed to open but whose authorisation was withdrawn — pending
removal at the next terminal update. The normal matrix parse drops them (to
keep the active/planned counts matching the CSV target state); this command
captures *only* those hollow crosses and stores them in ``removed_locks``,
matched to the existing locks and transponders.

It is surgical: it never creates locks or transponders and never touches
active / planned / desired / groups — it clears and rewrites removed_locks
only, so re-running is idempotent.

    python manage.py import_removed ASTA-2026.pdf
    python manage.py import_removed ASTA-2026.pdf --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from access import ocr
from access.models import Lock, Transponder
from access.services import match_known_serial, match_lock_by_name


class Command(BaseCommand):
    help = ("Store hollow-cross (pending-removal) doors from a native matrix "
            "PDF into Transponder.removed_locks.")

    def add_arguments(self, parser):
        parser.add_argument("pdf", help="native matrix PDF, e.g. ASTA-2026.pdf")
        parser.add_argument("--dry-run", action="store_true",
                            help="report what would change, write nothing")

    def handle(self, *args, **opts):
        path = opts["pdf"]
        if not ocr.is_native_matrix_pdf(path):
            raise CommandError(f"{path} is not a native matrix PDF")
        data = ocr.parse_native_matrix(path, include_removed=True)

        existing_locks = list(Lock.objects.all())
        pre_known = set(Transponder.objects.values_list("serial", flat=True))

        # column -> existing transponder serial (same OCR serial correction the
        # importer uses, but never create a transponder).
        col_tp, unmatched_cols = {}, []
        used = {p.serial for p in data.persons if p.serial in pre_known}
        for p in data.persons:
            serial = p.serial
            if serial not in pre_known:
                m = match_known_serial(serial, pre_known - used)
                if m:
                    serial = m
            if serial in pre_known:
                used.add(serial)
                col_tp[p.column] = serial
            else:
                unmatched_cols.append(p.raw_serial or p.serial)

        # row -> existing lock serial
        row_lock, unmatched_rows = {}, []
        for d in data.doors:
            lk = match_lock_by_name(d.name, existing_locks)
            if lk is not None:
                row_lock[d.row] = lk.serial
            else:
                unmatched_rows.append(d.name)

        removed, skipped = {}, 0
        n_hollow = 0
        for (col, row), state in data.mark_states.items():
            if state != "remove":
                continue
            n_hollow += 1
            tp, lk = col_tp.get(col), row_lock.get(row)
            if tp is None or lk is None:
                skipped += 1
                continue
            removed.setdefault(tp, set()).add(lk)

        total = sum(len(v) for v in removed.values())
        self.stdout.write(
            f"hollow crosses: {n_hollow} → {len(removed)} transponders, "
            f"{total} removed-door grants (unmapped skipped: {skipped})")
        if unmatched_cols:
            self.stdout.write(self.style.WARNING(
                f"columns without a transponder: {len(unmatched_cols)} "
                f"{unmatched_cols[:5]}"))
        if unmatched_rows:
            self.stdout.write(self.style.WARNING(
                f"door rows without a lock: {len(unmatched_rows)} "
                f"{unmatched_rows[:5]}"))

        if opts["dry_run"]:
            self.stdout.write("dry-run — nothing written.")
            return

        with transaction.atomic():
            # Clear+rewrite removed_locks only; leave everything else alone.
            Transponder.removed_locks.through.objects.all().delete()
            for serial, lockset in removed.items():
                Transponder.objects.get(serial=serial).removed_locks.set(
                    Lock.objects.filter(serial__in=lockset))
        self.stdout.write(self.style.SUCCESS(
            f"Wrote removed_locks for {len(removed)} transponders "
            f"({total} grants)."))
