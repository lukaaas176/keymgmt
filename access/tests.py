"""Tests for the Schließmatrix (matrix) parser and its import pipeline.

Two fixture sources:

* ``scan.pdf`` in the project root — a real scanned ASTA matrix whose
  scanner-OCR text layer stores the rotated column headers character-
  reversed. Golden values below were read by eye from the page images.
  These tests skip when the file is absent.
* Synthetic PDFs built in-test with raw content streams — a native-style
  matrix (reading-order rotated text, door rows, X marks, footers) and a
  minimal list-format page. These cover the door/mark path that the scan
  (an empty matrix, "Zeile 1-0") cannot.
"""

import io
import os
import tempfile
import unittest
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from . import ocr, services
from .matrix_parser import (RE_FOOTER, MatrixDoor, MatrixPerson, MatrixResult,
                            detect_format, lookalike_equal, parse_matrix_pdf,
                            repair_serial)
from .models import Group, Lock, Transponder

SCAN_PDF = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "scan.pdf"))
HAVE_SCAN = os.path.exists(SCAN_PDF)
SCREENSHOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "screenshot.png"))
HAVE_SHOT = os.path.exists(SCREENSHOT) and ocr.tesseract_available()
TOCHECK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "to_check"))
HAVE_TOCHECK = os.path.isdir(TOCHECK) and os.path.exists(
    os.path.join(TOCHECK, "Lukas.pdf"))
import shutil
HAVE_TYPST = shutil.which("typst") is not None
ASTA_PDF = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ASTA-2026.pdf"))
ASTA_CSV = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ASTA-2026.csv"))
HAVE_ASTA = os.path.exists(ASTA_PDF)
HAVE_ASTA_CSV = HAVE_ASTA and os.path.exists(ASTA_CSV)


# --- Raw-PDF synthesis helpers ------------------------------------------------

def _esc(text: str) -> str:
    """Escape a string for a PDF literal, latin-1 bytes as octal."""
    out = []
    for ch in text:
        if ch in "()\\":
            out.append("\\" + ch)
        elif ord(ch) > 126:
            out.append("\\%03o" % ord(ch.encode("latin-1")))
        else:
            out.append(ch)
    return "".join(out)


def _upright(x: float, y: float, text: str, size: int = 8) -> str:
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({_esc(text)}) Tj ET"


def _rotated(x: float, y: float, text: str, size: int = 8) -> str:
    """90°-rotated text that reads bottom-to-top starting at (x, y)."""
    return f"BT /F1 {size} Tf 0 1 -1 0 {x} {y} Tm ({_esc(text)}) Tj ET"


def _pdf_bytes(content_streams: list[str]) -> bytes:
    """Assemble a minimal multi-page A4 PDF around raw content streams."""
    objs: list[bytes] = []
    n_pages = len(content_streams)
    page_ids = [4 + 2 * i for i in range(n_pages)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>"
                .encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                b" /Encoding /WinAnsiEncoding >>")
    for i, cs in enumerate(content_streams):
        data = cs.encode("latin-1")
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
                     f" /Resources << /Font << /F1 3 0 R >> >>"
                     f" /Contents {page_ids[i] + 1} 0 R >>").encode())
        objs.append(b"<< /Length %d >>\nstream\n%s\nendstream"
                    % (len(data), data))

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
            % (len(objs) + 1, xref_at))
    return bytes(out)


# Column geometry mirroring the real printouts: person strips ~10.5pt apart.
COL_X0, COL_PITCH = 320.0, 10.5


def _matrix_page(persons, doors=(), footer="", col_offset=0) -> str:
    """One native-style matrix page.

    persons: [(name, serial)] left to right.
    doors:   [(name, room, floor, mark_cols)] with mark_cols as 0-based
             page-local column indexes that get an X.
    """
    parts = []
    for i, (name, serial) in enumerate(persons):
        x = COL_X0 + (col_offset + i) * COL_PITCH
        parts.append(_rotated(x, 560, serial))       # the SN band
        parts.append(_rotated(x, 620, name))         # name above it
    if doors:
        parts.append(_upright(40, 452, "NAME (TÜREN/SCHLIESSUNGEN)"))
        parts.append(_upright(160, 452, "PB"))
        parts.append(_upright(185, 452, "RN"))
        parts.append(_upright(230, 452, "ZB"))
        parts.append(_upright(250, 452, "N"))
        parts.append(_upright(265, 452, "E"))
        y = 435.0
        for name, room, floor, mark_cols in doors:
            parts.append(_upright(40, y, name))
            if room:
                parts.append(_upright(185, y, room))
            if floor:
                parts.append(_upright(265, y, floor))
            for col in mark_cols:
                x = COL_X0 + (col_offset + col) * COL_PITCH
                parts.append(_upright(x - 6, y, "X"))
            y -= 15
    if footer:
        parts.append(_upright(30, 25, footer))
    return "\n".join(parts)


def _write_pdf(content_streams) -> str:
    fd, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as fh:
        fh.write(_pdf_bytes(content_streams))
    return path


PERSONS_P1 = [("ASTA 1 Muster, Anna", "03UAG03"),
              ("ASTA 2", "02UH4PG"),
              ("Winner, Henry", "03TN2G5")]
PERSONS_P2 = [("V Tresor, Flo, Nicht vorhanden 03U4345", "03U4345")]
DOORS_P1 = [("5532 Eingang Studitum", "0.002", "EG", [0, 2]),
            ("5532 AStA-Besprechungsraum", "1.105", "1.OG", [1]),
            ("5532 Bandraum U 20", "U20", "UG", [0, 1, 2])]
DOORS_P2 = [("5532 Eingang Studitum", "0.002", "EG", [0]),
            ("5532 AStA-Besprechungsraum", "1.105", "1.OG", []),
            ("5532 Bandraum U 20", "U20", "UG", [0])]


def _synthetic_matrix() -> str:
    """Two-page matrix: 3 + 1 columns over the same 3 door rows."""
    return _write_pdf([
        _matrix_page(PERSONS_P1, DOORS_P1, "Zeile 1-3 ; Spalte 1-3"),
        _matrix_page(PERSONS_P2, DOORS_P2, "Zeile 1-3 ; Spalte 4-4"),
    ])


def _synthetic_list_page(rows=()) -> str:
    """A minimal but complete 'Berechtigungen für den Transponder' printout."""
    parts = [
        _upright(40, 800, "Berechtigungen für den Transponder"),
        _upright(40, 782, "ASTA 51 Justus, Rossmeier / 02UA77F"),
        _upright(40, 740, "Tür"), _upright(200, 740, "Raumnummer"),
        _upright(300, 740, "Seriennummer"), _upright(420, 740, "Bereich"),
    ]
    y = 722
    for door, room, serial, area in rows:
        parts.append(_upright(40, y, door))
        parts.append(_upright(200, y, room))
        parts.append(_upright(300, y, serial))
        if area:
            parts.append(_upright(420, y, area))
        y -= 18
    if rows:
        parts.append(_upright(40, y - 6, f"Anzahl der Datensätze: {len(rows)}"))
    return _write_pdf(["\n".join(parts)])


# --- Pure unit tests -----------------------------------------------------------

class RepairSerialTests(TestCase):
    """Every case below is a real read from scan.pdf's OCR layer."""

    CLEAN = ["03UAG03", "02UH4PG", "03UAHSN", "T-00061", "TC-0178",
             "TC-00296", "011R2DX", "010BF7C"]
    TRIVIAL = {  # O/l/I lookalikes and noise-stripping only -> not suspect
        "O2URHK6": "02URHK6", "O3TR9UX": "03TR9UX",
        "03UAPOB": "03UAP0B", "OlUSSC2": "01USSC2",
        "O3UCEAK": "03UCEAK", "O1X0ALB": "01X0ALB",
        "0'l06NRT": "0106NRT", "O,IUSCHF": "01USCHF",
    }
    REPAIRED = {  # needed confusion maps -> suspect
        "03tJB9Ft": "03UB9FL",       # tJ->U, t->L
        "03tJcB91": "03UCB91",
        "03ucc4E": "03UCC4E",
        "03uAR'11": "03UAR11",
        "O1UUTMs": "01UUTM5",        # s->5
        "o2x6754": "02X6754",
        "o2ttM6GX": "02UM6GX",       # tt->U
        "02I.JKXOE": "02UKX0E",      # IJ->U
        "01USNgR": "01USN9R",        # g->9
        "0't0A1P9": "010A1P9",       # leading-digit enforcement
        "OlOA¿Á¿": "010A2A2",        # symbol map
        "O3TgOGN": "03T90GN",
    }

    def test_clean_serials_pass_untouched(self):
        for raw in self.CLEAN:
            self.assertEqual(repair_serial(raw), (raw, True, False), raw)

    def test_trivial_fixes_are_not_suspect(self):
        for raw, want in self.TRIVIAL.items():
            self.assertEqual(repair_serial(raw), (want, True, False), raw)

    def test_confusion_repairs_are_suspect(self):
        for raw, want in self.REPAIRED.items():
            serial, valid, suspect = repair_serial(raw)
            self.assertEqual(serial, want, raw)
            self.assertTrue(valid, raw)
            self.assertTrue(suspect, raw)

    def test_garbage_stays_invalid(self):
        for raw in ("SN", "NAME", "ASTA 27", "", "o", "ZB"):
            self.assertFalse(repair_serial(raw)[1], raw)

    def test_letter_o_is_never_accepted(self):
        # The serial alphabet has no O; an unrepaired O must not validate.
        serial, valid, _ = repair_serial("03UAPOB")
        self.assertEqual(serial, "03UAP0B")
        self.assertTrue(valid)

    def test_legal_1j_substring_survives_full_repair(self):
        # '1J' is a legal serial substring; the digraph pass must not
        # collapse it while repairing an unrelated character.
        self.assertEqual(repair_serial("011J4Ag"), ("011J4A9", True, True))
        self.assertEqual(repair_serial("011J4A9"), ("011J4A9", True, False))


class LookalikeTests(TestCase):
    def test_ocr_confusion_pairs_match(self):
        for a, b in [("02UKSKC", "02UK9KC"), ("02UKDSD", "02UKD8D"),
                     ("0104112", "010A112"), ("010A05C", "010A0SC"),
                     ("02UHTLR", "02UH7LR"), ("02UACSU", "02UAC5U")]:
            self.assertTrue(lookalike_equal(a, b), (a, b))

    def test_distinct_serials_do_not_match(self):
        # 010A0CS (ASTA 31) and 010A0SC (ASTA 67) are both real transponders that
        # differ by a transposition — they must never be merged.
        self.assertFalse(lookalike_equal("010A0CS", "010A0SC"))
        self.assertFalse(lookalike_equal("03UAG03", "03UAG04"))
        self.assertFalse(lookalike_equal("03UAG03", "3UAG03"))


class FooterRegexTests(TestCase):
    def test_variants(self):
        cases = {
            "Zeile 1-0 ; Spalte 1-26": (1, 0, 1, 26),
            "Zeile '1-0 ; Spalte 81-81": (1, 0, 81, 81),   # OCR quote noise
            "Zeile 12-40; Spalte 27-80": (12, 40, 27, 80),
        }
        for text, want in cases.items():
            m = RE_FOOTER.search(text)
            self.assertIsNotNone(m, text)
            self.assertEqual(tuple(int(g) for g in m.groups()), want, text)


