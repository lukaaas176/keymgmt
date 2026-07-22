"""
matrix_parser.py
================

Parse SimonsVoss "Schließmatrix" (locking-plan matrix) exports.

This is the second printout format the locking software produces: a grid
with one column per transponder and one row per door. The transponder
columns are labelled with 90°-rotated text (name, then the serial in the
"SN" band), door rows are labelled horizontally on the left, and an X at
an intersection means "this transponder opens this door". Each page
carries a footer like ``Zeile 12-40 ; Spalte 27-80`` stating which slice
of the full matrix it shows, which lets us validate coverage the same way
the list parser validates against "Anzahl der Datensätze".

The parser handles two kinds of input:

* **Native text-layer PDFs** — rotated header text extracts in reading
  order.
* **Scanned printouts with a scanner-generated OCR layer** — the OCR
  engine reads the rotated column headers in the wrong direction, so
  every word comes out character-reversed ('ATSA' for ASTA, 'AB4PU20'
  for 02UP4BA) and stacked top-to-bottom. Both orientations are tried and
  the one that yields more well-formed serials wins.

Scanner OCR also garbles serials with systematic lookalike confusions
(O↔0, l/I↔1, g↔9, tJ/IJ↔U, ...). `repair_serial` fixes everything that
can be fixed from the serial alphabet alone and flags the rest as
suspect; resolving suspects against already-known serials is the import
layer's job (see services.py), because that needs the database.

No Django imports here — the module is usable standalone, like
pdf_parser.py.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

import pdfplumber

# --- Patterns ---------------------------------------------------------------

# Transponder/lock serials: '03UAG03', '010A0SC' style, the 'T-00061' /
# 'TC-00296' loaner-transponder style, and the 'LC-1234' style from the list format.
# The alphabet excludes O and I (always 0 and 1 across every known serial),
# which lets validation reject unrepaired OCR reads like '03UAPOB'.
RE_SERIAL = re.compile(r"^(?:\d{2}[0-9A-HJ-NP-Z]{5}|TC?-\d{4,6}|LC-\d{4})$")

# Page footer: "Zeile 1-0 ; Spalte 27-80" (the row/column slice of the whole
# matrix this page shows). OCR may prepend stray quote marks to the numbers,
# hence the \D gaps.
RE_FOOTER = re.compile(
    r"Zeile\D{0,3}(\d+)\s*-\s*(\d+)\D{1,8}Spalte\D{0,3}(\d+)\s*-\s*(\d+)")

# A fragment of that footer: scanner OCR can jitter the footer words apart
# vertically, splitting them over several visual "lines".
RE_FOOTER_FRAG = re.compile(r"(?:Zeile|Spalte)\D{0,3}\d+\s*-\s*\d+")

# "ASTA 51 Justus, Rossmeier" -> (51, 'Justus, Rossmeier')
RE_ASTA = re.compile(r"^ASTA\s*(\d{1,3})\b[.,]?\s*(.*)$")

# List-format marker, used by detect_format().
LIST_MARKER = "Berechtigungen für den Transponder"

X_MARK_TOKENS = {"x", "×", "✗", "✕"}

# Attribute columns that may follow the door name, depending on how the
# printout was configured (RN = room number, E = floor).
DOOR_ATTR_TOKENS = {"PB", "RN", "ZB", "N", "E", "AM", "PIN", "IM"}

# Rotated header words are as wide as the font is tall (~8-11pt). Anything
# wider is upright text bleeding into the rotated set (logo fragments).
MAX_ROTATED_WORD_WIDTH = 16.0

# Person columns sit ~10.5pt apart; word centres inside one column jitter
# by ~±2pt, so a 5pt gap safely separates neighbouring columns.
STRIP_GAP = 5.0


# --- Parsed structures -------------------------------------------------------

@dataclass
class MatrixPerson:
    """One transponder column of the matrix."""
    column: int                     # 1-based global column number
    serial: str                     # repaired serial (best effort)
    raw_serial: str                 # as read from the text layer
    serial_valid: bool              # repaired serial matches RE_SERIAL
    serial_suspect: bool            # repair went beyond trivial fixes
    asta_number: int | None
    person_name: str                # remainder of the header, verbatim


@dataclass
class MatrixDoor:
    """One door row of the matrix (no lock serial in this format)."""
    row: int                        # 1-based global row number
    name: str
    room_number: str = ""
    floor: str = ""


@dataclass
class MatrixResult:
    source_file: str = ""
    persons: list[MatrixPerson] = field(default_factory=list)
    doors: list[MatrixDoor] = field(default_factory=list)
    marks: set[tuple[int, int]] = field(default_factory=set)  # (column, row)
    # (column, row) -> "active" (bold ×, programmed) | "planned" (thin ×,
    # will be granted at the next terminal update). Only populated by the
    # native-PDF matrix reader, which can tell the two mark weights apart;
    # elsewhere it stays empty and every mark is treated as active.
    mark_states: dict[tuple[int, int], str] = field(default_factory=dict)
    expected_columns: int | None = None   # from the largest 'Spalte a-b' footer
    expected_rows: int | None = None      # from the largest 'Zeile a-b' footer
    ocr_scan: bool = False                # text layer came from scanner OCR
    warnings: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        cols_ok = (self.expected_columns is None
                   or self.expected_columns == len(self.persons))
        rows_ok = (self.expected_rows is None
                   or self.expected_rows == len(self.doors))
        return cols_ok and rows_ok


# --- Serial repair -----------------------------------------------------------

# Characters that never occur in a serial and are pure scan noise.
_STRIP_CHARS = set("'’‘`\",.  ")

# The serial alphabet is uppercase letters + digits and (by observation
# across every known serial) contains neither 'O' nor 'I' — those glyphs are
# always 0 and 1. The same holds for the lowercase lookalikes, so mapping
# them is not evidence of a bad read.
_TRIVIAL = {"O": "0", "o": "0", "I": "1", "l": "1", "i": "1"}

# OCR digraphs: a 'U' scanned at low quality splits into two glyphs.
# Applied before the single-character maps so 'IJ' is seen intact. Every
# entry must contain a character that is impossible in a real serial —
# a digit-1 variant ("1J") would corrupt legitimate serials containing
# the legal substring 1J whenever the full-repair path runs.
_DIGRAPHS = (("tJ", "U"), ("IJ", "U"), ("lJ", "U"), ("tt", "U"))

# Lowercase letters cannot occur in a serial at all, so each one is some
# misreading; this map is derived from scan/printout pairs.
_LOWER = {"g": "9", "s": "5", "q": "9", "t": "L"}
_SYMBOLS = {"¿": "2", "Ø": "0", "ø": "0"}

# In the two leading positions of a 7-char serial only digits are legal.
_DIGIT_POS = {"L": "1", "T": "1", "S": "5", "B": "8", "G": "6", "Z": "2",
              "A": "4", "Q": "0", "D": "0"}


def _strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def repair_serial(raw: str) -> tuple[str, bool, bool]:
    """Normalize an OCR-read serial.

    Returns (serial, valid, suspect): `valid` when the result matches
    RE_SERIAL, `suspect` when repairs beyond the trivial O→0 / l→1 /
    diacritic fixes were needed — the caller should then double-check the
    serial, e.g. against serials already known to the database.
    """
    s = _strip_diacritics(raw)
    s = "".join(c for c in s if c not in _STRIP_CHARS)
    if RE_SERIAL.match(s):
        return s, True, False

    trivial = "".join(_TRIVIAL.get(c, c) for c in s)
    if RE_SERIAL.match(trivial):
        return trivial, True, False

    # Full repair; anything from here on marks the serial as suspect.
    for a, b in _DIGRAPHS:
        s = s.replace(a, b)
    s = "".join(_TRIVIAL.get(c, _SYMBOLS.get(c, _LOWER.get(c, c))) for c in s)
    s = s.upper()
    if len(s) == 7 and "-" not in s:
        # Enforce the two leading digits of the 7-char shape.
        s = "".join(_DIGIT_POS.get(c, c) for c in s[:2]) + s[2:]
    return s, bool(RE_SERIAL.match(s)), True


def lookalike_equal(a: str, b: str) -> bool:
    """True when two serials differ only by OCR-lookalike characters.

    Used to resolve scan-read serials against known ones. The comparison
    is strictly positional — no insertions or transpositions — so
    genuinely distinct serials like 010A0CS / 010A0SC never match.
    """
    if len(a) != len(b):
        return False
    pairs = {("O", "0"), ("Q", "0"), ("D", "0"), ("I", "1"), ("L", "1"),
             ("S", "5"), ("S", "8"), ("S", "9"), ("B", "8"), ("Z", "2"),
             ("G", "6"), ("G", "9"), ("T", "7"), ("A", "4"), ("1", "7"),
             ("0", "9")}
    for x, y in zip(a.upper(), b.upper()):
        if x != y and (x, y) not in pairs and (y, x) not in pairs:
            return False
    return True


# --- Geometry helpers --------------------------------------------------------

def _x_center(w) -> float:
    return (w["x0"] + w["x1"]) / 2


def _cluster_strips(words, gap: float = STRIP_GAP):
    """Group rotated words into vertical strips (one per matrix column)."""
    ws = sorted(words, key=_x_center)
    strips, cur, prev = [], [], None
    for w in ws:
        c = _x_center(w)
        if prev is None or c - prev <= gap:
            cur.append(w)
        else:
            strips.append(cur)
            cur = [w]
        prev = c
    if cur:
        strips.append(cur)
    return strips


def _cluster_lines(words, tol: float = 3.0):
    """Group upright words into visual lines by vertical position."""
    words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines, cur, top = [], [], None
    for w in words:
        if top is None or abs(w["top"] - top) <= tol:
            cur.append(w)
            top = w["top"] if top is None else top
        else:
            lines.append(sorted(cur, key=lambda x: x["x0"]))
            cur, top = [w], w["top"]
    if cur:
        lines.append(sorted(cur, key=lambda x: x["x0"]))
    return lines


# --- Column (person) extraction ---------------------------------------------

def _read_strip(strip, flipped: bool) -> list[str]:
    """Return a strip's words in reading order.

    The rotated header text reads bottom-to-top on the page. A native PDF
    stores it in reading order already; a scanner OCR layer stores it
    top-to-bottom with every word character-reversed (`flipped`).
    """
    ordered = sorted(strip, key=lambda w: -w["top"])
    if flipped:
        return [w["text"][::-1] for w in ordered]
    return [w["text"] for w in ordered]


def _serial_band(strips, flipped: bool):
    """Find the vertical band where the SN row lives.

    Serials are the one part of a column recognisable from shape alone.
    A serial can also appear *inside* a person's name ("V Tresor, Flo,
    Nicht vorhanden 03U4345"), so the band is the largest vertical
    cluster of serial-shaped words — that is the SN row — rather than
    the hull of all of them.
    """
    hits = []
    for strip in strips:
        for w in strip:
            text = w["text"][::-1] if flipped else w["text"]
            if repair_serial(text)[1]:
                hits.append((w["top"], w["bottom"]))
    if not hits:
        return None
    hits.sort()
    groups, cur = [], [hits[0]]
    for t, b in hits[1:]:
        if t - cur[-1][0] <= 15.0:
            cur.append((t, b))
        else:
            groups.append(cur)
            cur = [(t, b)]
    groups.append(cur)
    # Most serial-shaped words wins; on a tie, the lowest band on the page
    # (the SN row always sits below the name region in this layout).
    best = max(groups, key=lambda g: (len(g), g[0][0]))
    return min(t for t, _ in best) - 4.0, max(b for _, b in best) + 4.0


def _extract_persons_oriented(rotated_words, flipped: bool):
    """One orientation attempt.

    Returns (persons, score) where persons is a list of
    (serial, raw_serial, valid, suspect, name, strip_x_center) tuples in
    left-to-right page order. The score counts serials that validate
    *without* confusion repairs — repaired ones don't count, because the
    repair maps can manufacture a "valid" serial out of reversed junk.
    """
    band = _serial_band(_cluster_strips(rotated_words), flipped)
    if band is None:
        return [], 0
    band_top, band_bottom = band

    # Words below the SN band are expiry dots / PB junk / footer noise;
    # dropping them keeps neighbouring strips from being bridged.
    strips = _cluster_strips(
        [w for w in rotated_words if w["top"] <= band_bottom])

    persons, n_valid = [], 0
    for strip in strips:
        sn_words = [w for w in strip if band_top <= w["top"] <= band_bottom]
        name_words = [w for w in strip if w["top"] < band_top]
        if not sn_words:
            continue                      # logo fragments, stray marks
        raw_serial = "".join(_read_strip(sn_words, flipped))
        name = " ".join(_read_strip(name_words, flipped)).strip()
        if raw_serial.upper() == "SN" or "PERSONEN" in name.upper():
            continue                      # the axis-label column itself
        serial, valid, suspect = repair_serial(raw_serial)
        if valid and not suspect:
            n_valid += 1
        center = sum(_x_center(w) for w in strip) / len(strip)
        persons.append((serial, raw_serial, valid, suspect, name, center))
    return persons, n_valid


# --- Row (door) extraction ---------------------------------------------------

def _extract_doors(upright_words, strip_centers):
    """Parse door rows and X marks from the upright text of one page.

    `strip_centers` are the x-centres of the person columns (left to
    right), used to map an X mark to its column index. Attribute columns
    (RN = room, E = floor) are located from the 'NAME (TÜREN/
    SCHLIESSUNGEN)' header row when present; without it the whole
    left-hand text becomes the door name.

    Returns (doors, marks) with 1-based page-local rows in both.
    """
    grid_left = min(strip_centers) - STRIP_GAP - 2

    header_top = None
    anchors = {}
    for ln in _cluster_lines(upright_words):
        text = " ".join(w["text"] for w in ln).upper()
        if "NAME" in text and ("SCHLIESSUNGEN" in text or "TÜREN" in text
                               or "TUREN" in text):
            header_top = ln[0]["top"]
            anchors = {w["text"]: w["x0"] for w in ln
                       if w["text"] in DOOR_ATTR_TOKENS}
            break

    def attr_value(key, attr_words):
        lo = anchors.get(key)
        if lo is None:
            return ""
        higher = [x for x in anchors.values() if x > lo]
        hi = min(higher) if higher else grid_left
        return " ".join(w["text"] for w in attr_words
                        if lo - 4 <= w["x0"] < hi - 4).strip()

    # Locate the footer band first: OCR jitter can split the footer words
    # over several visual lines, and any orphaned fragment (';') would
    # otherwise be counted as a door row. Everything at or below the
    # highest footer fragment is off limits.
    lines = _cluster_lines(upright_words)
    footer_top = None
    for ln in lines:
        text = " ".join(w["text"] for w in ln)
        if RE_FOOTER_FRAG.search(text) or text.startswith("Zeile"):
            top = ln[0]["top"]
            footer_top = top if footer_top is None else min(footer_top, top)

    doors, marks, row = [], [], 0
    for ln in lines:
        if header_top is not None and ln[0]["top"] <= header_top:
            continue
        if footer_top is not None and ln[0]["top"] >= footer_top - 2:
            continue
        full = " ".join(w["text"] for w in ln).strip()
        if not full or RE_FOOTER.search(full) or full.startswith("Zeile"):
            continue

        name_words, attr_words, x_words = [], [], []
        attr_left = min(anchors.values()) - 4 if anchors else grid_left
        for w in ln:
            if _x_center(w) >= grid_left:
                x_words.append(w)
            elif w["x0"] >= attr_left:
                attr_words.append(w)
            else:
                name_words.append(w)

        name = " ".join(w["text"] for w in name_words).strip()
        # A real door name contains a letter or digit. Test alphanumericity
        # Unicode-aware (covers Latin Extended-A like Č/Š/ž) instead of a raw
        # codepoint range — the old [À-ÿ] both accepted the × mark glyph
        # (U+00D7), which could turn a stray cross into a phantom door row,
        # and rejected those Extended-A letters.
        if name and not any(c.isalnum() for c in name):
            name = ""            # bare punctuation fragments are not doors
        if name:
            row += 1
            doors.append(MatrixDoor(
                row=row, name=name,
                room_number=attr_value("RN", attr_words),
                floor=attr_value("E", attr_words)))
        if row:
            for w in x_words:
                if w["text"].strip().lower() not in X_MARK_TOKENS:
                    continue
                c = _x_center(w)
                nearest = min(range(len(strip_centers)),
                              key=lambda i: abs(strip_centers[i] - c))
                if abs(strip_centers[nearest] - c) <= STRIP_GAP + 2:
                    marks.append((row, nearest))
    return doors, marks


# --- Whole-file parsing ------------------------------------------------------

def _page_footer(page_text: str):
    m = RE_FOOTER.search(page_text)
    if not m:
        return None
    z1, z2, s1, s2 = (int(g) for g in m.groups())
    return {"rows": (z1, z2), "cols": (s1, s2)}


def parse_matrix_pdf(path: str) -> MatrixResult:
    """Parse one Schließmatrix export (native or scanned) into a MatrixResult."""
    res = MatrixResult(source_file=os.path.basename(path))

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False)
            pages.append({
                "rotated": [w for w in words if not w["upright"]
                            and (w["x1"] - w["x0"]) <= MAX_ROTATED_WORD_WIDTH],
                "upright": [w for w in words if w["upright"]],
                "footer": _page_footer(page.extract_text() or ""),
            })

    # Decide the header-text orientation once for the whole file (one file
    # = one production pipeline), voting with cleanly-valid serials. A
    # single-column page can tie — a reversed serial may itself be
    # serial-shaped ('03U4345' ↔ '5434U30') — which page-local decisions
    # cannot break. Reversed wins ties: pdfplumber emits the bottom-to-top
    # rotated headers of this layout character-reversed even for native
    # text, so reversed is this format's normal case.
    score = {False: 0, True: 0}
    oriented = {}
    for i, pg in enumerate(pages):
        for flip in (False, True):
            persons, n = _extract_persons_oriented(pg["rotated"], flip)
            oriented[(i, flip)] = persons
            score[flip] += n
    use_flipped = score[True] >= score[False]

    col_cursor = 0          # highest global column number assigned so far
    row_cursor = 0          # highest global row number assigned so far
    known_cols: dict[int, MatrixPerson] = {}
    for page_no, pg in enumerate(pages, start=1):
        persons_raw = oriented[(page_no - 1, use_flipped)]
        upright, footer = pg["upright"], pg["footer"]

        page_col_start = footer["cols"][0] if footer else col_cursor + 1
        if footer:
            expected_here = footer["cols"][1] - footer["cols"][0] + 1
            if expected_here != len(persons_raw):
                res.warnings.append(
                    f"page {page_no}: footer states {expected_here} "
                    f"column(s) (Spalte {footer['cols'][0]}-"
                    f"{footer['cols'][1]}) but {len(persons_raw)} could "
                    f"be read from the text layer")

        page_persons = []
        for i, (serial, raw, valid, suspect, name, _c) in enumerate(
                persons_raw):
            asta, person = None, name
            m = RE_ASTA.match(name)
            if m:
                asta, person = int(m.group(1)), m.group(2).strip()
            p = MatrixPerson(
                column=page_col_start + i, serial=serial, raw_serial=raw,
                serial_valid=valid, serial_suspect=suspect,
                asta_number=asta, person_name=person)
            page_persons.append(p)
            # A matrix split by rows repeats its column headers on every
            # page (same Spalte range, new Zeile range); count each
            # physical column once.
            seen = known_cols.get(p.column)
            if seen is None:
                known_cols[p.column] = p
                res.persons.append(p)
            elif seen.serial != p.serial:
                res.warnings.append(
                    f"page {page_no}: column {p.column} reads serial "
                    f"{p.serial} but an earlier page read {seen.serial}")

        col_cursor = max(col_cursor, footer["cols"][1] if footer
                         else page_col_start + len(persons_raw) - 1)
        if footer:
            res.expected_columns = max(res.expected_columns or 0,
                                       footer["cols"][1])
            z1, z2 = footer["rows"]
            res.expected_rows = max(res.expected_rows or 0,
                                    z2 if z2 >= z1 else 0)

        # Door rows + X marks (absent on header-only pages, Zeile 1-0).
        # When a matrix is split across column pages the same door rows
        # repeat on each page, so global row numbers come from the
        # footer's Zeile range; rows already seen are not re-added.
        if upright and page_persons:
            page_row_start = footer["rows"][0] if footer else row_cursor + 1
            known_rows = {d.row: d for d in res.doors}
            centers = [c for *_x, c in persons_raw]
            doors, page_marks = _extract_doors(upright, centers)
            for d in doors:
                g = page_row_start + d.row - 1
                seen = known_rows.get(g)
                if seen is None:
                    d.row = g
                    res.doors.append(d)
                elif seen.name != d.name:
                    res.warnings.append(
                        f"page {page_no}: row {g} reads {d.name!r} but an "
                        f"earlier page read {seen.name!r}")
            for line_row, col_idx in page_marks:
                res.marks.add((page_persons[col_idx].column,
                               page_row_start + line_row - 1))
            row_cursor = max(row_cursor, page_row_start + len(doors) - 1)
        elif upright and not page_persons:
            # Door rows without readable column headers cannot be mapped
            # to transponders; say so instead of dropping them silently.
            leftover = RE_FOOTER.sub("", " ".join(w["text"] for w in upright))
            if len(re.findall(r"[0-9A-Za-z]", leftover)) > 12:
                res.warnings.append(
                    f"page {page_no}: text rows are present but no "
                    f"transponder columns could be read — door/mark "
                    f"extraction skipped for this page")

    res.ocr_scan = any(p.serial_suspect or not p.serial_valid
                       for p in res.persons)

    if (res.expected_columns is not None
            and res.expected_columns != len(res.persons)):
        res.warnings.append(
            f"matrix states {res.expected_columns} transponder column(s) "
            f"but {len(res.persons)} were read")
    if (res.expected_rows is not None
            and res.expected_rows != len(res.doors)):
        res.warnings.append(
            f"matrix states {res.expected_rows} door row(s) "
            f"but {len(res.doors)} were read")
    return res


# --- Format detection --------------------------------------------------------

def detect_format(path: str) -> str:
    """Classify a PDF as 'list' (per-transponder printout) or 'matrix'.

    List printouts always carry their title as native text, so that marker
    wins. Matrix exports are recognised by their axis labels or page
    footers — in a scanned file those may only exist character-reversed in
    the OCR layer, so both directions are checked.
    """
    with pdfplumber.open(path) as pdf:
        saw_matrix = False
        n_rotated_serials = 0
        for page in pdf.pages:
            text = page.extract_text() or ""
            if LIST_MARKER in text or ("Raumnummer" in text
                                       and "Seriennummer" in text):
                return "list"
            if (RE_FOOTER.search(text)
                    or "PERSONEN" in text or "NENOSREP" in text
                    or "SCHLIESSUNGEN" in text or "NEGNUSSEILHCS" in text):
                saw_matrix = True
            if not saw_matrix:
                for w in page.extract_words():
                    if w["upright"]:
                        continue
                    if any(repair_serial(t)[1]
                           for t in (w["text"], w["text"][::-1])):
                        n_rotated_serials += 1
        if saw_matrix or n_rotated_serials >= 5:
            return "matrix"
        return "list"
