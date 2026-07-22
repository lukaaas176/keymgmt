# Schließmatrix

A small Django tool that turns SimonsVoss PDF printouts into a browsable,
persistent access database — and shows where keycards share access.

Two printout formats are understood and auto-detected on upload:

- **List** — "Berechtigungen für den Transponder": one PDF per keycard listing
  every door it opens (parsed by `access/pdf_parser.py`).
- **Matrix** — "Schließmatrix": one grid for many keycards, transponders as
  rotated column headers, doors as rows, X marks as authorizations (parsed by
  `access/matrix_parser.py`). Works for native PDF exports *and* scanned
  printouts, as long as the scanner embedded an OCR text layer (most office
  copiers do). When a native matrix draws its X marks as **vector graphics**
  (so the text layer has the grid but no marks), the file is rendered page by
  page and the marks are read from pixels instead — `access/ocr.py`,
  `parse_native_matrix`; no tesseract needed for this path. Bold × counts as a
  programmed authorization, thin × as a pending grant.
- **Matrix images** — screenshots/photos/scans *without* a text layer
  (png/jpg/tiff) are parsed by `access/ocr.py` when the
  [tesseract](https://github.com/tesseract-ocr/tesseract) binary is installed
  (`brew install tesseract`; adding `tesseract-lang` improves German door
  names). The grid geometry is detected first, every cell is OCR'd in
  isolation, and X marks are classified from pixel patterns — hatched
  (greyed-out) columns are deactivated/expired transponders and are *not*
  counted as authorizations. Door rows are matched to existing locks by
  name, tolerating small OCR errors.

Everything is stored in a SQLite file (`db.sqlite3`) that persists across
restarts.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000 and drop your PDF printouts onto the upload area.

To bulk-import a folder instead of using the browser:

```bash
python manage.py loadpdfs /path/to/pdfs
```

Re-importing a transponder refreshes its data and access set, so uploading an
updated printout is safe and idempotent.

### How matrix imports behave

Matrix printouts — especially scanned ones — are a lossier source than the
per-transponder list printouts, so they are imported conservatively:

- They **fill in** missing data (new keycards, missing owner names and ASTA
  numbers) but never overwrite what a list printout established.
- Door rows carry no lock serial in this format, so doors are matched to
  existing locks **by door name**; unmatched doors get a synthetic `MX:…`
  serial. X marks only ever *add* authorizations.
- Scanner OCR confuses lookalike glyphs (`O↔0`, `S↔5/8`, `T↔7`, `A↔4`…). The
  importer repairs what the serial alphabet makes unambiguous, and resolves
  the rest against serials already in the database (reported in the upload
  toast, e.g. `02UKSKC→02UK9KC`). Serials that stay unreadable are skipped
  and reported rather than imported wrong.
- Every page footer (`Zeile 1-40 ; Spalte 27-80`) is checked against what was
  actually read; shortfalls (e.g. a column the scanner's OCR skipped) show up
  as warnings — treat those as "verify this one by eye".

## Views

- **Overview** — counts, upload, and the list of imported keycards.
- **Transponders** — every keycard (searchable); a detail page lists the doors
  it opens, grouped by location.
- **Locks** — every door/cylinder (searchable); a detail page lists which
  keycards open it.
- **Overlap** — the analytical view:
  - an interactive **similarity heatmap** (Jaccard overlap between every pair of
    keycards; tap a cell to see exactly which doors two keys share);
  - **Identical access** — keycards that open precisely the same set of doors;
  - **Access tiers** — doors grouped by the exact set of keys that open them.

## Admin (optional)

```bash
python manage.py createsuperuser
```

Then visit `/admin/` to edit locks and transponders directly.

## Data model

- `Lock` — one physical cylinder, keyed by its SimonsVoss serial
  (door name, room, location, area).
- `Transponder` — one keycard (serial, ASTA number, owner, print date) with a
  many-to-many link to the locks it opens.

## Notes

- **Styling** uses the Tailwind Play CDN and Alpine.js from a CDN, so the UI
  needs internet access to render its styles. For an offline or production
  deployment, build a static stylesheet with the Tailwind CLI
  (`npx tailwindcss -i in.css -o static/app.css --minify`) and replace the two
  CDN `<script>`/`<link>` tags in `access/templates/access/base.html`.
- `DEBUG=True` and `ALLOWED_HOSTS=["*"]` are set for easy local use. Set a real
  `SECRET_KEY`, turn off debug, and restrict hosts before exposing it.
- The parser and its standalone test suite
  (`parse_transponder_pdfs.py`, `test_parse_transponder_pdfs.py`) were delivered
  separately; `access/pdf_parser.py` is the same module.