# --- Synthetic native-format matrix -------------------------------------------

class SyntheticMatrixParseTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.path = _synthetic_matrix()
        cls.result = parse_matrix_pdf(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.path)
        super().tearDownClass()

    def test_detected_as_matrix(self):
        self.assertEqual(detect_format(self.path), "matrix")

    def test_native_orientation_is_not_flagged_as_scan(self):
        self.assertFalse(self.result.ocr_scan)

    def test_persons_across_pages(self):
        r = self.result
        self.assertEqual(len(r.persons), 4)
        self.assertEqual([p.column for p in r.persons], [1, 2, 3, 4])
        self.assertEqual(r.persons[0].serial, "03UAG03")
        self.assertEqual(r.persons[0].asta_number, 1)
        self.assertEqual(r.persons[0].person_name, "Muster, Anna")
        self.assertEqual(r.persons[2].person_name, "Winner, Henry")
        self.assertEqual(r.persons[3].serial, "03U4345")
        self.assertTrue(all(p.serial_valid for p in r.persons))
        self.assertEqual(r.expected_columns, 4)
        self.assertTrue(r.consistent)

    def test_door_rows_are_shared_across_column_pages(self):
        # Both pages show the same 3 rows (Zeile 1-3); no duplicates.
        self.assertEqual(len(self.result.doors), 3)
        self.assertEqual([d.row for d in self.result.doors], [1, 2, 3])
        d = {d.name: d for d in self.result.doors}["5532 AStA-Besprechungsraum"]
        self.assertEqual(d.room_number, "1.105")
        self.assertEqual(d.floor, "1.OG")

    def test_marks_map_to_columns_and_rows(self):
        # Page 1: col1 rows 1+3, col2 rows 2+3, col3 rows 1+3.
        # Page 2 shows the same rows for col 4: rows 1 and 3.
        want = {(1, 1), (3, 1), (2, 2), (1, 3), (2, 3), (3, 3),
                (4, 1), (4, 3)}
        self.assertEqual(self.result.marks, want)


class RowSplitAndFooterEdgeTests(TestCase):
    def test_row_split_pages_do_not_duplicate_persons(self):
        # A tall matrix: same columns on both pages, rows 1 and 2 split.
        path = _write_pdf([
            _matrix_page(PERSONS_P1, [DOORS_P1[0]], "Zeile 1-1 ; Spalte 1-3"),
            _matrix_page(PERSONS_P1, [DOORS_P1[1]], "Zeile 2-2 ; Spalte 1-3"),
        ])
        try:
            r = parse_matrix_pdf(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(r.persons), 3)
        self.assertEqual([p.column for p in r.persons], [1, 2, 3])
        self.assertEqual([d.row for d in r.doors], [1, 2])
        self.assertEqual(r.marks, {(1, 1), (3, 1), (2, 2)})
        self.assertTrue(r.consistent)
        self.assertEqual(r.warnings, [])

    def test_footerless_pages_continue_columns_and_rows(self):
        path = _write_pdf([
            _matrix_page(PERSONS_P1[:2],
                         [("Tuer Eins", "", "", [0]),
                          ("Tuer Zwei", "", "", [1])]),
            _matrix_page([PERSONS_P1[2]], [("Tuer Drei", "", "", [0])]),
        ])
        try:
            r = parse_matrix_pdf(path)
        finally:
            os.unlink(path)
        self.assertEqual([p.column for p in r.persons], [1, 2, 3])
        self.assertEqual([(d.row, d.name) for d in r.doors],
                         [(1, "Tuer Eins"), (2, "Tuer Zwei"),
                          (3, "Tuer Drei")])
        self.assertEqual(r.marks, {(1, 1), (2, 2), (3, 3)})
        self.assertTrue(r.consistent)   # nothing to validate against

    def test_jittered_footer_fragments_are_not_doors(self):
        # Scanner OCR can split the footer over two visual lines (seen on
        # the real scan: the ';' sits ~5pt below the rest). No fragment
        # may become a door row.
        page = "\n".join([
            _matrix_page(PERSONS_P1[:2], [("Tuer Eins", "", "", [0])]),
            _upright(30, 25, "Zeile 1-1"),
            _upright(75, 20.2, ";"),
            _upright(85, 25, "Spalte 1-2"),
        ])
        path = _write_pdf([page])
        try:
            r = parse_matrix_pdf(path)
        finally:
            os.unlink(path)
        self.assertEqual([d.name for d in r.doors], ["Tuer Eins"])
        self.assertTrue(r.consistent)


class DetectFormatTests(TestCase):
    def test_list_format_detected(self):
        path = _synthetic_list_page()
        try:
            self.assertEqual(detect_format(path), "list")
        finally:
            os.unlink(path)

    def test_scan_detected_as_matrix(self):
        if not HAVE_SCAN:
            self.skipTest("scan.pdf not present")
        self.assertEqual(detect_format(SCAN_PDF), "matrix")


# --- The real scanned matrix ---------------------------------------------------

class ScanPdfGoldenTests(TestCase):
    """Golden should-state for scan.pdf, derived by eye from the images."""

    GOLDEN_SERIALS = {
        1: "T-00061", 2: "TC-0178", 3: "03UAG03", 4: "03UB9FL",
        5: "03UAP0B", 6: "03UCB91", 7: "03UB67H", 8: "03UCC4E",
        11: "03UAHSN", 12: "03U35MC", 15: "01UUTM5", 18: "03U02LU",
        23: "02UH4PG", 26: "02UM6GX", 27: "02UP4BA", 28: "03THFLX",
        29: "010A0CS", 42: "TC-00296", 48: "02UA77F", 53: "01UTRPX",
        57: "02UKX0E", 60: "03U50U4", 61: "011ELMN", 62: "0106NRT",
        64: "010A2A2", 71: "010BF7C", 74: "011R2DX", 77: "03T90GN",
        78: "03THP0F", 79: "03U4345", 80: "03TN2G5",
    }
    GOLDEN_ASTA = {3: 1, 7: 5, 11: 9, 12: 11, 26: 27, 28: 30, 29: 31,
                   48: 51, 60: 63, 74: 76}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_SCAN:
            cls.result = parse_matrix_pdf(SCAN_PDF)

    def setUp(self):
        if not HAVE_SCAN:
            self.skipTest("scan.pdf not present")

    def test_shape(self):
        r = self.result
        self.assertEqual(len(r.persons), 80)
        self.assertEqual(r.expected_columns, 81)  # page 3 col is not OCR'd
        self.assertEqual(r.expected_rows, 0)      # "Zeile 1-0": empty matrix
        self.assertEqual(len(r.doors), 0)
        self.assertEqual(len(r.marks), 0)
        self.assertTrue(r.ocr_scan)
        self.assertFalse(r.consistent)

    def test_missing_column_is_reported(self):
        self.assertTrue(any("page 3" in w and "1 column" in w
                            for w in self.result.warnings))
        self.assertTrue(any("81" in w and "80" in w
                            for w in self.result.warnings))

    def test_all_serials_repair_to_valid(self):
        for p in self.result.persons:
            self.assertTrue(p.serial_valid, (p.column, p.raw_serial))

    def test_golden_serials(self):
        by_col = {p.column: p for p in self.result.persons}
        for col, want in self.GOLDEN_SERIALS.items():
            self.assertEqual(by_col[col].serial, want, f"column {col}")

    def test_golden_asta_numbers(self):
        by_col = {p.column: p for p in self.result.persons}
        for col, want in self.GOLDEN_ASTA.items():
            self.assertEqual(by_col[col].asta_number, want, f"column {col}")

    def test_names_survive(self):
        by_col = {p.column: p for p in self.result.persons}
        self.assertEqual(by_col[28].person_name, "Barth. Yves")
        self.assertIn("Hennessen", by_col[35].person_name)
        self.assertEqual(by_col[75].person_name, "Deaktiviert")
        self.assertIn("Nicht vorhanden", by_col[79].person_name)


# --- Import pipeline -----------------------------------------------------------

class MatrixImportTests(TestCase):
    """Import semantics: fill gaps, correct scan serials, never clobber."""

    def setUp(self):
        if not HAVE_SCAN:
            self.skipTest("scan.pdf not present")
        # State a couple of list printouts would have established.
        self.named = Transponder.objects.create(
            serial="03UAHSN", person_name="StudiTUM, Student")
        self.lock = Lock.objects.create(serial="00G4LTS", door_name="Tür X")
        self.named.locks.add(self.lock)
        for s in ("02UK9KC", "010A112", "010A0SC", "02UKD8D"):
            Transponder.objects.create(serial=s)

    def test_import_scan(self):
        r = services.import_pdf(SCAN_PDF, "scan.pdf")
        self.assertEqual(r["format"], "matrix")
        self.assertEqual(r["persons"], 80)
        self.assertEqual(r["skipped"], [])

        corrected = dict(r["corrected"])
        self.assertEqual(corrected.get("02UKSKC"), "02UK9KC")
        self.assertEqual(corrected.get("02UKDSD"), "02UKD8D")
        self.assertEqual(corrected.get("0104112"), "010A112")
        self.assertEqual(corrected.get("010A05C"), "010A0SC")

        # 80 columns: 4 resolve to corrections, 1 (03UAHSN) exists directly.
        self.assertEqual(r["created"], 75)
        self.assertEqual(Transponder.objects.count(), 5 + 75)

        # Wrong serials never entered the table; the corrected ones gained
        # their ASTA numbers.
        self.assertFalse(Transponder.objects.filter(serial="02UKSKC").exists())
        self.assertEqual(
            Transponder.objects.get(serial="02UK9KC").asta_number, 25)
        self.assertEqual(
            Transponder.objects.get(serial="010A0SC").asta_number, 67)

        # Fill, don't overwrite: the list-import name stays, ASTA arrives.
        tp = Transponder.objects.get(serial="03UAHSN")
        self.assertEqual(tp.person_name, "StudiTUM, Student")
        self.assertEqual(tp.asta_number, 9)
        # An empty matrix must not touch authorizations.
        self.assertEqual(list(tp.locks.all()), [self.lock])

        # Both real near-twin transponders exist separately.
        self.assertTrue(Transponder.objects.filter(serial="010A0CS").exists())
        self.assertTrue(Transponder.objects.filter(serial="010A0SC").exists())

    def test_reimport_is_idempotent(self):
        services.import_pdf(SCAN_PDF, "scan.pdf")
        count = Transponder.objects.count()
        r2 = services.import_pdf(SCAN_PDF, "scan.pdf")
        self.assertEqual(r2["created"], 0)
        self.assertEqual(r2["updated"], 0)
        self.assertEqual(Transponder.objects.count(), count)


