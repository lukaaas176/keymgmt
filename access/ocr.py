"""
ocr.py
======

Grid-guided OCR for Schließmatrix printout **images** (screenshots, photos,
scans without a text layer). Requires the `tesseract` binary; everything
else is PIL + stdlib.

Plain tesseract over the whole page fails on this material — the dense
grid rulings and the 90°-rotated column headers defeat its layout
analysis. Instead the matrix geometry is recovered first and each cell is
OCR'd in isolation:

1. **Column rulings** — person-column separators run the full page height
   (unlike the door-attribute sub-columns, which exist only below the
   header), so a min-over-bands projection finds exactly them.
2. **Header bands** — the axis-label column (the first column) carries
   NAME (PERSONEN) / PB / SN / EXPIRY in boxed bands; OCRing those labels
   yields the y-range of every band.
3. **Per-cell OCR** — each serial/name/door cell is cropped, near-full-
   width ruling lines are blanked (serials often sit *on* a ruling, which
   otherwise reads as garbage), the ink band nearest the cell centre is
   isolated (drops neighbour-cell bleed), and tesseract runs with several
   configs; the first read that repairs to a valid serial wins.
4. **X marks** — no OCR at all: each grid cell is classified from a 3×3
   ink-density downsample. A cross has an inked *centre* (its crossing
   point) plus strong corner-vs-edge-midpoint diagonal contrast; hatching
   is uniform; empty is white.

The cell vocabulary follows the SimonsVoss LSM "Doors/Persons view"
(LSM 3.5 manual §7.5, p.108). An authorisation is drawn as a cross in one
of three states — configured (thin), programmed (bold), or being removed
(grey) — optionally with a small corner triangle when it is inherited
from a group. All crosses count as authorizations here. Two look-alikes
must NOT count and are excluded by the centre-ink requirement: a corner
triangle *without* a cross (a withdrawn group authorisation) has an empty
centre, and a chequered/greyed-out box (a deactivated transponder or G2
transponder) is uniform hatch with no crossing point.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pdfplumber
from PIL import Image, ImageDraw, ImageOps

from .matrix_parser import (MAX_ROTATED_WORD_WIDTH, RE_ASTA, RE_FOOTER,
                            X_MARK_TOKENS, MatrixDoor, MatrixPerson,
                            MatrixResult, _cluster_lines, _cluster_strips,
                            _page_footer, repair_serial)

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")

# Native-matrix rendering (see parse_native_matrix): render each page, then
# read the vector-drawn X marks from pixels. A cell centre is sampled in a
# small box; anything with real ink is an authorization cross (the LSM plan
# uses bold ×=programmed and thin ×=configured — both count). Calibrated
# and validated against Lukas.pdf at 200 dpi.
NATIVE_DPI = 200
NATIVE_INK_LEVEL = 100
NATIVE_CELL_HALF_PT = 3.4        # half a cell, in PDF points
NATIVE_MARK_MIN = 0.06           # ink fraction at/above which a cell is marked
# The mark weight is strongly bimodal (measured on Lukas.pdf): a thin ×
# (configured, not yet programmed) fills ~0.2-0.3 of the cell, a bold ×
# (programmed) ~0.7-0.8, with a clear gap between. Above this fraction a
# cell is an active authorization, below it a planned one.
NATIVE_ACTIVE_MIN = 0.45
# A real authorization is a *solid* cross (bold or thin) whose strokes run
# through the cell centre. A hollow outline cross fills the same overall ink
# fraction as a thin solid one but leaves its centre white — it is a
# different, non-authorizing state (verified against ASTA-2026.csv: hollow
# crosses are never an 'x') and must be rejected. Only the centre separates
# the two, so sample a small central probe and require it to be inked.
NATIVE_CENTER_HALF_PT = 1.4      # half-size of the centre probe, in PDF points
NATIVE_CENTER_MIN = 0.30         # min centre ink for a solid (real) cross
# A door label may be trailed by the first door-property column (PB), a lone
# number in its own column well to the right of the name. A gap this wide (pt)
# between the last two words marks that number as PB — to be dropped — while a
# real numeric suffix ('Vorhangschloss 1', gap ~2pt) stays with the name.
NATIVE_PROP_GAP = 10
# The serial is the top-most band below NATIVE_HEADER_SPLIT. A vertical gap
# this wide (pt) below it marks a separate lower band (expiry / PB), which
# must not be glued onto the serial. The serial's own stacked words sit only
# a few points apart, so this only ever trims a genuinely separate band.
NATIVE_SN_GAP = 20
# Vertical split (PDF points from the page top) between a column header's
# name band and its serial band. The name is printed upward from a fixed
# "ASTA/A" anchor near top~165, so it never reaches this line however long
# it is; the serial sits below it (measured top~222 on A3/ASTA-2026,
# top~232 on A4/Lukas). Anything at or below this line is the serial band,
# which also keeps the PB/SN/ZB legend column (top~200-272) out of the name
# so that strip is dropped as "no name, no serial".
NATIVE_HEADER_SPLIT = 190

# Cell classification works on the original greyscale. Pixels darker than
# INK_LEVEL count as ink (structure); pixels darker than SOLID_LEVEL count
# as a solid stroke (a printed cross), as opposed to grey hatch texture;
# pixels darker than FAINT_LEVEL include the light-grey ink of a faint
# (transitional) cross.
INK_LEVEL = 128
SOLID_LEVEL = 64
FAINT_LEVEL = 200
# Ink fraction below which a grid cell is empty.
CELL_EMPTY = 0.12
# Corner+centre minus edge-midpoint density at/above which a cell is an X.
CELL_X_CONTRAST = 0.15
# A thin × has little total ink but still some diagonal structure.
CELL_THIN_X_INK = 0.35
CELL_THIN_X_CONTRAST = 0.05
# A cross always inks its centre (the crossing point). A corner triangle
# without a cross (a withdrawn group authorisation, manual §7.5) does not,
# so this gate keeps such cells out of the 'x' class. Measured across the
# sample: real crosses score >=0.59 here, hatch/empty far below.
CELL_X_CENTER = 0.45
# A printed cross is a solid stroke; hatching is thin grey lines. This is
# the fraction of near-black pixels required to call a cell a cross.
# Strongly bimodal on the sample: real crosses >=0.55, misread hatch <=0.09,
# with a wide empty gap — the threshold sits low in that gap so a genuinely
# thinner cross still clears it.
CELL_X_SOLID = 0.15
# A 'faint' cell has light-grey content but almost no dark ink — a cross in
# a transitional state (configured-not-programmed or removed-not-transmitted,
# manual §7.5). It is too ambiguous to count as access, so it is reported
# for manual review rather than scored as an authorization.
CELL_FAINT_MIN = 0.12   # grey content at/above this is not empty
CELL_FAINT_MAX_INK = 0.20   # dark-ink fraction below this is "not solid"

_SERIAL_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-"


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_SUFFIXES)


# --- native-PDF matrix (vector marks) ----------------------------------------

def _native_columns(page):
    """Person columns of one matrix page: (x_center_pt, serial, raw, valid,
    suspect, name), left to right. Serials are read from the SN band of the
    90°-rotated headers (stored character-reversed in this layout)."""
    rot = [w for w in page.extract_words()
           if not w["upright"] and (w["x1"] - w["x0"]) <= MAX_ROTATED_WORD_WIDTH]
    out = []
    for strip in _cluster_strips(rot):
        xc = sum((w["x0"] + w["x1"]) / 2 for w in strip) / len(strip)
        sn_words = sorted((w for w in strip if w["top"] >= NATIVE_HEADER_SPLIT),
                          key=lambda w: w["top"])
        sn = []
        for w in sn_words:
            if sn and w["top"] - sn[-1]["bottom"] > NATIVE_SN_GAP:
                break   # a lower band (expiry / PB) — not part of the serial
            sn.append(w)
        raw = "".join(w["text"][::-1] for w in sorted(sn, key=lambda w: -w["top"])).strip()
        name = " ".join(w["text"][::-1] for w in
                        sorted([w for w in strip if w["top"] < NATIVE_HEADER_SPLIT],
                               key=lambda w: -w["top"])).strip()
        serial, valid, suspect = repair_serial(raw) if raw else ("", False, False)
        # Skip the far-left axis-label strip: no name and no readable serial.
        if "PERSONEN" in name.upper() or (not name and not valid):
            continue
        out.append((xc, serial, raw, valid, suspect, name))
    return out


def _native_doors(page):
    """Door rows of one matrix page: (y_center_pt, name).

    The label band also catches the first door-property column (PB): a lone
    number sitting well right of the name. Drop that trailing number only
    when a wide gap separates it from the name, so a real numeric suffix
    ('Mensa Vorhangschloss 1', gap ~2pt) survives while the PB value
    ('... 40', in its own column) is removed."""
    up = [w for w in page.extract_words() if w["upright"]]
    out = []
    for ln in _cluster_lines(up):
        left = sorted((w for w in ln if w["x0"] < 230), key=lambda w: w["x0"])
        if not left:
            continue
        if (len(left) >= 2 and re.fullmatch(r"\d+", left[-1]["text"])
                and left[-1]["x0"] - left[-2]["x1"] > NATIVE_PROP_GAP):
            left = left[:-1]
        txt = " ".join(w["text"] for w in left).strip()
        if any(k in txt for k in ("NAME", "SCHLIESS", "Zeile", "Spalte",
                                  "SimonsVoss", "Technologies")):
            continue
        if re.fullmatch(r"[\d\s.]+", txt) or len(txt) < 3:
            continue
        out.append((sum(w["top"] for w in left) / len(left), txt))
    return out


def is_native_matrix_pdf(path: str) -> bool:
    """A native Schließmatrix draws its X marks as vector graphics, so its
    text layer has the rotated headers and door labels but *no* X tokens.
    True when the file is a matrix (Zeile/Spalte footer + serial-shaped
    rotated headers) whose marks are not in the text layer — as opposed to
    a text-mark matrix, which the plain text parser handles."""
    if is_image(path):
        return False
    with pdfplumber.open(path) as pdf:
        saw_matrix = False
        for page in pdf.pages:
            words = page.extract_words()
            if any(w["text"].strip().lower() in X_MARK_TOKENS
                   for w in words if w["upright"]):
                return False        # textual marks present -> text parser
            if _page_footer(page.extract_text() or "") and any(
                    v for _x, _s, _r, v, _su, _n in _native_columns(page)):
                saw_matrix = True
    return saw_matrix


def parse_native_matrix(path: str, *, include_removed: bool = False) -> MatrixResult:
    """Parse a native-PDF Schließmatrix whose marks are vector graphics.

    The text layer gives the geometry (rotated column headers, door-row
    labels, Zeile/Spalte page footers); the marks themselves are read by
    rendering each page and sampling ink at every cell centre. A matrix is
    tiled across pages by row-band × column-group — the column geometry is
    taken from the page that carries each Spalte range's headers, the door
    rows from the leftmost (label-bearing) page of each Zeile band, and
    every page's marks are sampled against that shared geometry.

    Validated against Lukas.pdf: the group templates reproduce the
    by-hand audit exactly (Muster Allgem 53 doors, Muster Umwelt 4).
    """
    res = MatrixResult(source_file=os.path.basename(path), ocr_scan=True)
    with pdfplumber.open(path) as pdf:
        info = [(pg, _page_footer(pg.extract_text() or "")) for pg in pdf.pages]

        colgeom = {}            # (s1,s2) -> columns, from its header page
        for pg, f in info:
            if f and f["cols"] not in colgeom:
                cols = _native_columns(pg)
                if cols:
                    colgeom[f["cols"]] = cols
        doorband = {}           # (z1,z2) -> (spalte_start, doors), leftmost page
        for pg, f in info:
            if not f:
                continue
            if f["rows"] in doorband and doorband[f["rows"]][0] <= f["cols"][0]:
                continue
            doorband[f["rows"]] = (f["cols"][0], _native_doors(pg))

        col_order = sorted(colgeom)
        row_order = sorted(doorband, key=lambda r: r[0])
        col_base, n = {}, 0
        for k in col_order:
            col_base[k] = n
            n += len(colgeom[k])
        row_base, n = {}, 0
        for k in row_order:
            row_base[k] = n
            n += len(doorband[k][1])

        for k in col_order:
            for i, (_x, ser, raw, valid, suspect, name) in enumerate(colgeom[k]):
                asta, person = None, name
                m = RE_ASTA.match(name)
                if m:
                    asta, person = int(m.group(1)), m.group(2).strip()
                res.persons.append(MatrixPerson(
                    column=col_base[k] + i + 1, serial=ser, raw_serial=raw,
                    serial_valid=valid, serial_suspect=suspect,
                    asta_number=asta, person_name=person))
        for k in row_order:
            for j, (_y, name) in enumerate(doorband[k][1]):
                res.doors.append(MatrixDoor(row=row_base[k] + j + 1, name=name))

        sc = NATIVE_DPI / 72.0
        half = int(NATIVE_CELL_HALF_PT * sc)
        chalf = max(1, int(NATIVE_CENTER_HALF_PT * sc))
        for pg, f in info:
            if not f or f["cols"] not in colgeom or f["rows"] not in doorband:
                continue
            im = pg.to_image(resolution=NATIVE_DPI).original.convert("L")
            W, H = im.size
            cols = colgeom[f["cols"]]
            drows = doorband[f["rows"]][1]
            for ci, (xc, *_r) in enumerate(cols):
                cx = int(xc * sc)
                if cx - half < 0 or cx + half > W:
                    continue
                for ri, (yc, _n) in enumerate(drows):
                    cy = int((yc + NATIVE_CELL_HALF_PT + 1.8) * sc)
                    if cy - half < 0 or cy + half > H:
                        continue
                    cell = im.crop((cx - half, cy - half, cx + half, cy + half))
                    px = cell.getdata()
                    frac = sum(1 for p in px if p < NATIVE_INK_LEVEL) / len(px)
                    if frac < NATIVE_MARK_MIN:
                        continue
                    # Reject hollow outline crosses (empty centre): a real
                    # cross, however thin, inks the cell centre where its
                    # strokes meet.
                    ccell = im.crop((cx - chalf, cy - chalf,
                                     cx + chalf, cy + chalf))
                    cpx = ccell.getdata()
                    cfrac = sum(1 for p in cpx if p < NATIVE_INK_LEVEL) / len(cpx)
                    key = (col_base[f["cols"]] + ci + 1,
                           row_base[f["rows"]] + ri + 1)
                    if cfrac < NATIVE_CENTER_MIN:
                        # Hollow outline cross: not a live authorisation. It is a
                        # door still programmed but withdrawn — pending removal.
                        # Dropped by default (keeps the CSV-validated active +
                        # planned counts); captured only when asked for.
                        if include_removed:
                            res.marks.add(key)
                            res.mark_states[key] = "remove"
                        continue
                    res.marks.add(key)
                    res.mark_states[key] = (
                        "active" if frac >= NATIVE_ACTIVE_MIN else "planned")

        # Coverage check: the footers declare the full Spalte/Zeile spans, so
        # the last column/row index is the expected total. Seeding these from
        # the footer (not from what was read) lets MatrixResult.consistent and
        # the warning below catch a silently dropped column or row — the same
        # safety net the text-mark parser has.
        exp_cols = max((k[1] for k in colgeom), default=0)
        exp_rows = max((k[1] for k in doorband), default=0)
        if exp_cols:
            res.expected_columns = exp_cols
        if exp_rows:
            res.expected_rows = exp_rows

    if res.expected_columns is None:
        res.expected_columns = len(res.persons)
    if res.expected_rows is None:
        res.expected_rows = len(res.doors)
    if res.expected_columns != len(res.persons):
        res.warnings.append(
            f"matrix footer states {res.expected_columns} column(s) but "
            f"{len(res.persons)} were read")
    if res.expected_rows != len(res.doors):
        res.warnings.append(
            f"matrix footer states {res.expected_rows} row(s) but "
            f"{len(res.doors)} were read")
    unreadable = [p.column for p in res.persons if not p.serial_valid]
    if unreadable:
        res.warnings.append(
            f"{len(unreadable)} column(s) have a serial truncated in the PDF "
            f"(e.g. '02UM6…') and can only be imported once resolved by hand")
    return res


# --- geometry ----------------------------------------------------------------

def _long_lines(bw, region, axis, nbands=24, thresh=140, min_gap=8):
    """Positions of ruled lines spanning a whole region axis.

    Works on a binarized image (ink=255). Averaging the region into
    `nbands` slices and taking the per-position minimum keeps only lines
    that are present in *every* slice — text and hatching drop out.
    """
    x0, y0, x1, y1 = (int(v) for v in region)
    crop = bw.crop((x0, y0, x1, y1))
    if axis == "v":
        small = crop.resize((x1 - x0, nbands), Image.Resampling.BOX)
        px = small.load()
        prof = [min(px[x, b] for b in range(nbands)) for x in range(x1 - x0)]
        base = x0
    else:
        small = crop.resize((nbands, y1 - y0), Image.Resampling.BOX)
        px = small.load()
        prof = [min(px[b, y] for b in range(nbands)) for y in range(y1 - y0)]
        base = y0
    lines, run = [], []
    for i, v in enumerate(prof):
        if v >= thresh:
            run.append(i)
        elif run:
            lines.append(base + sum(run) / len(run))
            run = []
    if run:
        lines.append(base + sum(run) / len(run))
    merged = []
    for p in lines:
        if merged and p - merged[-1] < min_gap:
            merged[-1] = (merged[-1] + p) / 2
        else:
            merged.append(p)
    return merged


def _isolate_center_band(img, line_frac=0.80):
    """Prepare a cell crop for OCR.

    Blanks near-full-width horizontal rulings (cell text may sit right on
    one), then keeps only the ink band closest to the vertical centre —
    text bleeding over from the neighbouring cell lies beyond the ruling
    and is dropped with it.
    """
    w, h = img.size
    if w < 4 or h < 4:
        return None
    ink = img.point(lambda p: 1 if p < 140 else 0)
    px = ink.load()
    rowink = [sum(px[x, y] for x in range(w)) / w for y in range(h)]
    draw = ImageDraw.Draw(img)
    for y, v in enumerate(rowink):
        if v >= line_frac:
            draw.rectangle([0, max(0, y - 1), w, y + 1], fill=255)
            rowink[y] = 0
    bands, cur = [], None
    for y, v in enumerate(rowink):
        if v > 0.02:
            cur = [y, y] if cur is None else [cur[0], y]
        elif cur:
            bands.append(cur)
            cur = None
    if cur:
        bands.append(cur)
    if not bands:
        return None
    y0, y1 = min(bands, key=lambda b: abs((b[0] + b[1]) / 2 - h / 2))
    band = img.crop((0, max(0, y0 - 3), w, min(h, y1 + 3)))
    bbox = band.point(lambda p: 255 if p < 140 else 0).getbbox()
    if bbox is None:
        return None
    bx0, by0, bx1, by1 = bbox
    return band.crop((max(0, bx0 - 4), max(0, by0 - 2),
                      min(w, bx1 + 4), min(band.height, by1 + 2)))


# --- OCR ---------------------------------------------------------------------

class _Ocr:
    """A tesseract runner bound to one scratch file."""

    def __init__(self):
        fd, self._path = tempfile.mkstemp(suffix=".png")
        os.close(fd)

    def close(self):
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def read(self, img, psm=7, scale=2, whitelist=None) -> str:
        if img is None:
            return ""
        img = img.resize((img.width * scale, img.height * scale),
                         Image.Resampling.LANCZOS)
        img = ImageOps.expand(img, border=24, fill=255)
        img.save(self._path)
        cmd = ["tesseract", self._path, "stdout", "--psm", str(psm)]
        if whitelist:
            cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
        out = subprocess.run(cmd, capture_output=True, text=True)
        return out.stdout.strip()

    def read_serial(self, img) -> tuple[str, str, bool, bool]:
        """Multi-config serial read: (serial, raw, valid, suspect).

        All configs run; the most frequent valid repair wins (ties prefer
        an unsuspicious one). Disagreement between valid reads marks the
        result suspect, so the import layer double-checks it against
        already-known serials.
        """
        reads = []
        for psm, scale in ((7, 3), (8, 4), (13, 3)):
            raw = self.read(img.copy(), psm=psm, scale=scale,
                            whitelist=_SERIAL_CHARS).replace(" ", "")
            raw = raw.strip("-") if not raw.startswith(("T-", "TC-")) else raw
            if raw:
                reads.append((raw, *repair_serial(raw)))
        valid = [r for r in reads if r[2]]
        if valid:
            counts = {}
            for raw, serial, _v, suspect in valid:
                counts.setdefault(serial, [0, suspect, raw])[0] += 1
            serial, (_n, suspect, raw) = max(
                counts.items(), key=lambda kv: (kv[1][0], not kv[1][1]))
            return serial, raw, True, suspect or len(counts) > 1
        if reads:
            return reads[0][1], reads[0][0], False, True
        return "", "", False, True

    def read_text(self, img) -> str:
        text = self.read(img, psm=7, scale=2)
        text = text.strip(" |¦!©®_.")
        text = re.sub(r"\s{2,}", " ", text).strip()
        # Box borders and hatching read as trailing symbol runs.
        return re.sub(r"[\s=\[\]{}|<>~«»°'\"^*_-]+$", "", text)


# --- cell classification -------------------------------------------------------

def _classify_cell(gray, box, inset=9) -> str:
    """'empty' | 'x' | 'faint' | 'hatch' for one grid cell.

    An 'x' (an authorisation cross, manual §7.5) must satisfy three gates
    that together separate it from the look-alikes that must not count —
    a bare corner triangle (withdrawn authorisation) and a chequered/
    greyed-out box (deactivated transponder):

    * inked centre — the cross's crossing point; a corner triangle lacks it;
    * diagonal dominance — corners+centre darker than edge-midpoints;
    * solid stroke — a real cross is solid black, whereas hatching is thin
      grey lines, so dense single-direction hatch (which can fake the first
      two gates) is rejected here.

    'faint' is a cell with light-grey content but almost no dark ink — a
    cross in a transitional (not-programmed / being-removed) state. It is
    reported for review, not counted as access.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    # Shrink the border inset when a fixed one would leave too little cell —
    # a narrow column / short row on a low-resolution scan would otherwise be
    # cropped to nothing and silently classified 'empty'. For normal-size
    # cells this is a no-op (eff == inset), so classifications are unchanged.
    eff = min(inset, max(2, (min(x1 - x0, y1 - y0) - 8) // 2))
    cell = gray.crop((x0 + eff, y0 + eff, x1 - eff, y1 - eff))
    if cell.width < 8 or cell.height < 8:
        return "empty"
    # Emptiness and solidity come from full-resolution pixel counts (robust
    # to thin strokes that a 3×3 average would wash out); spatial structure
    # (centre, diagonal dominance) comes from the 3×3 downsample.
    pixels = list(cell.getdata())
    n = len(pixels)
    grey_frac = sum(1 for p in pixels if p < FAINT_LEVEL) / n
    if grey_frac < CELL_FAINT_MIN:
        return "empty"
    ink_frac = sum(1 for p in pixels if p < INK_LEVEL) / n
    solid = sum(1 for p in pixels if p < SOLID_LEVEL) / n
    v = [1.0 if p < INK_LEVEL else 0.0
         for p in cell.resize((3, 3), Image.Resampling.BOX).getdata()]
    center = v[4]
    diag = (v[0] + v[2] + v[4] + v[6] + v[8]) / 5 \
        - (v[1] + v[3] + v[5] + v[7]) / 4
    if center >= CELL_X_CENTER and solid >= CELL_X_SOLID:
        if diag >= CELL_X_CONTRAST:
            return "x"
        if ink_frac < CELL_THIN_X_INK and diag >= CELL_THIN_X_CONTRAST:
            return "x"                 # thin × on white
    if ink_frac < CELL_FAINT_MAX_INK:
        # Light-grey content, no solid stroke and no dense hatch fill.
        return "faint"
    return "hatch"


# --- whole-image parsing --------------------------------------------------------

DOOR_ATTR_LABELS = {"PB", "RN", "ZB", "N", "E", "AM", "PIN", "IM"}


def parse_matrix_image(path: str) -> MatrixResult:
    """Parse one Schließmatrix image into a MatrixResult.

    Requires tesseract; raises RuntimeError when it is not installed.
    """
    if not tesseract_available():
        raise RuntimeError(
            "reading matrix images requires the 'tesseract' OCR binary "
            "(e.g. `brew install tesseract`)")

    res = MatrixResult(source_file=os.path.basename(path), ocr_scan=True)
    im = Image.open(path).convert("L")
    W, H = im.size
    bw = im.point(lambda p: 255 if p < 128 else 0)
    ocr = _Ocr()
    try:
        _parse_into(res, im, bw, W, H, ocr)
    finally:
        ocr.close()
    return res


def _parse_into(res, im, bw, W, H, ocr):
    vlines = _long_lines(bw, (0, 0, W, H), "v")
    if len(vlines) < 3:
        res.warnings.append("no matrix grid found in the image")
        return

    columns = [(vlines[i], vlines[i + 1]) for i in range(len(vlines) - 1)
               if 20 <= vlines[i + 1] - vlines[i] <= 200]

    # The axis-label column tells us where each header band lies. Search
    # the leftmost columns for one whose bands read SN / PB / EXPIRY. The
    # column's ruling scan covers the whole page, so only bands whose OCR
    # matches a known axis label are kept — everything below the header
    # is grid rows, which must not creep into the band map.
    axis_names = {"PB", "SN", "EXPIRY", "ZB", "AM", "IM", "PIN"}
    bands = None
    label_idx = None
    for idx, (x0, x1) in enumerate(columns[:6]):
        hl = _long_lines(bw, (x0 + 4, 0, x1 - 4, H), "h", nbands=8)
        cand = {}
        for y0, y1 in zip(hl[:9], hl[1:10]):
            if y1 - y0 < 24:
                continue
            crop = im.crop((int(x0) + 4, int(y0) + 3, int(x1) - 4, int(y1) - 3))
            label = ocr.read_text(
                _isolate_center_band(crop.transpose(Image.Transpose.ROTATE_270)))
            label = label.upper().replace(" ", "")
            if label in axis_names or label.startswith("NAME"):
                cand[label] = (y0, y1)
        if "SN" in cand and any(k.startswith("NAME") for k in cand):
            bands = cand
            label_idx = idx
            break
    if bands is None:
        res.warnings.append(
            "could not locate the NAME/SN axis labels — not a matrix image?")
        return

    sn_y0, sn_y1 = bands["SN"]
    name_key = next(k for k in bands if k.startswith("NAME"))
    nm_y0, nm_y1 = bands[name_key]
    grid_top = max(y1 for _, y1 in bands.values())

    # --- person columns -------------------------------------------------
    person_cols = []
    for x0, x1 in columns[label_idx + 1:]:
        cell = im.crop((int(x0), int(sn_y0) + 2, int(x1), int(sn_y1) - 2))
        cell = _isolate_center_band(cell.transpose(Image.Transpose.ROTATE_270))
        if cell is None:
            continue
        serial, raw, valid, suspect = ocr.read_serial(cell)
        if not raw:
            continue
        name_crop = im.crop((int(x0) + 2, int(nm_y0) + 2,
                             int(x1) - 2, int(nm_y1) - 2))
        name = ocr.read_text(_isolate_center_band(
            name_crop.transpose(Image.Transpose.ROTATE_270)))
        person_cols.append(((x0, x1), serial, raw, valid, suspect, name))

    for i, ((x0, x1), serial, raw, valid, suspect, name) in enumerate(
            person_cols, start=1):
        asta, person = None, name
        m = RE_ASTA.match(name)
        if m:
            asta, person = int(m.group(1)), m.group(2).strip()
        res.persons.append(MatrixPerson(
            column=i, serial=serial, raw_serial=raw, serial_valid=valid,
            serial_suspect=suspect, asta_number=asta, person_name=person))

    if not person_cols:
        res.warnings.append("no transponder columns could be read")
        return

    # --- door rows --------------------------------------------------------
    # The door labels live left of the first person column. Skip a thin
    # left frame, but never past the label column itself (a narrow left
    # margin would otherwise invert the crop box).
    label_left = columns[label_idx][0]
    left_margin = max(0, min(70, int(label_left) - 40))
    hrows = _long_lines(bw, (left_margin, grid_top, label_left, H), "h",
                        nbands=16)
    row_bounds = [(a, b) for a, b in zip(hrows, hrows[1:]) if b - a > 20]
    if not row_bounds:
        res.warnings.append("no door rows found below the header")
        return

    subv = _long_lines(bw, (0, grid_top, label_left, H), "v", nbands=8,
                       thresh=120)
    subcells = [(a, b) for a, b in zip(subv, subv[1:]) if b - a > 12]

    # Header row maps the attribute sub-columns (RN = room, E = floor).
    attr_at = {}
    name_cell = max(subcells, key=lambda c: c[1] - c[0]) if subcells else None
    hy0, hy1 = row_bounds[0]
    for cx0, cx1 in subcells:
        label = ocr.read_text(_isolate_center_band(
            im.crop((int(cx0) + 4, int(hy0) + 2, int(cx1) - 4, int(hy1) - 2))))
        if label.upper() in DOOR_ATTR_LABELS:
            attr_at[label.upper()] = (cx0, cx1)

    def row_cell_text(bounds, r0, r1):
        if bounds is None:
            return ""
        crop = im.crop((int(bounds[0]) + 5, int(r0) + 2,
                        int(bounds[1]) - 5, int(r1) - 2))
        return ocr.read_text(_isolate_center_band(crop))

    row_no = 0
    row_geo = []
    for r0, r1 in row_bounds[1:]:
        name = row_cell_text(name_cell, r0, r1)
        name = re.sub(r"^[\W_]+|[\W_]+$", "", name)
        if not name or not re.search(r"[0-9A-Za-zÀ-ÿ]", name):
            continue
        row_no += 1
        room = re.sub(r"[|()\[\]]", "",
                      row_cell_text(attr_at.get("RN"), r0, r1)).strip()
        floor = re.sub(r"[|()\[\]]", "",
                       row_cell_text(attr_at.get("E"), r0, r1)).strip()
        floor = floor.replace("0G", "OG")     # floors are '1.OG', not '1.0G'
        res.doors.append(MatrixDoor(row=row_no, name=name,
                                    room_number=room, floor=floor))
        row_geo.append((row_no, r0, r1))

    # --- marks ------------------------------------------------------------
    hatched_cols = []
    faint = []          # (col, row) of transitional / ambiguous marks
    for col_i, ((x0, x1), *_rest) in enumerate(person_cols, start=1):
        n_hatch = 0
        for row_no, r0, r1 in row_geo:
            cls = _classify_cell(im, (x0, r0, x1, r1))
            if cls == "x":
                res.marks.add((col_i, row_no))
            elif cls == "faint":
                faint.append((col_i, row_no))
            elif cls == "hatch":
                n_hatch += 1
        if row_geo and n_hatch >= 0.8 * len(row_geo):
            hatched_cols.append(col_i)
    if hatched_cols:
        names = ", ".join(
            (res.persons[c - 1].person_name or res.persons[c - 1].serial)
            for c in hatched_cols)
        res.warnings.append(
            f"{len(hatched_cols)} column(s) are greyed out (deactivated or "
            f"expired transponders): {names}; hatching was not counted as "
            f"an authorization")
    if faint:
        door = {r: n for r, _a, _b in row_geo
                for n in [next((d.name for d in res.doors if d.row == r), r)]}
        detail = "; ".join(
            f"{res.persons[c - 1].person_name or res.persons[c - 1].serial} × "
            f"{door.get(r, r)}" for c, r in faint[:8])
        more = "" if len(faint) <= 8 else f" (+{len(faint) - 8} more)"
        res.warnings.append(
            f"{len(faint)} cell(s) hold a faint/partial mark (an "
            f"authorization being added or removed, not yet programmed); "
            f"these were NOT counted — review by eye: {detail}{more}")

    # --- footer (often cropped off screenshots) ----------------------------
    # An image is a single matrix slice, so the footer's Zeile/Spalte give
    # this slice's extent (e.g. 'Spalte 27-80' = 54 columns), not the whole
    # matrix. expected_* therefore hold the slice size to check against what
    # was actually read — using the end index would wrongly flag a perfect
    # mid-matrix crop as inconsistent.
    tail = im.crop((0, int(row_bounds[-1][1]), min(W, 1600), H))
    m = RE_FOOTER.search(ocr.read(_isolate_center_band(tail), psm=7, scale=2)
                         or "")
    if m:
        z1, z2, s1, s2 = (int(g) for g in m.groups())
        res.expected_rows = (z2 - z1 + 1) if z2 >= z1 else 0
        res.expected_columns = s2 - s1 + 1
        if res.expected_columns != len(res.persons):
            res.warnings.append(
                f"footer states {res.expected_columns} column(s) "
                f"(Spalte {s1}-{s2}) but {len(res.persons)} were read")
        if res.expected_rows and res.expected_rows != len(res.doors):
            res.warnings.append(
                f"footer states {res.expected_rows} door row(s) "
                f"(Zeile {z1}-{z2}) but {len(res.doors)} were read")
    else:
        res.warnings.append(
            "no 'Zeile/Spalte' footer found in the image — column and row "
            "counts could not be cross-checked")
