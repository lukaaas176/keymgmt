"""Export the access matrix as a tiled PDF (Typst).

    python manage.py exportpdf --size a3 -o matrix.pdf
    python manage.py exportpdf --size a4 --scope planned -o pending.pdf

Page size (a4/a3) tiles the full Transponder×doors grid across as many pages as it
takes; --scope optionally limits the marks to active or planned grants.
"""

from django.core.management.base import BaseCommand, CommandError

from access import pdf_export


class Command(BaseCommand):
    help = "Export the locking matrix to a tiled PDF via Typst."

    def add_arguments(self, parser):
        parser.add_argument(
            "-o",
            "--output",
            default="matrix.pdf",
            help="output .pdf path (default: matrix.pdf)",
        )
        parser.add_argument(
            "--size",
            choices=pdf_export.SIZES,
            default="a3",
            help="page size (default: a3)",
        )
        parser.add_argument(
            "--scope",
            choices=pdf_export.SCOPES,
            default="all",
            help="matrix marks to include (default: all)",
        )
        parser.add_argument(
            "--mode",
            choices=pdf_export.MODES,
            default="matrix",
            help="'matrix' or 'diff' (Soll/Ist comparison)",
        )
        parser.add_argument(
            "--hide-empty",
            action="store_true",
            help="diff only: drop doors with no rights anywhere",
        )

    def handle(self, *args, **opts):
        try:
            if opts["mode"] == "changes":
                data = pdf_export.build_changes_data()
            elif opts["mode"] == "diff":
                data = pdf_export.build_diff_data(hide_empty=opts["hide_empty"])
            else:
                data = pdf_export.build_matrix_data(opts["scope"])
            pdf = pdf_export.render_pdf(data, opts["size"])
        except Exception as exc:  # noqa: BLE001 - surface cleanly to the CLI
            raise CommandError(str(exc))
        with open(opts["output"], "wb") as fh:
            fh.write(pdf)
        if opts["mode"] == "changes":
            c = data["counts"]
            summary = (
                f"{c['transponders']} Transponder betroffen, "
                f"+{c['add']} / -{c['remove']} Änderungen"
            )
        else:
            tail = (
                "diff (Soll/Ist)"
                if opts["mode"] == "diff"
                else f"scope={opts['scope']}"
            )
            summary = (
                f"{len(data['transponders'])} Transponder × "
                f"{len(data['doors'])} doors, {tail}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {opts['output']} — {summary}, {opts['size'].upper()} "
                f"({len(pdf)} bytes)."
            )
        )