class ListFormatEndToEndTests(TestCase):
    """The pre-existing list-format path, now routed through the format
    dispatcher, must keep working end to end."""

    ROWS = [("5532 Eingang Studitum", "0.002", "07XYZ01", "West"),
            ("5532 Bandraum U 20", "U20", "07XYZ02", "")]

    def test_import_list_pdf(self):
        path = _synthetic_list_page(self.ROWS)
        try:
            r = services.import_pdf(path, "transponder.pdf")
        finally:
            os.unlink(path)
        self.assertEqual(r["format"], "list")
        self.assertEqual(r["serial"], "02UA77F")
        self.assertEqual(r["label"], "Justus, Rossmeier")
        self.assertEqual((r["parsed"], r["stated"]), (2, 2))
        self.assertTrue(r["consistent"])
        self.assertTrue(r["created"])
        tp = Transponder.objects.get(serial="02UA77F")
        self.assertEqual(tp.asta_number, 51)
        self.assertEqual(
            set(tp.locks.values_list("serial", flat=True)),
            {"07XYZ01", "07XYZ02"})
        self.assertEqual(Lock.objects.get(serial="07XYZ01").area, "West")

    def test_upload_endpoint_accepts_list_pdf(self):
        path = _synthetic_list_page(self.ROWS)
        try:
            with open(path, "rb") as fh:
                resp = self.client.post("/upload/", {"pdfs": fh})
        finally:
            os.unlink(path)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Transponder.objects.filter(serial="02UA77F").exists())


class InvalidSerialSkipTests(TestCase):
    def test_unreadable_serial_is_skipped_and_reported(self):
        path = _write_pdf([_matrix_page(
            [("K1", "03UAG03"), ("Someone", "######"), ("K3", "02UH4PG")],
            footer="Zeile 1-0 ; Spalte 1-3")])
        try:
            r = services.import_pdf(path, "m.pdf")
        finally:
            os.unlink(path)
        self.assertEqual(r["skipped"], ["######"])
        self.assertFalse(r["consistent"])
        self.assertTrue(any("unreadable serial" in w for w in r["warnings"]))
        self.assertEqual(
            set(Transponder.objects.values_list("serial", flat=True)),
            {"03UAG03", "02UH4PG"})


class CorrectionOrderingTests(TestCase):
    """A lookalike correction must not steal a serial that another column
    of the same file matches exactly (exact reads claim targets first)."""

    def test_exact_match_beats_earlier_lookalike(self):
        Transponder.objects.create(serial="02UH4PG", person_name="Owner")
        # Column 1 carries lowercase noise so the file counts as OCR'd;
        # column 2 lookalike-matches 02UH4PG (G↔6); column 3 IS 02UH4PG.
        path = _write_pdf([_matrix_page(
            [("K1", "03ucc4E"), ("K2", "02UH4P6"), ("K3", "02UH4PG")],
            footer="Zeile 1-0 ; Spalte 1-3")])
        try:
            r = services.import_pdf(path, "m.pdf")
        finally:
            os.unlink(path)
        self.assertTrue(r["ocr_scan"])
        self.assertEqual(r["corrected"], [])
        self.assertEqual(
            set(Transponder.objects.values_list("serial", flat=True)),
            {"02UH4PG", "02UH4P6", "03UCC4E"})
        self.assertEqual(
            Transponder.objects.get(serial="02UH4PG").person_name, "Owner")


class SyntheticMatrixImportTests(TestCase):
    def setUp(self):
        self.path = _synthetic_matrix()

    def tearDown(self):
        os.unlink(self.path)

    def test_doors_and_marks_become_locks_and_grants(self):
        existing = Lock.objects.create(
            serial="07XYZ01", door_name="5532 Eingang Studitum")
        r = services.import_pdf(self.path, "matrix.pdf")
        self.assertEqual(r["format"], "matrix")
        self.assertEqual(r["created"], 4)
        # 3 distinct doors; one matched the existing lock by name.
        self.assertEqual(r["doors_created"], 2)
        self.assertEqual(Lock.objects.count(), 3)

        anna = Transponder.objects.get(serial="03UAG03")
        self.assertEqual(anna.asta_number, 1)
        self.assertEqual(anna.person_name, "Muster, Anna")
        self.assertEqual(
            set(anna.locks.values_list("door_name", flat=True)),
            {"5532 Eingang Studitum", "5532 Bandraum U 20"})
        self.assertIn(existing, anna.locks.all())

        flo = Transponder.objects.get(serial="03U4345")
        self.assertEqual(
            set(flo.locks.values_list("door_name", flat=True)),
            {"5532 Eingang Studitum", "5532 Bandraum U 20"})

        besprechung = Lock.objects.get(door_name="5532 AStA-Besprechungsraum")
        self.assertTrue(besprechung.serial.startswith("MX:"))
        self.assertEqual(besprechung.room_number, "1.105")
        self.assertEqual(
            set(besprechung.transponders.values_list("serial", flat=True)),
            {"02UH4PG"})

    def test_reimport_is_idempotent(self):
        services.import_pdf(self.path, "matrix.pdf")
        locks, grants = (Lock.objects.count(),
                         Transponder.locks.through.objects.count())
        services.import_pdf(self.path, "matrix.pdf")
        self.assertEqual(Lock.objects.count(), locks)
        self.assertEqual(Transponder.locks.through.objects.count(), grants)


class CellClassifierTests(TestCase):
    """Pixel classification of matrix cells, per the LSM manual §7.5
    Doors/Persons symbol vocabulary. Cells are drawn as the parser sees
    them: a binarized image with ink=255 on a 0 background."""

    SIZE = 68

    def _cell(self, draw_fn):
        # Cells are greyscale as the parser sees them: dark ink (0) on a
        # white (255) background.
        from PIL import Image, ImageDraw
        img = Image.new("L", (self.SIZE, self.SIZE), 255)
        draw_fn(ImageDraw.Draw(img))
        return img

    def _classify(self, draw_fn):
        return ocr._classify_cell(self._cell(draw_fn), (0, 0, self.SIZE, self.SIZE))

    GREY = 110   # hatch prints as mid-grey: darker than the ink cutoff
                 # (128) so it registers as structure, lighter than the
                 # solid-stroke cutoff (64) so it is never a cross.

    def _hatch(self, dr):
        for off in range(-self.SIZE, self.SIZE, 5):
            dr.line([off, 0, self.SIZE + off, self.SIZE],
                    fill=self.GREY, width=2)

    def test_bold_cross_is_x(self):
        def d(dr):
            dr.line([12, 12, 56, 56], fill=0, width=8)
            dr.line([56, 12, 12, 56], fill=0, width=8)
        self.assertEqual(self._classify(d), "x")

    def test_thin_cross_is_x(self):
        # "Configured but not programmed" renders as a lighter cross than
        # the bold programmed one, but still a full-size solid stroke.
        def d(dr):
            dr.line([14, 14, 54, 54], fill=0, width=4)
            dr.line([54, 14, 14, 54], fill=0, width=4)
        self.assertEqual(self._classify(d), "x")

    def test_cross_over_hatch_is_x(self):
        def d(dr):
            self._hatch(dr)
            dr.line([12, 12, 56, 56], fill=0, width=8)
            dr.line([56, 12, 12, 56], fill=0, width=8)
        self.assertEqual(self._classify(d), "x")

    def test_uniform_hatch_is_not_x(self):
        self.assertEqual(self._classify(self._hatch), "hatch")

    def test_dense_single_direction_hatch_is_not_x(self):
        # The real failure the audit caught: dense "/" hatch with a darker
        # band fakes centre + diagonal, but has no solid stroke.
        def d(dr):
            for off in range(-self.SIZE, self.SIZE, 3):
                dr.line([off, 0, self.SIZE + off, self.SIZE],
                        fill=self.GREY, width=2)
            dr.line([20, 8, 60, 48], fill=90, width=4)   # darker band, still grey
        self.assertEqual(self._classify(d), "hatch")

    def test_thin_cross_over_hatch_still_solid(self):
        # An X over hatch is authorised only if the cross itself is solid
        # ink, not merely a denser grey band.
        def d(dr):
            self._hatch(dr)
            dr.line([14, 14, 54, 54], fill=0, width=5)
            dr.line([54, 14, 14, 54], fill=0, width=5)
        self.assertEqual(self._classify(d), "x")

    def test_empty_is_empty(self):
        self.assertEqual(self._classify(lambda dr: None), "empty")

    def test_faint_grey_cross_is_faint(self):
        # A cross drawn in light grey (a transitional/removed state) has no
        # solid stroke and no dense fill: reported, not counted.
        def d(dr):
            dr.line([12, 12, 56, 56], fill=185, width=6)
            dr.line([56, 12, 12, 56], fill=185, width=6)
        self.assertEqual(self._classify(d), "faint")

    def test_corner_triangle_without_cross_is_not_x(self):
        # Manual §7.5: a withdrawn group authorisation shows the corner
        # triangle but no cross — it must not count as an authorization.
        def d(dr):
            dr.polygon([(9, 9), (34, 9), (9, 34)], fill=0)
        self.assertNotEqual(self._classify(d), "x")

    def test_triangle_plus_cross_is_x(self):
        # A programmed group authorisation: triangle AND cross -> authorized.
        def d(dr):
            dr.polygon([(9, 9), (30, 9), (9, 30)], fill=0)
            dr.line([12, 12, 56, 56], fill=0, width=7)
            dr.line([56, 12, 12, 56], fill=0, width=7)
        self.assertEqual(self._classify(d), "x")


class DoorNameMatchingTests(TestCase):
    """OCR'd door names must find their list-imported locks."""

    def setUp(self):
        self.locks = [
            Lock.objects.create(serial="L1", door_name="5532 AStA-Besprechungsraum"),
            Lock.objects.create(serial="L2", door_name="5532 Küchenschrank 1.OG"),
            Lock.objects.create(serial="L3", door_name="5532 Herd 1.OG"),
            Lock.objects.create(serial="L4", door_name="5532 Herd 2.OG"),
            Lock.objects.create(serial="L5", door_name="5532 Seminarraum EG links"),
            Lock.objects.create(serial="L6", door_name="5532 Seminarraum EG rechts"),
        ]

    def match(self, name):
        lk = services.match_lock_by_name(name, self.locks)
        return lk.serial if lk else None

    def test_exact_and_normalized(self):
        self.assertEqual(self.match("5532 AStA-Besprechungsraum"), "L1")
        self.assertEqual(self.match("5532 asta-besprechungsraum"), "L1")
        # 0/O confusion and lost umlauts are normalized away.
        self.assertEqual(self.match("5532 Kuchenschrank 1.0G"), "L2")

    def test_fuzzy_unique_minimum(self):
        self.assertEqual(self.match("5532 AStA-Besprechunasraum"), "L1")
        # 'Herd 1.0G' is distance 0 to Herd 1.OG after normalization and
        # must not be confused with Herd 2.OG.
        self.assertEqual(self.match("5532 Herd 1.0G"), "L3")

    def test_no_cross_match(self):
        self.assertEqual(self.match("5532 Seminarraum EG rechts"), "L6")
        self.assertIsNone(self.match("5532 Bandraum U 20"))

    def test_digit_difference_blocks_fuzzy_merge(self):
        # A genuinely new door one digit away from an existing one must NOT
        # merge onto it, even when it is the only near-twin present.
        only_1og = [Lock.objects.get(serial="L3")]  # 5532 Herd 1.OG
        # Same door, OCR-garbled letters -> matches.
        self.assertEqual(
            services.match_lock_by_name("5532 Herd 1.OG", only_1og), only_1og[0])
        # Different floor -> must not match (would corrupt Herd 1.OG).
        self.assertIsNone(
            services.match_lock_by_name("5532 Herd 4.OG", only_1og))
        self.assertIsNone(
            services.match_lock_by_name("5532 Kuchenschrank 5.OG",
                                        [Lock.objects.get(serial="L2")]))

    def test_orientation_difference_blocks_fuzzy_merge(self):
        # Ost vs West is one-two edits apart but a different door; it must
        # never merge, even as the only near-twin present.
        ost = [Lock.objects.create(serial="LW", door_name="Raum 0321 Ost")]
        self.assertEqual(
            services.match_lock_by_name("Raum 0321 Ost", ost), ost[0])
        self.assertIsNone(
            services.match_lock_by_name("Raum 0321 West", ost))
        # Abbreviated O/W, too.
        o = [Lock.objects.create(serial="LO", door_name="Raum 201 O")]
        self.assertIsNone(services.match_lock_by_name("Raum 201 W", o))

    def test_zero_containing_numbers_do_not_merge(self):
        # The digit guard keeps its zeros, so numbers differing only by zeros
        # are distinct doors (regression: 0->o collapse used to erase them).
        r100 = [Lock.objects.create(serial="L100", door_name="Raum 100")]
        self.assertEqual(services.match_lock_by_name("Raum 100", r100), r100[0])
        self.assertIsNone(services.match_lock_by_name("Raum 1", r100))
        self.assertIsNone(services.match_lock_by_name("Raum 10", r100))
        # But the O/0 OCR variant of the SAME door still matches (normalized).
        self.assertEqual(self.match("5532 Herd 1.0G"), "L3")


