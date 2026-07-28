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

## Quickstart (local development)

This project uses [`uv`](https://docs.astral.sh/uv/) and, optionally,
[`just`](https://just.systems) as a task runner. Production defaults are
**secure** (DEBUG off, login required, a real `SECRET_KEY` demanded), so local
work runs with `KEYMGMT_DEBUG=1` — `just` sets that for you.

```bash
just migrate          # or: KEYMGMT_DEBUG=1 uv run python manage.py migrate
just createuser       # create a login account
just dev              # → http://127.0.0.1:8000
```

Run `just` to see all recipes (`test`, `check`, `import`, `export-changes`,
`backup`, …). Without `just`, prefix any manage.py command with
`KEYMGMT_DEBUG=1 uv run python`.

Open http://127.0.0.1:8000, sign in, and drop your PDF printouts onto the upload
area. To bulk-import a folder instead of the browser:

```bash
just import /path/to/pdfs      # or: uv run python manage.py loadpdfs /path/to/pdfs
```

To deploy on your own server (NixOS module, gunicorn, nginx, secrets, backups),
see **[DEPLOY.md](DEPLOY.md)**.

Re-importing a transponder refreshes its data and access set, so uploading an
updated printout is safe and idempotent.

### Portable website backups

The **Daten** page exports all Schließmatrix domain data as one versioned JSON
file: doors, transponders, groups, metadata, and every current, planned, removed,
and desired authorization. Login accounts, passwords, sessions, and Django
internals are never included.

The same page imports a backup in either of two modes:

- **Zusammenführen** updates matching records from the backup and retains records
  that exist only on the current site.
- **Alle Daten ersetzen** validates the complete file and then atomically replaces
  the domain data so it exactly matches the backup. This requires an explicit
  confirmation.

This portable JSON workflow is separate from SimonsVoss printout imports, PDF
reports, and the deployment-level SQLite backups described in `DEPLOY.md`.

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
- **Transponders** — dashboard cards and the searchable full list show the same
  compact derived group label for each keycard's target programming; a detail
  page lists the doors it opens, grouped by location.
- **Locks** — every door/cylinder (searchable); a detail page lists which
  keycards open it.
- **Overlap** — the analytical view:
  - an interactive **similarity heatmap** (Jaccard overlap between every pair of
    keycards; tap a cell to see exactly which doors two keys share);
  - **Identical access** — keycards that open precisely the same set of doors;
  - **Access tiers** — doors grouped by the exact set of keys that open them.
- **Individuell** — every transponder's *individual* access: doors it holds that
  aren't provided by any group it belongs to (toggle current vs. planned state).
- **Soll** — the target ("Soll") editor: a Groups × Doors matrix and reusable
  door-groups; a per-transponder Soll editor distinguishes group-inherited
  (locked) from individual doors. Matrix, Soll/Ist, and reprogramming-worklist
  PDFs use the same flattened group label. Hollow-cross (`wird entfernt`) marks
  from a matrix show up as pending removals throughout.
- **Zugangsübersicht** — from the group list, export exact group combinations
  such as `AStA A` or `AStA LT`: each section lists inherited group doors before
  exact transponder serials. A final appendix lists additional individual Soll
  doors by transponder. The same report is available as copyable Markdown and an
  A4 PDF.
- **Daten** — download a portable, domain-only JSON backup or import one by
  merging it into the current state or replacing the state exactly.

## Authentication

The whole UI is gated behind a login (`LoginRequiredMiddleware`); there is no
Django admin exposed. Create an account with `just createuser` (or
`uv run python manage.py createsuperuser`) and sign in at `/accounts/login/`.
In production the NixOS module provisions the initial user for you — see
[DEPLOY.md](DEPLOY.md).

## Data model

- `Lock` — one physical cylinder, keyed by its SimonsVoss serial
  (door name, room, location, area).
- `Group` — one atomic reusable door set with a stable export code. Multiple
  groups assigned to a transponder are flattened to one display/export label;
  combined groups are not stored as separate records.
- `Transponder` — one keycard (serial, ASTA number, owner, print date) with a
  many-to-many link to the locks it opens.

## Notes

- **Styling** uses the Tailwind Play CDN and Alpine.js from a CDN, so the UI
  needs internet access to render its styles. For an offline or production
  deployment, build a static stylesheet with the Tailwind CLI
  (`npx tailwindcss -i in.css -o static/app.css --minify`) and replace the two
  CDN `<script>`/`<link>` tags in `access/templates/access/base.html`.
- The UI is in **German**. Settings are environment-driven with secure
  production defaults (DEBUG off, login required, hosts restricted, secret key
  demanded); local dev opts back in with `KEYMGMT_DEBUG=1`. See
  [DEPLOY.md](DEPLOY.md) for the full configuration and a hardened NixOS +
  nginx deployment.
- The parser and its standalone test suite
  (`parse_transponder_pdfs.py`, `test_parse_transponder_pdfs.py`) were delivered
  separately; `access/pdf_parser.py` is the same module.
