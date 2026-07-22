# Schließmatrix — project summary

A Django tool that turns SimonsVoss access-control printouts into a browsable,
persistent database and shows where keycards share access. It ingests **three**
printout formats, all auto-detected on upload, and stores everything in a
SQLite file that survives restarts.

---

## Input formats

| Format | Looks like | Parser | Source quality |
|---|---|---|---|
| **List** | "Berechtigungen für den Transponder" — one PDF per keycard, one row per door it opens (keyed by lock serial) | `access/pdf_parser.py` | richest; authoritative |
| **Matrix PDF** | "Schließmatrix" — one grid, transponders as rotated column headers, doors as rows, X = authorization | `access/matrix_parser.py` | native export **or** scanned copy with an OCR text layer |
| **Matrix image** | a screenshot / photo / scan of a Schließmatrix with **no** text layer (png, jpg, tiff, bmp, webp) | `access/ocr.py` (needs the `tesseract` binary) | lossiest; conservative import |

`services.import_pdf(path, name)` detects the format and dispatches. All three
are idempotent — re-uploading the same file is safe.

---

## How each format is read

### List printouts (`pdf_parser.py`)
Reconstructs the table from word coordinates (not the linear text stream, which
mangles wrapped cells), carries `Schließanlage:` / `Standort.Gebäude.Etage:`
state across page breaks, and validates the row count against the printout's own
stated `Anzahl der Datensätze`.

### Matrix PDFs (`matrix_parser.py`)
The rotated column headers extract **character-reversed** in this layout (both
in native PDFs and in scanner OCR layers), so orientation is decided once per
file by voting on how many serials read cleanly each way. Serials are repaired
against the known alphabet (no `O`/`I` — those are always `0`/`1`) plus a
confusion table derived from real scans; anything beyond a trivial fix is
flagged **suspect** and resolved against serials already in the database. Page
footers (`Zeile a-b ; Spalte c-d`) validate coverage and drive multi-page
column/row assembly.

### Matrix images (`ocr.py`) — the format added last
Plain tesseract fails on this material (dense grid rulings + rotated headers
defeat its layout analysis), so the geometry is recovered first and each cell is
OCR'd in isolation:

1. **Column rulings** — person-column separators span the full page height, so a
   min-over-bands projection isolates exactly them.
2. **Header bands** — the axis-label column (NAME / PB / SN / EXPIRY) gives the
   y-range of the serial and name rows.
3. **Per-cell OCR** — each serial/name/door cell is cropped, ruling lines are
   blanked, the ink band nearest the cell centre is kept (drops neighbour
   bleed), and tesseract runs several configs; the best-voted valid serial wins.
4. **X marks** — no OCR at all: each grid cell is classified from its pixels
   (see below).

Door rows carry no lock serial in the matrix, so they are matched to existing
locks **by name** (`match_lock_by_name`), tolerating a couple of OCR glyph
errors — but never across a **digit** difference, so `Herd 1.OG` can never merge
onto `Herd 2.OG`. Unmatched doors get a synthetic `MX:…` serial.

---

## The cell vocabulary (grounded in the manual)

The X-mark classifier follows the SimonsVoss LSM manual §7.5 "Matrix →
Doors/Persons view" (p.108), which documents every symbol:

- a **cross** — thin (configured), bold (programmed), or grey (being removed),
  optionally with a corner **triangle** when inherited from a group — **counts
  as an authorization**;
- a corner **triangle with no cross** (a *withdrawn* group authorization) — does
  **not** count;
- a **chequered / greyed-out box** (a deactivated transponder or G2 card at the
  cylinder) — does **not** count.

`_classify_cell` returns `empty` / `x` / `faint` / `hatch` and gates an `x` on
three measured, strongly-bimodal features:

- **inked centre** — the cross's crossing point (a bare triangle lacks it);
- **diagonal dominance** — corners+centre darker than the edge-midpoints;
- **solid stroke** — a real cross is solid black, whereas hatching is grey
  texture. This last gate is what a visual audit proved necessary (see below).

A **faint** cell (a light-grey cross in a transitional state) is *reported for
manual review, never counted* — the tool refuses to guess on ambiguous marks.
Fully-hatched columns are reported as deactivated transponders.

---

## Verification & the bug the audit caught

Every OCR run was checked two ways: an independent by-eye transcription of the
serials (differential testing), and a **visual audit of all parsed X-marks**
rendered back over the source image.

That audit caught a real defect: **8 cells of dense single-direction hatching
were being miscounted as authorizations** (the "/" hatch faked the centre and
diagonal signals). Measuring the near-black pixel fraction showed a clean split
— real crosses ≥ 0.55, misread hatch ≤ 0.09, with a wide empty gap — so the
solid-stroke gate was added. Marks dropped 808 → **800**, and the full-matrix
overlay now shows every counted mark on a solid X and every greyed cell
correctly skipped.

Adversarial code review additionally fixed: door-name merges across a digit
difference; a crash on narrow left margins; footer slice-count semantics
(a mid-matrix crop is no longer flagged inconsistent); and case-insensitive
image globbing in `loadpdfs`.

---

## Web UI (`access/views.py`, `templates/`)

- **Overview** — counts, drag-and-drop upload, keycard grid.
- **Transponders** — searchable list → detail (doors grouped by location).
- **Locks** — searchable list → detail (which keycards open it).
- **Overlap** — Jaccard similarity heatmap between every keycard pair, exact
  "identical access" clone groups, and access tiers (doors grouped by their
  exact holder set).

---

## Current data

Live DB (`db.sqlite3`): **109 transponders, 220 locks, 1227 authorizations**,
built from the 12 original list printouts, `scan.pdf` (an 81-column ASTA matrix
scan), and `screenshot.png` (a 53-column StudiTUM matrix image → 53 transponders
/ 31 doors / 800 authorizations). Pre-import backups: `db.sqlite3.bak-2026-07-15`
(before scan.pdf) and `db.sqlite3.bak-2026-07-15b` (before screenshot).

A handful of serials were corrected by eye where OCR produced a valid-but-wrong
glyph the software cannot catch (e.g. `01X5TES→01X5TE5`); these are noted in the
project memory.

---

## Running it

```bash
uv venv && uv pip install -r requirements.txt
uv run python manage.py migrate
uv run python manage.py runserver            # then open http://127.0.0.1:8000
uv run python manage.py loadpdfs /path/to/dir  # bulk import PDFs and images
uv run python manage.py test access            # 57 tests
```

Importing a matrix **image** needs `tesseract` on the PATH
(`brew install tesseract`; add `tesseract-lang` for better German door names).
PDFs need nothing beyond the Python deps.

---

## Known limitations

- Image OCR counts **solid, programmed** crosses. Faint/transitional marks and
  greyed-out (deactivated) columns are surfaced as import warnings for manual
  review rather than counted.
- OCR can still produce a valid-but-wrong serial for an unlucky glyph pair
  (`T`/`7`, `S`/`5`); the importer mitigates this by matching against known
  serials and reporting suspects, but a genuinely new mis-read card needs an eye.
- A matrix image is treated as a single slice; a door name mangled beyond a
  couple of glyphs creates a new `MX:` lock instead of matching an existing one
  (safe by design — a miss duplicates, a wrong match would corrupt).
- Local-use posture unchanged: `DEBUG=True`, `ALLOWED_HOSTS=["*"]`, styling via
  CDN. Not production-hardened.