class LockLabelTests(TestCase):
    def test_room_appended_only_as_whole_token(self):
        # room '12' is a substring of 'Labor 123' but not a token -> keep it.
        self.assertEqual(
            Lock(serial="S1", door_name="Labor 123", room_number="12").label,
            "Labor 123 (12)")
        # exact room token already in the name -> not duplicated.
        self.assertEqual(
            Lock(serial="S2", door_name="Labor 12", room_number="12").label,
            "Labor 12")
        self.assertEqual(
            Lock(serial="S3", door_name="Labor", room_number="12").label,
            "Labor (12)")

    def test_blank_door_name_has_no_dangling_room(self):
        # No leading-space, door-less ' (12)' label; fall back to the serial.
        self.assertEqual(
            Lock(serial="S4", door_name="", room_number="12").label, "S4")


class MatrixPlannedDedupTests(TestCase):
    """A door that is already active must not also be listed as planned."""

    def _matrix(self, serial, door_name, state):
        res = MatrixResult()
        res.persons.append(MatrixPerson(
            column=1, serial=serial, raw_serial=serial, serial_valid=True,
            serial_suspect=False, asta_number=None, person_name=""))
        res.doors.append(MatrixDoor(row=1, name=door_name))
        res.marks.add((1, 1))
        res.mark_states[(1, 1)] = state
        return res

    def test_planned_skipped_when_lock_already_active(self):
        lk = Lock.objects.create(serial="MX:D1", door_name="Raum 1")
        tp = Transponder.objects.create(serial="AAA0001")
        tp.locks.add(lk)                       # active from a prior import
        services._import_matrix(self._matrix("AAA0001", "Raum 1", "planned"),
                                "later.pdf")
        tp.refresh_from_db()
        self.assertIn(lk, tp.locks.all())
        self.assertNotIn(lk, tp.planned_locks.all())
        self.assertFalse(tp.has_planned)

    def test_activation_clears_prior_planned(self):
        lk = Lock.objects.create(serial="MX:D2", door_name="Raum 2")
        tp = Transponder.objects.create(serial="AAA0002")
        tp.planned_locks.add(lk)               # pending from a prior import
        services._import_matrix(self._matrix("AAA0002", "Raum 2", "active"),
                                "later.pdf")
        tp.refresh_from_db()
        self.assertIn(lk, tp.locks.all())
        self.assertNotIn(lk, tp.planned_locks.all())


@unittest.skipUnless(HAVE_SHOT, "screenshot.png or tesseract not available")
class ScreenshotOcrTests(TestCase):
    """Golden should-state for the image path, verified against an
    independent visual transcription of screenshot.png."""

    GOLDEN_SERIALS = {   # column -> serial (eye-verified)
        1: "03UCB91", 2: "02UH7LR", 3: "010A112", 4: "02UH4PG",
        7: "TC-00042", 10: "02UP4BA", 11: "03U50U4", 13: "T-00095",
        14: "010A0SC", 17: "02UA77F", 22: "02UL6P5", 26: "03TN2G5",
        29: "03UAHSN", 31: "02UM6GX", 36: "03TRUH3", 38: "01XEUPT",
        42: "01X0ALB", 47: "010BF7C", 51: "02X9787", 53: "01X15G4",
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.result = ocr.parse_matrix_image(SCREENSHOT)

    def test_shape(self):
        r = self.result
        self.assertEqual(len(r.persons), 53)
        self.assertEqual(len(r.doors), 31)
        self.assertTrue(r.ocr_scan)
        # Solid programmed crosses only; the pixel classifier is
        # deterministic, so this is exact (hatch and faint marks excluded).
        self.assertEqual(len(r.marks), 800)

    def test_faint_marks_are_reported_not_counted(self):
        self.assertTrue(any("faint/partial mark" in w
                            for w in self.result.warnings))

    def test_golden_serials(self):
        by_col = {p.column: p for p in self.result.persons}
        for col, want in self.GOLDEN_SERIALS.items():
            self.assertEqual(by_col[col].serial, want, f"column {col}")

    def test_door_rows(self):
        names = [d.name for d in self.result.doors]
        self.assertIn("5532 Bandraum U 20", names)
        by_name = {d.name: d for d in self.result.doors}
        d = by_name.get("5532 AStA-Besprechunasraum") \
            or by_name.get("5532 AStA-Besprechungsraum")
        self.assertIsNotNone(d)
        self.assertEqual(d.room_number, "1.105")
        self.assertEqual(d.floor, "1.OG")

    def test_deactivated_columns_are_not_authorized(self):
        # 'Fa. Forster ... bis 07.2020' (column 6) is expired: fully
        # hatched, and hatching must not count as authorization.
        marks_col6 = [m for m in self.result.marks if m[0] == 6]
        self.assertEqual(marks_col6, [])
        self.assertTrue(any("greyed out" in w for w in self.result.warnings))

    def test_dense_columns_have_dense_marks(self):
        # Göppl (col 9) and the Leitung group open nearly everything.
        per_col = {}
        for c, _row in self.result.marks:
            per_col[c] = per_col.get(c, 0) + 1
        self.assertGreaterEqual(per_col.get(9, 0), 25)
        self.assertGreaterEqual(per_col.get(22, 0), 25)


@unittest.skipUnless(HAVE_SHOT, "screenshot.png or tesseract not available")
class ScreenshotImportTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.parsed = ocr.parse_matrix_image(SCREENSHOT)

    def test_import_applies_marks_and_matches_doors(self):
        Lock.objects.create(serial="07XYZ09",
                            door_name="5532 AStA-Besprechungsraum")
        for s in ("02UEE9D", "03TR9UX", "00XGTD5", "010BF7C"):
            Transponder.objects.create(serial=s)
        r = services._import_matrix(self.parsed, "screenshot.png")
        self.assertEqual(r["format"], "matrix")
        self.assertEqual(r["persons"], 53)
        # OCR lookalike misreads resolve onto the known transponders.
        corrected = dict(r["corrected"])
        self.assertEqual(corrected.get("02UEESD"), "02UEE9D")
        self.assertEqual(corrected.get("03TR0UX"), "03TR9UX")
        self.assertFalse(Transponder.objects.filter(serial="02UEESD").exists())
        # The seeded lock was matched by name (no MX: duplicate).
        self.assertEqual(
            Lock.objects.filter(door_name__icontains="Besprechun").count(), 1)
        self.assertGreater(
            Lock.objects.get(serial="07XYZ09").transponders.count(), 5)

    def test_reimport_is_idempotent(self):
        services._import_matrix(self.parsed, "screenshot.png")
        counts = (Transponder.objects.count(), Lock.objects.count(),
                  Transponder.locks.through.objects.count())
        services._import_matrix(self.parsed, "screenshot.png")
        self.assertEqual(
            (Transponder.objects.count(), Lock.objects.count(),
             Transponder.locks.through.objects.count()), counts)


class UploadAndCommandTests(TestCase):
    def test_upload_endpoint_accepts_matrix(self):
        if not HAVE_SCAN:
            self.skipTest("scan.pdf not present")
        with open(SCAN_PDF, "rb") as fh:
            resp = self.client.post("/upload/", {"pdfs": fh})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Transponder.objects.count(), 80)

    def test_loadpdfs_handles_matrix(self):
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "grid.pdf")   # name must not say 'matrix'
        with open(path, "wb") as fh:
            fh.write(_pdf_bytes([
                _matrix_page(PERSONS_P1, DOORS_P1, "Zeile 1-3 ; Spalte 1-3"),
                _matrix_page(PERSONS_P2, DOORS_P2, "Zeile 1-3 ; Spalte 4-4"),
            ]))
        out = io.StringIO()
        try:
            call_command("loadpdfs", tmpdir, stdout=out)
        finally:
            os.unlink(path)
            os.rmdir(tmpdir)
        self.assertEqual(Transponder.objects.count(), 4)
        # The matrix branch of the command's output, not the file name.
        self.assertIn("4 transponders", out.getvalue())


# --- Real to_check/ fixtures: list extracts + native-PDF matrix -------------

@unittest.skipUnless(HAVE_TOCHECK, "to_check/ fixtures not present")
class ListExtractGoldenTests(TestCase):
    """The six real per-transponder rights extracts (a different, live
    locking system: TUM G 2 / GAB 43). Each states its own record count,
    which the parser must reproduce exactly."""

    # file -> (serial, label, stated==parsed door count)
    GOLDEN = {
        "010a0cs.pdf": ("010A0CS", "AStA Allgemein 21", 72),
        "010a0sc.pdf": ("010A0SC", "ASTA 67", 20),
        "010a1p9.pdf": ("010A1P9", "ASTA 70", 13),
        "02ua77f.pdf": ("02UA77F", "Justus, Rossmeier", 63),
        "02ukd8d.pdf": ("02UKD8D", "ASTA 26", 80),
        "03tphtg.pdf": ("03TPHTG", "Kastenmüller, Lukas", 33),
    }

    def test_each_extract_imports_consistently(self):
        for fname, (serial, label, count) in self.GOLDEN.items():
            with self.subTest(file=fname):
                r = services.import_pdf(os.path.join(TOCHECK, fname), fname)
                self.assertEqual(r["format"], "list")
                self.assertEqual(r["serial"], serial)
                self.assertEqual(r["label"], label)
                self.assertEqual(r["parsed"], count)
                self.assertEqual(r["stated"], count)
                self.assertTrue(r["consistent"])
                tp = Transponder.objects.get(serial=serial)
                self.assertEqual(tp.locks.count(), count)

    def test_known_doors_present(self):
        services.import_pdf(os.path.join(TOCHECK, "010a0cs.pdf"), "x.pdf")
        tp = Transponder.objects.get(serial="010A0CS")
        names = set(tp.locks.values_list("door_name", flat=True))
        for d in ("Raum 308 Dusche", "Mensa ASTA Eingang",
                  "Bihinderten Eing. 010 Ost", "Raum - 103 Lager"):
            self.assertIn(d, names)


