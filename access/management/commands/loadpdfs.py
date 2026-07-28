"""Bulk-import printouts from a folder:  python manage.py loadpdfs ./pdfs

Accepts list-format PDFs, matrix-format PDFs (native or scanned), and —
when tesseract is installed — matrix printout images (png/jpg/tiff).
"""

import os

from django.core.management.base import BaseCommand, CommandError

from access import ocr
from access.services import import_pdf


def _importable(name: str) -> bool:
    """A PDF, or an image whose format the OCR path accepts. Matched on
    the lowercased suffix, so uppercase extensions count too."""
    return name.lower().endswith(".pdf") or ocr.is_image(name)


class Command(BaseCommand):
    help = "Import all SimonsVoss printouts (PDFs/images) found in a directory."

    def add_arguments(self, parser):
        parser.add_argument("directory", help="folder containing printout PDFs/images")

    def handle(self, *args, **opts):
        directory = opts["directory"]
        if not os.path.isdir(directory):
            raise CommandError(f"not a directory: {directory}")
        paths = sorted(
            os.path.join(directory, e)
            for e in os.listdir(directory)
            if _importable(e) and (not ocr.is_image(e) or ocr.tesseract_available())
        )
        if not paths:
            raise CommandError(f"no importable files in {directory}")
        for path in paths:
            name = os.path.basename(path)
            try:
                r = import_pdf(path, name)
            except Exception as exc:  # noqa: BLE001 - report and continue
                self.stderr.write(self.style.ERROR(f"  {name}: failed ({exc})"))
                continue
            flag = "" if r["consistent"] else self.style.WARNING(" [count mismatch]")
            if r["format"] == "matrix":
                extra = ""
                if r["corrected"]:
                    extra += "".join(
                        f"\n      serial {a} matched to known {b}"
                        for a, b in r["corrected"]
                    )
                self.stdout.write(
                    f"  {name:16} matrix     {r['persons']:3} transponders "
                    f"({r['created']} new), {r['doors']} doors, "
                    f"{r['marks']} grants{flag}{extra}"
                )
                for w in r["warnings"]:
                    self.stderr.write(self.style.WARNING(f"      {w}"))
            else:
                verb = "added" if r["created"] else "updated"
                self.stdout.write(
                    f"  {name:16} {r['serial']:10} {verb:7} {r['parsed']:3} doors{flag}"
                )
        self.stdout.write(self.style.SUCCESS(f"Imported {len(paths)} file(s)."))