@unittest.skipUnless(HAVE_TOCHECK, "to_check/ fixtures not present")
class NativeMatrixTests(TestCase):
    """Lukas.pdf is a native-PDF Schließmatrix whose X marks are vector
    graphics — the text parser reads columns/doors but zero marks, so it
    must be rendered. Ground-truth numbers are from the by-hand audit."""

    LUKAS = os.path.join(TOCHECK, "Lukas.pdf")

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_TOCHECK:
            cls.r = ocr.parse_native_matrix(cls.LUKAS)

    def _col(self, name):
        return next(p.column for p in self.r.persons if p.person_name == name)

    def _doors_of(self, col):
        return {row for c, row in self.r.marks if c == col}

    def test_routing_detects_native(self):
        self.assertTrue(ocr.is_native_matrix_pdf(self.LUKAS))
        self.assertEqual(detect_format(self.LUKAS), "matrix")

    def test_text_parser_reads_no_marks(self):
        # This is *why* the native path exists: the plain parser sees the
        # geometry but none of the graphical marks.
        self.assertEqual(len(parse_matrix_pdf(self.LUKAS).marks), 0)

    def test_shape(self):
        self.assertEqual(len(self.r.persons), 75)
        self.assertEqual(len(self.r.doors), 172)
        # Solid crosses only: hollow outline crosses (empty centre) are not
        # authorizations and are excluded by the centre-ink gate.
        self.assertGreater(len(self.r.marks), 2800)
        self.assertLess(len(self.r.marks), 3100)
        self.assertTrue(self.r.ocr_scan)

    def test_group_templates_match_audit(self):
        # The Muster (template) columns define each group's door set.
        self.assertEqual(len(self._doors_of(self._col("A Muster Allgem"))), 53)
        self.assertEqual(len(self._doors_of(self._col("A Muster Umwelt"))), 4)

    def test_allgemein_inheritance(self):
        # The 10 base doors every AStA key should open (from the mail).
        allg10 = {"Mensa ASTA Eingang", "Mensa ASTA Eingang Links",
                  "Mensa ASTA Notausgang", "Mensa Gitterbox 2 Mülllager",
                  "Bihinderten Eing. 010 Ost", "Haupteingang 008 West",
                  "Raum 104 Stud. Arbeit", "Raum 308 Dusche",
                  "Eingang -111 ( Keller )", "Raum - 103 Lager"}
        door_name = {d.row: d.name for d in self.r.doors}

        def coverage(col):
            opened = {door_name.get(row) for row in self._doors_of(col)}
            return len(allg10 & opened)

        # A plain Allgemein member opens all 10; a Technik combo key does not
        # (the finding that the specialised keys lack the Allgemein base).
        plain = next(p.column for p in self.r.persons
                     if p.person_name == "AStA Allgemein 01")
        technik = next(p.column for p in self.r.persons
                       if p.person_name == "AStA Technik 04")
        self.assertEqual(coverage(plain), 10)
        self.assertLess(coverage(technik), 10)

    def test_truncated_serials_flagged(self):
        # ~28 serials are cut off with "…" in the PDF itself.
        suspect = [p for p in self.r.persons if not p.serial_valid]
        self.assertGreater(len(suspect), 20)
        self.assertTrue(any("truncated" in w for w in self.r.warnings))


@unittest.skipUnless(HAVE_TOCHECK, "to_check/ fixtures not present")
class NativeMatrixImportTests(TestCase):
    def test_import_routes_through_native_and_creates_grants(self):
        r = services.import_pdf(os.path.join(TOCHECK, "Lukas.pdf"), "Lukas.pdf")
        self.assertEqual(r["format"], "matrix")
        self.assertEqual(r["persons"], 75)
        self.assertGreater(r["marks"], 2800)
        # Marks became real authorizations (0 before the native reader),
        # split into active (bold ×) and planned (thin ×).
        active = Transponder.locks.through.objects.count()
        planned = Transponder.planned_locks.through.objects.count()
        self.assertGreater(active + planned, 1000)
        self.assertGreater(active, 0)
        self.assertGreater(planned, active)   # the plan is mostly pending
        # Marks now split three ways: active + planned + hollow (pending removal).
        self.assertEqual(
            r["active_marks"] + r["planned_marks"] + r["removed_marks"],
            r["marks"])
        self.assertGreater(r["removed_marks"], 0)   # Lukas.pdf has hollow crosses
        # Hollow marks land in removed_locks (fewer rows than marks: the 28
        # truncated-serial columns are skipped, same as for active/planned).
        removed = Transponder.removed_locks.through.objects.count()
        self.assertGreater(removed, 0)
        self.assertGreater(r["planned_marks"], r["active_marks"])
        # Truncated-serial columns are skipped rather than mis-keyed.
        self.assertEqual(len(r["skipped"]), 28)

    def test_group_template_is_all_planned(self):
        # The Muster template's authorizations are all thin × (the group is
        # being set up), so every one lands in planned_locks, none active.
        services.import_pdf(os.path.join(TOCHECK, "Lukas.pdf"), "Lukas.pdf")
        muster = Transponder.objects.filter(
            person_name="A Muster Allgem").first()
        self.assertIsNotNone(muster)
        self.assertEqual(muster.locks.count(), 0)          # nothing active
        # 53 planned marks -> 52 distinct locks (one door name repeats).
        self.assertGreaterEqual(muster.planned_locks.count(), 52)
        self.assertTrue(muster.has_planned)


@unittest.skipUnless(HAVE_TOCHECK, "to_check/ fixtures not present")
class MarkStateTests(TestCase):
    """The native reader tells bold (active) from thin (planned) marks."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_TOCHECK:
            cls.r = ocr.parse_native_matrix(os.path.join(TOCHECK, "Lukas.pdf"))

    def test_both_states_present_and_bimodal(self):
        states = set(self.r.mark_states.values())
        self.assertEqual(states, {"active", "planned"})
        n_active = sum(1 for s in self.r.mark_states.values() if s == "active")
        # Every mark is classified; the plan is mostly pending grants.
        self.assertEqual(len(self.r.mark_states), len(self.r.marks))
        self.assertGreater(len(self.r.marks) - n_active, n_active)

    def test_template_column_all_planned(self):
        col = next(p.column for p in self.r.persons
                   if p.person_name == "A Muster Allgem")
        states = [s for (c, _r), s in self.r.mark_states.items() if c == col]
        self.assertEqual(len(states), 53)
        self.assertTrue(all(s == "planned" for s in states))


def _csv_marks(path):
    """Ground-truth (serial, normalized door name) authorizations from an
    LSM CSV export (UTF-16, ';'-separated; header rows 0-5, doors from 6)."""
    import csv as _csv
    import re as _re

    def norm(s):
        return _re.sub(r"\s+", " ", (s or "").strip()).casefold()

    txt = open(path, "rb").read().decode("utf-16")
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    rows = list(_csv.reader(io.StringIO(txt), delimiter=";"))
    ncol = max(len(r) for r in rows)
    serials = {c: rows[5][c].strip() for c in range(8, ncol)
               if c < len(rows[5]) and rows[5][c].strip()}
    marks, doors = set(), set()
    for ri in range(6, len(rows)):
        r = rows[ri]
        if len(r) <= 3 or not r[3].strip():
            continue
        doors.add(norm(r[3]))
        for c, ser in serials.items():
            if c < len(r) and r[c].strip() == "x":
                marks.add((ser, norm(r[3])))
    return marks, doors, set(serials.values())


@unittest.skipUnless(HAVE_ASTA, "ASTA-2026.pdf not present")
class Asta2026NativeTests(TestCase):
    """The 2026 ASTA export: a larger A3 native matrix (84×209) whose column
    headers sit higher than Lukas' A4 layout — the header-split must not miss
    their serials."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r = ocr.parse_native_matrix(ASTA_PDF)

    def test_routing_detects_native(self):
        self.assertTrue(ocr.is_native_matrix_pdf(ASTA_PDF))
        self.assertEqual(detect_format(ASTA_PDF), "matrix")

    def test_shape_and_all_serials_read(self):
        self.assertEqual(len(self.r.persons), 84)
        self.assertEqual(len(self.r.doors), 209)
        # Unlike Lukas, no serial is truncated in this export: all 84 read.
        self.assertTrue(all(p.serial_valid for p in self.r.persons))
        self.assertEqual(self.r.warnings, [])

    def test_distinct_doors_not_merged(self):
        # 209 rows, one genuine duplicate name -> 208 distinct.
        names = [d.name for d in self.r.doors]
        self.assertEqual(len(set(names)), 208)
        # Ost/West and O/W are different doors and must stay distinct.
        self.assertIn("Raum 201 O", names)
        self.assertIn("Raum 201 W", names)
        self.assertIn("Flur Bau 3 z. Bau 8 Ost", names)
        self.assertIn("Flur Bau 3 z. Bau 8 West", names)

    def test_real_numeric_suffix_kept(self):
        # The PB property column is stripped, but a real trailing number
        # (empty PB) survives: 'Mensa Vorhangschloss 1' keeps its 1.
        names = [d.name for d in self.r.doors]
        self.assertIn("Mensa Vorhangschloss 1", names)
        self.assertIn("Mensa Vorhangschloss 2", names)


@unittest.skipUnless(HAVE_ASTA_CSV, "ASTA-2026.csv not present")
class Asta2026CsvOracleTests(TestCase):
    """Validate the pixel-read marks against the CSV export of the same
    matrix — an independent ground truth for every authorization."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.r = ocr.parse_native_matrix(ASTA_PDF)
        cls.csv_marks, cls.csv_doors, cls.csv_serials = _csv_marks(ASTA_CSV)

    def _parser_marks(self):
        import re
        ser = {p.column: p.serial for p in self.r.persons}
        name = {d.row: d.name for d in self.r.doors}
        return {(ser[c], re.sub(r"\s+", " ", name[rw].strip()).casefold())
                for (c, rw) in self.r.marks}

    def test_columns_and_serials_match_csv(self):
        self.assertEqual({p.serial for p in self.r.persons}, self.csv_serials)

    def test_marks_match_csv_exactly(self):
        # The hollow-cross exclusion makes the pixel read reproduce the CSV
        # authorizations exactly: no missed marks, no spurious ones.
        par = self._parser_marks()
        self.assertEqual(len(self.csv_marks), 1553)
        self.assertEqual(par - self.csv_marks, set())   # no false positives
        self.assertEqual(self.csv_marks - par, set())   # no false negatives


@unittest.skipUnless(HAVE_ASTA, "ASTA-2026.pdf not present")
class Asta2026ImportTests(TestCase):
    def test_import_creates_all_distinct_doors(self):
        r = services.import_pdf(ASTA_PDF, "ASTA-2026.pdf")
        self.assertEqual(r["format"], "matrix")
        self.assertEqual(r["persons"], 84)
        self.assertEqual(len(r["skipped"]), 0)
        # 209 rows, one repeated name -> 208 distinct locks. A door must
        # never merge into a near-twin (Ost/West) of the same import.
        self.assertEqual(Lock.objects.count(), 208)
        self.assertEqual(Transponder.objects.count(), 84)
        for a, b in (("Raum 201 O", "Raum 201 W"),
                     ("Flur Bau 3 z. Bau 8 Ost", "Flur Bau 3 z. Bau 8 West")):
            self.assertNotEqual(
                Lock.objects.get(door_name=a).serial,
                Lock.objects.get(door_name=b).serial)


@unittest.skipUnless(HAVE_ASTA_CSV, "ASTA-2026.csv not present")
class Asta2026CombinedImportTests(TestCase):
    """CSV + PDF merge: CSV geometry/serials, PDF active/planned split."""

    def test_combined_import(self):
        from access.csv_import import import_asta_csv
        r = import_asta_csv(ASTA_CSV, ASTA_PDF, "ASTA-2026.csv")
        self.assertEqual(r["transponders"], 84)
        # 209 real lock serials -> two doors share a name but stay distinct.
        self.assertEqual(r["locks"], 209)
        self.assertEqual(Lock.objects.count(), 209)
        self.assertEqual(r["active"] + r["planned"], 1553)
        self.assertEqual(r["active"], 629)
        self.assertEqual(r["planned"], 924)
        # Real serials from the CSV, not synthetic MX: keys.
        self.assertFalse(Lock.objects.filter(serial__startswith="MX:").exists())
        self.assertTrue(Lock.objects.filter(serial__startswith="DC-").exists())
        # Full door names, not the PDF's truncation.
        self.assertTrue(Lock.objects.filter(
            door_name="Raum -1805 Wasserbau und Wasserwirtschaft").exists())
        # The two same-named locks are kept apart by serial.
        self.assertEqual(Lock.objects.filter(
            door_name="Raum -1342 Architekturmuseum").count(), 2)

    def test_wipe_is_required(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("import_asta", ASTA_CSV, ASTA_PDF)


class PdfExportTests(TestCase):
    """Matrix PDF export: data assembly (pure) + Typst rendering."""

    def setUp(self):
        from access import pdf_export
        self.pdf_export = pdf_export
        self.l1 = Lock.objects.create(serial="DC-1", door_name="Door A",
                                      location="Loc1")
        self.l2 = Lock.objects.create(serial="DC-2", door_name="Door B",
                                      location="Loc2")
        self.a = Transponder.objects.create(
            serial="AAA", asta_number=1, person_name="Alice")
        self.b = Transponder.objects.create(
            serial="BBB", asta_number=2, person_name="Bob")
        self.a.locks.add(self.l1)              # active
        self.a.planned_locks.add(self.l2)      # planned

    def test_data_ordering_and_marks(self):
        d = self.pdf_export.build_matrix_data("all")
        self.assertEqual([c["serial"] for c in d["transponders"]], ["AAA", "BBB"])
        self.assertEqual([x["serial"] for x in d["doors"]], ["DC-1", "DC-2"])
        # col 0 (Alice): row 0 (Door A) active=2, row 1 (Door B) planned=1.
        self.assertEqual(d["marks"], {"0-0": 2, "0-1": 1})

    def test_scope_filters_marks(self):
        self.assertEqual(
            self.pdf_export.build_matrix_data("active")["marks"], {"0-0": 2})
        self.assertEqual(
            self.pdf_export.build_matrix_data("planned")["marks"], {"0-1": 1})

    def test_invalid_args_rejected(self):
        with self.assertRaises(ValueError):
            self.pdf_export.build_matrix_data("bogus")
        with self.assertRaises(ValueError):
            self.pdf_export.render_pdf({}, "a5")

    @unittest.skipUnless(HAVE_TYPST, "typst binary not installed")
    def test_render_produces_pdf_both_sizes(self):
        data = self.pdf_export.build_matrix_data("all")
        for size in ("a4", "a3"):
            pdf = self.pdf_export.render_pdf(data, size)
            self.assertTrue(pdf.startswith(b"%PDF"))
            self.assertGreater(len(pdf), 1000)

    @unittest.skipUnless(HAVE_TYPST, "typst binary not installed")
    def test_command_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "m.pdf")
            call_command("exportpdf", "--size", "a4", "-o", out,
                         stdout=io.StringIO())
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(4), b"%PDF")

    @unittest.skipUnless(HAVE_TYPST, "typst binary not installed")
    def test_view_streams_pdf(self):
        r = self.client.get("/export.pdf?size=a4&scope=planned")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertTrue(r.getvalue().startswith(b"%PDF"))

    def test_diff_data_classifies_cells(self):
        l3 = Lock.objects.create(serial="DC-3", door_name="Door C",
                                 location="Loc3")
        # Alice wants l1 (has it active) and l3 (not configured); l2 (planned)
        # is not wished.
        self.a.desired_locks.set([self.l1, l3])
        d = self.pdf_export.build_diff_data()
        self.assertEqual(d["mode"], "diff")
        self.assertEqual(d["marks"]["0-0"], [2, 1])   # active + wished -> ok
        self.assertEqual(d["marks"]["0-1"], [1, 0])   # planned, unwished -> remove
        self.assertEqual(d["marks"]["0-2"], [0, 1])   # wished, absent -> add
        self.assertEqual(d["counts"], {"ok": 1, "add": 1, "remove": 1})

    def test_seed_desired_and_overwrite(self):
        call_command("seed_desired", stdout=io.StringIO())
        self.a.refresh_from_db()
        self.assertEqual(set(self.a.desired_locks.all()), {self.l1, self.l2})
        # A curated wish is left alone on re-run …
        self.a.desired_locks.remove(self.l2)
        call_command("seed_desired", stdout=io.StringIO())
        self.a.refresh_from_db()
        self.assertNotIn(self.l2, self.a.desired_locks.all())
        # … unless --overwrite resets it.
        call_command("seed_desired", "--overwrite", stdout=io.StringIO())
        self.a.refresh_from_db()
        self.assertEqual(set(self.a.desired_locks.all()), {self.l1, self.l2})

    @unittest.skipUnless(HAVE_TYPST, "typst binary not installed")
    def test_diff_view_and_render(self):
        self.a.desired_locks.set([self.l1])
        pdf = self.pdf_export.render_pdf(self.pdf_export.build_diff_data(), "a4")
        self.assertTrue(pdf.startswith(b"%PDF"))
        r = self.client.get("/export.pdf?mode=diff&size=a4")
        self.assertEqual(r.status_code, 200)
        self.assertIn("diff", r["Content-Disposition"])
        self.assertTrue(r.getvalue().startswith(b"%PDF"))

    def test_diff_hide_empty_drops_unused_doors(self):
        # l1 active on a, l2 planned on a; DC-3 has no rights; DC-4 is only a
        # hollow-× (pending removal) — which still counts as a right to keep.
        Lock.objects.create(serial="DC-3", door_name="Door C", location="Loc3")
        l4 = Lock.objects.create(serial="DC-4", door_name="Door D", location="Loc4")
        self.a.removed_locks.add(l4)
        full = self.pdf_export.build_diff_data()
        trimmed = self.pdf_export.build_diff_data(hide_empty=True)
        self.assertIn("DC-3", [d["serial"] for d in full["doors"]])
        # DC-3 (no rights) dropped; DC-4 (hollow ×) kept alongside active/planned.
        self.assertEqual({d["serial"] for d in trimmed["doors"]},
                         {"DC-1", "DC-2", "DC-4"})
        # dropping empty doors changes nothing about the actual diff counts
        self.assertEqual(trimmed["counts"], full["counts"])
        # every mark still points at a valid (re-indexed) row
        max_row = len(trimmed["doors"]) - 1
        for key in trimmed["marks"]:
            self.assertLessEqual(int(key.split("-")[1]), max_row)

    def test_changes_data_lists_adds_and_removes(self):
        l3 = Lock.objects.create(serial="DC-3", door_name="Door C",
                                 location="Loc3")
        # Alice: has l1 active + l2 planned (configured); wishes l1 + l3.
        self.a.desired_locks.set([self.l1, l3])
        d = self.pdf_export.build_changes_data()
        self.assertEqual(d["mode"], "changes")
        self.assertEqual(d["counts"], {"add": 1, "remove": 1, "transponders": 1})
        # Only Alice is affected; Bob has neither a wish nor any config.
        self.assertEqual([e["serial"] for e in d["changes"]], ["AAA"])
        entry = d["changes"][0]
        self.assertEqual([x["serial"] for x in entry["add"]], ["DC-3"])
        self.assertEqual([x["serial"] for x in entry["remove"]], ["DC-2"])
        self.assertEqual(entry["remove"][0]["note"], "geplant")  # l2 planned

    def test_changes_planned_flag_is_per_transponder(self):
        # Same door removed from two transponders — active on one, planned on
        # the other. Each entry must carry its own planned flag (no shared-dict
        # aliasing leaking one onto the other).
        self.a.locks.add(self.l2)                 # Alice: l2 ACTIVE (+ l1 active)
        self.b.locks.add(self.l1)                 # Bob has l1 active …
        self.b.planned_locks.add(self.l2)         # … and l2 PLANNED
        self.a.desired_locks.set([self.l1])       # Alice drops l2 (active)
        self.b.desired_locks.set([self.l1])       # Bob drops l2 (planned)
        d = self.pdf_export.build_changes_data()
        by = {e["serial"]: e for e in d["changes"]}
        a_rm = {x["serial"]: x for x in by["AAA"]["remove"]}
        b_rm = {x["serial"]: x for x in by["BBB"]["remove"]}
        self.assertEqual(a_rm["DC-2"]["note"], "")         # Alice: active removal
        self.assertEqual(b_rm["DC-2"]["note"], "geplant")  # Bob: pending removal

    def test_changes_hollow_excluded_unless_wished(self):
        l3 = Lock.objects.create(serial="DC-3", door_name="Door C",
                                 location="Loc3")
        l4 = Lock.objects.create(serial="DC-4", door_name="Door D",
                                 location="Loc4")
        self.a.removed_locks.add(l3, l4)          # both hollow (pending removal)
        self.a.desired_locks.set([self.l1, l4])   # want l1 (active) + l4 (a hollow)
        d = self.pdf_export.build_changes_data()
        entry = next(e for e in d["changes"] if e["serial"] == "AAA")
        rem = [x["serial"] for x in entry["remove"]]
        add = [x["serial"] for x in entry["add"]]
        # l3: hollow AND unwished -> source already removes it -> not in export.
        self.assertNotIn("DC-3", rem)
        self.assertNotIn("DC-3", add)
        # l4: hollow but WISHED -> must be re-authorised -> shows as an add.
        self.assertIn("DC-4", add)
        # l2 (planned, unwished) is still a removal, noted "geplant".
        self.assertEqual({x["serial"]: x["note"] for x in entry["remove"]},
                         {"DC-2": "geplant"})

    def test_changes_data_empty_when_all_match(self):
        # Wish exactly equals configured (active ∪ planned) -> no changes.
        self.a.desired_locks.set([self.l1, self.l2])
        d = self.pdf_export.build_changes_data()
        self.assertEqual(d["changes"], [])
        self.assertEqual(d["counts"], {"add": 0, "remove": 0, "transponders": 0})

    @unittest.skipUnless(HAVE_TYPST, "typst binary not installed")
    def test_changes_view_and_render(self):
        self.a.desired_locks.set([self.l1])          # -> remove l2 (planned)
        pdf = self.pdf_export.render_pdf(
            self.pdf_export.build_changes_data(), "a4")
        self.assertTrue(pdf.startswith(b"%PDF"))
        r = self.client.get("/export.pdf?mode=changes")   # defaults to A4
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertIn("changes", r["Content-Disposition"])
        self.assertTrue(r.getvalue().startswith(b"%PDF"))


class SollEditingTests(TestCase):
    """Group/desired editing: service math + AJAX endpoints + pages render."""

    def setUp(self):
        import json
        self.json = json
        self.L = [Lock.objects.create(serial=f"D{i}", door_name=f"Door {i}",
                                      location="Bau X") for i in range(1, 6)]
        self.tp = Transponder.objects.create(serial="AAA", person_name="A")
        self.g1 = Group.objects.create(name="G1")
        self.g2 = Group.objects.create(name="G2")
        self.g1.doors.set(self.L[:3])          # D1 D2 D3
        self.g2.doors.set(self.L[2:5])         # D3 D4 D5  (D3 shared)

    def _post(self, url, body):
        return self.client.post(url, data=self.json.dumps(body),
                                content_type="application/json")

    def test_assign_unassign_preserves_overlap(self):
        from access import soll
        soll.assign_group(self.tp, self.g1)
        soll.assign_group(self.tp, self.g2)
        self.assertEqual(self.tp.desired_locks.count(), 5)   # D1..D5
        soll.unassign_group(self.tp, self.g1)
        got = set(self.tp.desired_locks.values_list("serial", flat=True))
        # D3 stays (still in G2); D1,D2 gone.
        self.assertEqual(got, {"D3", "D4", "D5"})
        self.assertNotIn(self.g1, self.tp.groups.all())

    def test_group_door_edit_propagates_to_members(self):
        from access import soll
        soll.assign_group(self.tp, self.g1)          # desired D1 D2 D3
        soll.set_group_doors(self.g2, [self.L[4]], True)  # add D5 to G2
        soll.assign_group(self.tp, self.g2)          # now member of G2 too
        self.assertIn("D5", self.tp.desired_locks.values_list("serial", flat=True))
        # Removing D3 from G1 keeps it (still in G2, which tp also has).
        soll.set_group_doors(self.g1, [self.L[2]], False)
        self.assertIn("D3", self.tp.desired_locks.values_list("serial", flat=True))

    def test_toggle_endpoint_batch(self):
        self._post("/soll/toggle/", {"ops": [
            {"kind": "group", "id": self.g1.id, "on": False, "locks": ["D1", "D2"]},
            {"kind": "tp", "id": "AAA", "on": True, "locks": ["D4", "D5"]},
        ]})
        self.assertEqual(set(self.g1.doors.values_list("serial", flat=True)), {"D3"})
        self.assertEqual(set(self.tp.desired_locks.values_list("serial", flat=True)),
                         {"D4", "D5"})

    def test_group_assign_endpoint(self):
        r = self._post("/soll/group-assign/",
                       {"transponder": "AAA", "group": self.g1.id, "assigned": True})
        self.assertEqual(r.json()["desired"], 3)
        self.assertIn(self.g1, self.tp.groups.all())

    def test_group_crud_endpoints(self):
        r = self._post("/groups/create/", {"name": "New"})
        gid = r.json()["id"]
        self._post(f"/groups/{gid}/rename/", {"name": "Renamed"})
        self.assertEqual(Group.objects.get(pk=gid).name, "Renamed")
        self._post(f"/groups/{gid}/delete/", {})
        self.assertFalse(Group.objects.filter(pk=gid).exists())

    def test_copy_and_clear(self):
        self.tp.locks.set(self.L[:2])
        self.tp.planned_locks.set([self.L[2]])
        self._post("/transponders/AAA/soll/", {"action": "copy"})
        self.assertEqual(self.tp.desired_locks.count(), 3)   # active ∪ planned
        self._post("/transponders/AAA/soll/", {"action": "clear"})
        self.assertEqual(self.tp.desired_locks.count(), 0)

    def test_pages_render(self):
        for url in ["/soll/", "/soll/?tp=AAA", "/groups/",
                    f"/groups/{self.g1.id}/", "/transponders/AAA/", "/locks/D1/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_admin_removed(self):
        self.assertEqual(self.client.get("/admin/").status_code, 404)

    def test_group_door_edit_propagates_to_current_members(self):
        from access import soll
        soll.assign_group(self.tp, self.g1)          # tp ∈ g1 (D1 D2 D3)
        soll.set_group_doors(self.g1, [self.L[3]], True)   # add D4
        self.assertIn("D4", self.tp.desired_locks.values_list("serial", flat=True))
        soll.set_group_doors(self.g1, [self.L[0]], False)  # remove D1 (only in g1)
        self.assertNotIn("D1", self.tp.desired_locks.values_list("serial", flat=True))

    def test_copy_clears_group_membership(self):
        from access import soll
        soll.assign_group(self.tp, self.g1)
        self.tp.locks.set(self.L[:1])
        soll.copy_current_to_desired(self.tp)
        self.assertEqual(self.tp.groups.count(), 0)   # groups cleared
        self.assertEqual(
            set(self.tp.desired_locks.values_list("serial", flat=True)), {"D1"})

    def test_toggle_bad_json_and_unknown_ids_are_safe(self):
        r = self.client.post("/soll/toggle/", data="not json",
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)
        before = set(self.g1.doors.values_list("serial", flat=True))
        self._post("/soll/toggle/", {"ops": [
            {"kind": "group", "id": 99999, "on": False, "locks": ["D1"]},
            {"kind": "group", "id": "abc", "on": False, "locks": ["D1"]},   # non-int
            {"kind": "tp", "id": "NOPE", "on": True, "locks": ["D1"]},
            {"kind": "group", "id": self.g1.id, "on": True, "locks": ["NOLOCK"]},
        ]})
        self.assertEqual(set(self.g1.doors.values_list("serial", flat=True)), before)

    def test_group_assign_bad_request(self):
        r = self.client.post("/soll/group-assign/", data="{}",
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_reverse_door_editor(self):
        self.tp.desired_locks.add(self.L[0])
        r = self.client.get("/locks/D1/")
        self.assertIn(self.tp, list(r.context["desirers"]))
        self.assertNotIn(self.tp, list(r.context["addable"]))
        self._post("/soll/toggle/",
                   {"ops": [{"kind": "tp", "id": "AAA", "on": False, "locks": ["D1"]}]})
        self.assertNotIn(self.L[0], self.tp.desired_locks.all())


class OverlapScopeTests(TestCase):
    """The /overlap/ view can be read over current (active) access or the
    planned end state (active ∪ planned)."""

    def setUp(self):
        L = [Lock.objects.create(serial=f"L{i}", door_name=f"Door {i}")
             for i in range(1, 4)]
        self.a = Transponder.objects.create(serial="AAA1111", person_name="A")
        self.b = Transponder.objects.create(serial="BBB2222", person_name="B")
        self.a.locks.set(L[:2])            # active {L1,L2}
        self.a.planned_locks.set([L[2]])   # planned {L3}
        self.b.locks.set([L[0]])           # active {L1}
        self.b.planned_locks.set(L[1:])    # planned {L2,L3}

    def _pct(self, resp):
        tps = list(resp.context["tps"])
        rows = resp.context["rows"]
        ia = next(i for i, t in enumerate(tps) if t.serial == "AAA1111")
        ib = next(i for i, t in enumerate(tps) if t.serial == "BBB2222")
        return rows[ia][1][ib]["pct"]

    def test_scope_changes_similarity(self):
        active = self.client.get("/overlap/?scope=active")
        planned = self.client.get("/overlap/?scope=planned")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.context["scope"], "active")
        self.assertEqual(planned.context["scope"], "planned")
        self.assertTrue(active.context["planned_pending"])
        # active: A{L1,L2} vs B{L1} = 1/2 = 50%. planned: both {L1,L2,L3} = 100%.
        self.assertEqual(self._pct(active), 50)
        self.assertEqual(self._pct(planned), 100)

    def test_diff_scope_uses_only_planned(self):
        r = self.client.get("/overlap/?scope=diff")
        self.assertEqual(r.context["scope"], "diff")
        # diff: A{L3} vs B{L2,L3} -> shared 1, union 2 -> 50%.
        self.assertEqual(self._pct(r), 50)

    def test_diff_scope_zero_when_no_shared_pending(self):
        # A transponder with no pending changes shares nothing in the diff view.
        c = Transponder.objects.create(serial="CCC3333", person_name="C")
        c.locks.set(Lock.objects.all())          # all active, none planned
        r = self.client.get("/overlap/?scope=diff")
        tps = list(r.context["tps"])
        rows = r.context["rows"]
        ic = next(i for i, t in enumerate(tps) if t.serial == "CCC3333")
        ia = next(i for i, t in enumerate(tps) if t.serial == "AAA1111")
        self.assertEqual(rows[ic][1][ia]["pct"], 0)

    def test_default_scope_is_active(self):
        self.assertEqual(self.client.get("/overlap/").context["scope"], "active")

    def test_unknown_scope_falls_back_to_active(self):
        self.assertEqual(
            self.client.get("/overlap/?scope=bogus").context["scope"], "active")

    def test_toggle_hidden_without_pending(self):
        self.a.planned_locks.clear()
        self.b.planned_locks.clear()
        r = self.client.get("/overlap/")
        self.assertFalse(r.context["planned_pending"])
        self.assertNotIn("Geplanter Endzustand", r.content.decode())

    def test_empty_scoped_sets_not_clone_grouped(self):
        # Two transponders with no pending change share an EMPTY diff set; they must
        # not be reported as a bogus "identical access" clone group.
        L = list(Lock.objects.all())
        c = Transponder.objects.create(serial="CCC3333")
        d = Transponder.objects.create(serial="DDD4444")
        c.locks.set([L[0]])                # active only, nothing planned
        d.locks.set([L[1]])
        groups = self.client.get("/overlap/?scope=diff").context["clone_groups"]
        for g in groups:
            serials = {t.serial for t in g["transponders"]}
            self.assertNotIn("CCC3333", serials)
            self.assertNotIn("DDD4444", serials)

    def test_clone_group_door_count_is_scoped(self):
        # Two identical transponders form a clone group whose door count reflects the
        # scope (active∪planned under scope=planned), not the raw active count.
        L = list(Lock.objects.all())
        for s in ("EEE5555", "FFF6666"):
            t = Transponder.objects.create(serial=s)
            t.locks.set(L[:2])             # active {L1,L2}
            t.planned_locks.set([L[2]])    # planned {L3}
        groups = self.client.get(
            "/overlap/?scope=planned").context["clone_groups"]
        grp = next(g for g in groups
                   if any(t.serial == "EEE5555" for t in g["transponders"]))
        self.assertEqual(grp["doors"], 3)  # active∪planned, not the 2 active


class InheritedAndIndividualTests(TestCase):
    """Group-inherited vs individual: editor flags + the /individual/ audit."""

    def setUp(self):
        self.L = [Lock.objects.create(serial=f"D{i}", door_name=f"Door {i}",
                                      location="Bau X") for i in range(1, 6)]
        self.tp = Transponder.objects.create(serial="AAA", person_name="A",
                                             asta_number=1)
        self.g1 = Group.objects.create(name="G1")
        self.g1.doors.set(self.L[:3])          # D1 D2 D3

    def _rowmap(self, ctx):
        return {row["lock"].serial: row
                for sec in ctx["sections"] for row in sec["rows"]}

    def test_transponder_editor_flags_inherited_vs_individual(self):
        from access import soll
        soll.assign_group(self.tp, self.g1)        # D1 D2 D3 inherited
        self.tp.desired_locks.add(self.L[3])       # D4 individual
        rows = self._rowmap(self.client.get("/transponders/AAA/").context)
        self.assertTrue(rows["D1"]["inherited"])
        self.assertEqual(rows["D1"]["via"], "G1")
        self.assertTrue(rows["D1"]["on"])
        self.assertFalse(rows["D4"]["inherited"])  # individual desired
        self.assertTrue(rows["D4"]["on"])
        self.assertFalse(rows["D5"]["inherited"])  # not desired at all
        self.assertFalse(rows["D5"]["on"])

    def test_soll_matrix_flags_inherited_tp_cell(self):
        from access import soll
        soll.assign_group(self.tp, self.g1)
        ctx = self.client.get("/soll/?tp=AAA").context
        cell = next(c for sec in ctx["sections"] for row in sec["rows"]
                    if row["lock"].serial == "D1"
                    for c in row["cells"] if c["kind"] == "tp")
        self.assertTrue(cell["inherited"])

    def test_individual_soll_scope_lists_individual_desired(self):
        # Default scope is the Soll: individual desired doors (not from a group).
        from access import soll
        soll.assign_group(self.tp, self.g1)          # D1 D2 D3 desired via group
        self.tp.desired_locks.add(self.L[3])         # D4 individual Soll
        self.tp.locks.set([self.L[3]])               # D4 already active
        ctx = self.client.get("/individual/").context
        self.assertEqual(ctx["scope"], "soll")
        self.assertEqual(len(ctx["rows"]), 1)
        states = {d["lock"].serial: d["state"] for d in ctx["rows"][0]["doors"]}
        self.assertEqual(states, {"D4": "active"})   # D1-D3 excluded (group)
        self.assertEqual(ctx["n_grants"], 1)

    def test_individual_soll_add_state(self):
        # A Soll door not yet programmed is tagged 'add'; ?scope=planned falls
        # through to the Soll view.
        from access import soll
        soll.assign_group(self.tp, self.g1)
        self.tp.desired_locks.add(self.L[3])         # D4 wished, not programmed
        ctx = self.client.get("/individual/?scope=planned").context
        self.assertEqual(ctx["scope"], "soll")
        states = {d["lock"].serial: d["state"] for d in ctx["rows"][0]["doors"]}
        self.assertEqual(states, {"D4": "add"})

    def test_individual_ist_scope_includes_planned_and_removed(self):
        # Ist = active ∪ planned ∪ hollow-×; ?scope=active maps to ist.
        from access import soll
        soll.assign_group(self.tp, self.g1)          # D1 D2 D3
        self.tp.locks.set([self.L[0], self.L[2]])    # active: D1(group), D3(group)
        self.tp.planned_locks.set([self.L[3]])       # planned: D4 (individual)
        self.tp.removed_locks.set([self.L[4]])       # hollow ×: D5 (individual)
        ctx = self.client.get("/individual/?scope=active").context
        self.assertEqual(ctx["scope"], "ist")
        states = {d["lock"].serial: d["state"] for d in ctx["rows"][0]["doors"]}
        self.assertEqual(states, {"D4": "planned", "D5": "removed"})  # groups excl.

    def test_individual_soll_removed_is_conflict_not_add(self):
        # A wished door that is currently a hollow-× shows 'removed', not 'add'.
        from access import soll
        soll.assign_group(self.tp, self.g1)
        self.tp.desired_locks.add(self.L[3])         # D4 individual Soll
        self.tp.removed_locks.add(self.L[3])         # but currently pending removal
        ctx = self.client.get("/individual/").context
        states = {d["lock"].serial: d["state"] for d in ctx["rows"][0]["doors"]}
        self.assertEqual(states, {"D4": "removed"})

    def test_individual_omits_covered_and_keeps_groupless(self):
        from access import soll
        # AAA's Soll is fully its group -> no individual Soll -> omitted.
        soll.assign_group(self.tp, self.g1)
        # BBB has no group but an individual Soll door.
        b = Transponder.objects.create(serial="BBB")
        b.desired_locks.add(self.L[0])
        ctx = self.client.get("/individual/").context
        self.assertEqual([r["tp"].serial for r in ctx["rows"]], ["BBB"])
        self.assertEqual(ctx["rows"][0]["n"], 1)

    def test_lock_detail_individual_desirers_and_status(self):
        from access import soll
        d1 = self.L[0]
        soll.assign_group(self.tp, self.g1)   # AAA desires D1 via group (inherited)
        self.tp.locks.add(d1)                 # AAA active on D1 -> keep
        b = Transponder.objects.create(serial="BBB")
        b.desired_locks.add(d1)               # BBB desires D1 individually
        c = Transponder.objects.create(serial="CCC")
        c.locks.add(d1)                       # active, not desired -> will be removed
        d = Transponder.objects.create(serial="DDD")
        d.planned_locks.add(d1)               # planned
        e = Transponder.objects.create(serial="EEE")
        e.removed_locks.add(d1)               # hollow × -> withdrawn, pending removal
        ctx = self.client.get(f"/locks/{d1.serial}/").context
        # individual desirers only — AAA (group-inherited) excluded
        self.assertEqual([t.serial for t in ctx["desirers"]], ["BBB"])
        cols = {col["title"]: [it["tp"].serial for it in col["items"]]
                for col in ctx["status_cols"]}
        self.assertEqual(cols["Bleibt"], ["AAA"])
        self.assertEqual(cols["Wird entfernt"], ["CCC", "EEE"])  # active∖Soll + hollow
        self.assertEqual(cols["Geplant"], ["DDD"])
        badges = {it["tp"].serial: it["badge"]
                  for col in ctx["status_cols"] for it in col["items"]}
        self.assertEqual(badges["EEE"], "entzogen")        # from removed_locks
        self.assertEqual(badges["CCC"], "nicht im Soll")   # active but unwished

    def test_import_matrix_routes_hollow_to_removed(self):
        # A synthetic matrix with one hollow (remove) mark lands in removed_locks,
        # not in active/planned.
        from access import services, ocr
        res = ocr.MatrixResult(source_file="x.pdf", ocr_scan=True)
        res.persons.append(ocr.MatrixPerson(
            column=1, serial="ZZZ", raw_serial="ZZZ", serial_valid=True,
            serial_suspect=False, asta_number=None, person_name="Z"))
        res.doors.append(ocr.MatrixDoor(row=1, name=self.L[0].door_name))
        res.marks.add((1, 1))
        res.mark_states[(1, 1)] = "remove"
        services._import_matrix(res, "x.pdf")
        z = Transponder.objects.get(serial="ZZZ")
        self.assertEqual([lk.serial for lk in z.removed_locks.all()],
                         [self.L[0].serial])
        self.assertEqual(z.locks.count(), 0)
        self.assertEqual(z.planned_locks.count(), 0)

    def test_group_list_counts_are_distinct(self):
        # g1 has 3 doors; give it 2 members. Without distinct=True both counts
        # would cross-multiply to 3×2 = 6.
        from access import soll
        soll.assign_group(self.tp, self.g1)
        soll.assign_group(Transponder.objects.create(serial="BBB"), self.g1)
        groups = {g.name: g for g in self.client.get("/groups/").context["groups"]}
        self.assertEqual(groups["G1"].n, 3)   # doors, not 6
        self.assertEqual(groups["G1"].m, 2)   # members, not 6

    def test_lock_detail_removed_and_desired_shows_keep(self):
        # A hollow-× door that the Soll still wants is a re-grant → "Bleibt".
        d1 = self.L[0]
        t = Transponder.objects.create(serial="RRR")
        t.removed_locks.add(d1)
        t.desired_locks.add(d1)
        cols = {c["title"]: {it["tp"].serial: it["badge"] for it in c["items"]}
                for c in self.client.get(f"/locks/{d1.serial}/").context["status_cols"]}
        self.assertEqual(cols["Bleibt"].get("RRR"), "Soll: behalten")
        self.assertNotIn("RRR", cols["Wird entfernt"])

    def test_import_reactivation_clears_removed(self):
        from access import services, ocr
        tp = Transponder.objects.create(serial="YYY")
        tp.removed_locks.add(self.L[0])          # prior hollow ×
        res = ocr.MatrixResult(source_file="x.pdf", ocr_scan=True)
        res.persons.append(ocr.MatrixPerson(
            column=1, serial="YYY", raw_serial="YYY", serial_valid=True,
            serial_suspect=False, asta_number=None, person_name=""))
        res.doors.append(ocr.MatrixDoor(row=1, name=self.L[0].door_name))
        res.marks.add((1, 1))
        res.mark_states[(1, 1)] = "active"       # now a bold ×
        services._import_matrix(res, "x.pdf")
        tp.refresh_from_db()
        self.assertIn(self.L[0], tp.locks.all())              # active now
        self.assertNotIn(self.L[0], tp.removed_locks.all())   # removed cleared

    def test_set_desired_ignores_inherited_off_toggle(self):
        from access import soll
        soll.assign_group(self.tp, self.g1)      # D1 D2 D3 inherited & desired
        # A replayed/hostile off-toggle of an inherited door must be ignored.
        soll.set_desired(self.tp, [self.L[0]], wished=False)
        self.assertIn(self.L[0], self.tp.desired_locks.all())
        # But a genuinely individual door can still be toggled off.
        self.tp.desired_locks.add(self.L[3])     # D4 individual
        soll.set_desired(self.tp, [self.L[3]], wished=False)
        self.assertNotIn(self.L[3], self.tp.desired_locks.all())


GATE_MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
]


@override_settings(MIDDLEWARE=GATE_MIDDLEWARE)
class LoginGateTests(TestCase):
    """With the login gate enabled the UI needs a session; login stays public."""

    def test_anonymous_is_redirected_to_login(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    def test_soll_toggle_endpoint_also_gated(self):
        r = self.client.get("/soll/")
        self.assertEqual(r.status_code, 302)

    def test_login_page_is_public(self):
        r = self.client.get("/accounts/login/")
        self.assertEqual(r.status_code, 200)

    def test_authenticated_user_gets_through(self):
        from django.contrib.auth import get_user_model
        get_user_model().objects.create_user("u", password="pw-abc-12345")
        self.client.login(username="u", password="pw-abc-12345")
        self.assertEqual(self.client.get("/").status_code, 200)


class EnsureUserCommandTests(TestCase):
    def test_creates_then_updates_from_env(self):
        from django.contrib.auth import authenticate
        env = {"KEYMGMT_ADMIN_USERNAME": "boss", "KEYMGMT_ADMIN_PASSWORD": "s3cret-pw-9"}
        with mock.patch.dict(os.environ, env):
            call_command("ensure_user", stdout=io.StringIO())
        self.assertIsNotNone(authenticate(username="boss", password="s3cret-pw-9"))
        # idempotent + rotates the password on re-run
        with mock.patch.dict(os.environ, {**env, "KEYMGMT_ADMIN_PASSWORD": "new-pw-77"}):
            call_command("ensure_user", stdout=io.StringIO())
        self.assertIsNotNone(authenticate(username="boss", password="new-pw-77"))

    def test_noop_without_env(self):
        from django.contrib.auth import get_user_model
        out = io.StringIO()
        call_command("ensure_user", stdout=out)
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertIn("skipping", out.getvalue())
