#!/usr/bin/env python3
"""
parse_transponder_pdfs.py
=========================

Parse SimonsVoss / TU München "Berechtigungen für den Transponder" PDF
printouts and emit a single SQL file.

Each PDF lists every door (lock cylinder) that one transponder may open.
The script reconstructs the table from word coordinates rather than the raw
text stream, so it is robust to the wrapped multi-line cells these Crystal
Reports printouts produce: any column (a long door name, a room number, or
a Bereich label) may spill onto one or more following lines.

It validates every file against the "Anzahl der Datensätze" (record count)
the printout states about itself; a mismatch is reported on stderr, which
makes future layout changes easy to notice.

Data model (re-runnable / idempotent; works on PostgreSQL and SQLite):

    transponders   one row per transponder        (PK: serial)
    locks          one row per physical    (PK: serial == Seriennummer)
                   lock cylinder
    authorizations transponders <-> locks  (PK: transponder_serial, lock_serial)

Usage
-----
    python parse_transponder_pdfs.py PATH [PATH ...] [-o transponders.sql]

PATH may be a PDF file, a glob, or a directory (scanned for *.pdf).

    python parse_transponder_pdfs.py ./prints -o out.sql
    python parse_transponder_pdfs.py *.pdf
    python parse_transponder_pdfs.py a.pdf b.pdf --no-ddl       # inserts only
    python parse_transponder_pdfs.py *.pdf --drop               # DROP TABLE first

Requires: pdfplumber  (pip install pdfplumber)
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import re
import sys
from dataclasses import dataclass, field

import pdfplumber

# --- Labels and patterns that appear verbatim in the printouts -------------

LABEL_LOCK = "Schließanlage:"            # locking system grouping header
LABEL_LOC = "Standort.Gebäude.Etage:"   # location grouping header (Site.Building.Floor)
HDR_TOKENS = {"Raumnummer", "Seriennummer", "Bereich"}  # column-header row markers

# "ASTA 51 Justus, Rossmeier / 02UA77F"  ->  ('51', 'Justus, Rossmeier', '02UA77F')
# "Hessler, Carla / 01XEUPT"             ->  (None, 'Hessler, Carla', '01XEUPT')
# "ASTA 67 / 010A0SC"                    ->  ('67', '', '010A0SC')
RE_TRANSPONDER = re.compile(r"^(?:ASTA\s+(\d+))?\s*(.*?)\s*/\s*(\S+)\s*$")
RE_COUNT = re.compile(r"Anzahl der Datensätze:\s*(\d+)")
RE_PRINTED = re.compile(r"Ausdruck vom:\s*(\d{2}\.\d{2}\.\d{4})")


# --- Parsed structures -----------------------------------------------------

@dataclass
class Authorization:
    """One door a transponder may open (a lock cylinder + its placement)."""
    door_name: str
    room_number: str
    lock_serial: str        # SimonsVoss serial, unique per physical lock
    area: str               # Bereich
    location: str           # Standort.Gebäude.Etage
    locking_system: str     # Schließanlage


@dataclass
class Transponder:
    serial: str                         # e.g. '010A0SC' (the transponder)
    asta_number: int | None = None
    person_name: str | None = None
    locking_system: str | None = None
    printed_on: str | None = None       # ISO date string
    record_count: int | None = None     # stated "Anzahl der Datensätze"
    source_file: str = ""
    authorizations: list[Authorization] = field(default_factory=list)


# --- Geometry helpers ------------------------------------------------------

def _cluster_lines(words, tol: float = 3.0):
    """Group extracted words into visual lines by their vertical position."""
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


def _column_classifier(anchors):
    """
    Build a function mapping a word's left edge (x0) to a column index
    0=Tür, 1=Raumnummer, 2=Seriennummer, 3=Bereich.

    `anchors` are the left edges of the four header words. A generous 25pt
    margin tolerates minor alignment jitter while staying clear of the gaps
    between columns (the printout leaves >50pt between adjacent columns).
    """
    tur, raum, ser, ber = anchors

    def col_of(x0: float) -> int:
        if x0 >= ber - 25:
            return 3
        if x0 >= ser - 25:
            return 2
        if x0 >= raum - 25:
            return 1
        return 0

    return col_of


def _normalize_location(loc: str) -> str | None:
    """The root group is printed as '..'; treat dots-only as no location."""
    loc = (loc or "").strip()
    if not loc or set(loc.replace(" ", "")) <= {"."}:
        return None
    return loc


def _to_iso_date(german: str | None) -> str | None:
    if not german:
        return None
    try:
        return dt.datetime.strptime(german, "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


# --- PDF parsing -----------------------------------------------------------

def parse_pdf(path: str) -> Transponder:
    """Parse one printout into a Transponder with its authorizations."""
    fname = os.path.basename(path)
    tp = Transponder(serial="", source_file=fname)

    cur_lock: str | None = None        # current Schließanlage
    cur_loc: str | None = None         # current Standort.Gebäude.Etage
    cur_row: Authorization | None = None  # last primary row (for line wraps)
    full_text_parts: list[str] = []

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            full_text_parts.append(page.extract_text() or "")
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            lines = _cluster_lines(words)

            # Locate the column-header row on this page; everything above it is
            # the address block / title (and, on page 1, the transponder id).
            col_idx = None
            anchors = None
            for i, ln in enumerate(lines):
                texts = {w["text"] for w in ln}
                if HDR_TOKENS <= texts:
                    pos = {w["text"]: w["x0"] for w in ln}
                    anchors = (pos.get("Tür", 48.0),
                               pos["Raumnummer"], pos["Seriennummer"], pos["Bereich"])
                    col_idx = i
                    break
            if col_idx is None:
                continue  # not a table page (shouldn't happen for these files)

            # Transponder id line (page 1 only): the line after "Berechtigungen ...".
            if not tp.serial:
                for i, ln in enumerate(lines[:col_idx]):
                    if any(w["text"] == "Berechtigungen" for w in ln) and i + 1 < col_idx:
                        raw = " ".join(w["text"] for w in lines[i + 1]).strip()
                        _apply_transponder_header(tp, raw, fname)
                        break

            col_of = _column_classifier(anchors)

            # Walk the table body.
            for ln in lines[col_idx + 1:]:
                full = " ".join(w["text"] for w in ln).strip()

                if full.startswith("Anzahl der Datensätze") or full.startswith("Ausdruck vom"):
                    cur_row = None
                    continue
                if full.startswith(LABEL_LOCK):
                    cur_lock = full[len(LABEL_LOCK):].strip() or None
                    tp.locking_system = cur_lock
                    cur_row = None
                    continue
                if full.startswith(LABEL_LOC):
                    cur_loc = _normalize_location(full[len(LABEL_LOC):])
                    cur_row = None
                    continue

                # Distribute the line's words across the four columns.
                cols = ["", "", "", ""]
                for w in ln:
                    c = col_of(w["x0"])
                    cols[c] = (cols[c] + " " + w["text"]).strip()
                door, room, serial, area = cols

                if serial:
                    # A row is "complete" iff it carries a Seriennummer.
                    cur_row = Authorization(
                        door_name=door, room_number=room, lock_serial=serial,
                        area=area, location=cur_loc, locking_system=cur_lock,
                    )
                    tp.authorizations.append(cur_row)
                elif cur_row is not None:
                    # Continuation line: a wrapped cell from the row above.
                    # Any text column (door / room / area) may overflow here.
                    if door:
                        cur_row.door_name = (cur_row.door_name + " " + door).strip()
                    if room:
                        cur_row.room_number = (cur_row.room_number + " " + room).strip()
                    if area:
                        cur_row.area = (cur_row.area + " " + area).strip()

    full_text = "\n".join(full_text_parts)
    m = RE_COUNT.search(full_text)
    if m:
        tp.record_count = int(m.group(1))
    m = RE_PRINTED.search(full_text)
    if m:
        tp.printed_on = _to_iso_date(m.group(1))

    if not tp.serial:
        # Fall back to filename stem if the id line could not be read.
        tp.serial = os.path.splitext(fname)[0].upper()
        print(f"  ! {fname}: could not read transponder id; "
              f"using fallback serial '{tp.serial}'", file=sys.stderr)

    # Self-check against the printout's own record count.
    if tp.record_count is not None and tp.record_count != len(tp.authorizations):
        print(f"  ! {fname}: parsed {len(tp.authorizations)} rows but printout "
              f"states {tp.record_count}", file=sys.stderr)

    return tp


def _apply_transponder_header(tp: Transponder, raw: str, fname: str) -> None:
    m = RE_TRANSPONDER.match(raw)
    if not m:
        print(f"  ! {fname}: unrecognised transponder header {raw!r}", file=sys.stderr)
        return
    asta, name, serial = m.groups()
    tp.serial = serial
    tp.asta_number = int(asta) if asta else None
    tp.person_name = name.strip() or None


# --- SQL emission ----------------------------------------------------------

def sql_str(value) -> str:
    """Render a Python value as a SQL literal (NULL / number / 'escaped')."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# Schema for the STANDALONE CLI (`python -m access.pdf_parser … -o out.sql`).
# This is intentionally self-contained and NOT the Django app's schema: the
# app stores the same data in access_transponder / access_lock and the M2M
# through tables (and also tracks planned_locks). Do not load this script into
# the app's db.sqlite3 — it would create parallel, app-invisible tables. Use
# the ORM path (services.import_pdf) to populate the app database.
DDL = """\
CREATE TABLE IF NOT EXISTS transponders (
    serial          TEXT PRIMARY KEY,
    asta_number     INTEGER,
    person_name     TEXT,
    locking_system  TEXT,
    printed_on      DATE,
    record_count    INTEGER,
    source_file     TEXT
);

CREATE TABLE IF NOT EXISTS locks (
    serial       TEXT PRIMARY KEY,   -- SimonsVoss Seriennummer (unique per lock)
    door_name    TEXT,
    room_number  TEXT,
    location     TEXT,               -- Standort.Gebäude.Etage
    area         TEXT                -- Bereich
);

CREATE TABLE IF NOT EXISTS authorizations (
    transponder_serial TEXT NOT NULL REFERENCES transponders(serial) ON DELETE CASCADE,
    lock_serial        TEXT NOT NULL REFERENCES locks(serial)        ON DELETE CASCADE,
    PRIMARY KEY (transponder_serial, lock_serial)
);
"""

DROP = "DROP TABLE IF EXISTS authorizations;\n" \
       "DROP TABLE IF EXISTS locks;\n" \
       "DROP TABLE IF EXISTS transponders;\n"


def build_sql(transponders: list[Transponder], *, ddl: bool = True,
              drop: bool = False) -> str:
    """Aggregate parsed transponders into one idempotent SQL script."""
    # De-duplicate shared entities across all files.
    tp_rows: dict[str, Transponder] = {}
    lock_rows: dict[str, Authorization] = {}
    auth_pairs: set[tuple[str, str]] = set()

    for tp in transponders:
        if tp.serial in tp_rows:
            print(f"  ! duplicate transponder serial {tp.serial} "
                  f"({tp.source_file}); keeping latest", file=sys.stderr)
        tp_rows[tp.serial] = tp
        for a in tp.authorizations:
            lock_rows[a.lock_serial] = a            # last placement wins
            auth_pairs.add((tp.serial, a.lock_serial))

    out: list[str] = []
    out.append("-- Transponder authorizations exported from SimonsVoss PDF printouts")
    out.append(f"-- Generated: {dt.datetime.now().isoformat(timespec='seconds')}")
    out.append(f"-- Transponders: {len(tp_rows)}  Locks: {len(lock_rows)}  "
               f"Authorizations: {len(auth_pairs)}")
    out.append("")
    if drop:
        out.append(DROP)
    if ddl:
        out.append(DDL)
    out.append("BEGIN;")
    out.append("")

    out.append("-- Transponders ---------------------------------------------------------")
    for s in sorted(tp_rows):
        tp = tp_rows[s]
        out.append(
            "INSERT INTO transponders "
            "(serial, asta_number, person_name, locking_system, printed_on, "
            "record_count, source_file) VALUES ("
            f"{sql_str(tp.serial)}, {sql_str(tp.asta_number)}, "
            f"{sql_str(tp.person_name)}, {sql_str(tp.locking_system)}, "
            f"{sql_str(tp.printed_on)}, {sql_str(tp.record_count)}, "
            f"{sql_str(tp.source_file)})\n"
            "ON CONFLICT (serial) DO UPDATE SET "
            "asta_number=excluded.asta_number, person_name=excluded.person_name, "
            "locking_system=excluded.locking_system, printed_on=excluded.printed_on, "
            "record_count=excluded.record_count, source_file=excluded.source_file;"
        )
    out.append("")

    out.append("-- Locks ----------------------------------------------------------------")
    for s in sorted(lock_rows):
        a = lock_rows[s]
        out.append(
            "INSERT INTO locks (serial, door_name, room_number, location, area) VALUES ("
            f"{sql_str(a.lock_serial)}, {sql_str(a.door_name or None)}, "
            f"{sql_str(a.room_number or None)}, {sql_str(a.location)}, "
            f"{sql_str(a.area or None)})\n"
            "ON CONFLICT (serial) DO UPDATE SET "
            "door_name=excluded.door_name, room_number=excluded.room_number, "
            "location=excluded.location, area=excluded.area;"
        )
    out.append("")

    out.append("-- Authorizations (transponder <-> lock) -------------------------------")
    for t_serial, l_serial in sorted(auth_pairs):
        out.append(
            "INSERT INTO authorizations (transponder_serial, lock_serial) VALUES ("
            f"{sql_str(t_serial)}, {sql_str(l_serial)}) ON CONFLICT DO NOTHING;"
        )
    out.append("")
    out.append("COMMIT;")
    out.append("")
    return "\n".join(out)


# --- CLI -------------------------------------------------------------------

def collect_pdfs(paths: list[str]) -> list[str]:
    """Expand files, globs, and directories into a sorted list of PDF paths."""
    found: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            found.extend(glob.glob(os.path.join(p, "*.pdf")))
        elif any(ch in p for ch in "*?["):
            found.extend(glob.glob(p))
        else:
            found.append(p)
    # De-duplicate while preserving a stable, sorted order.
    return sorted(dict.fromkeys(os.path.abspath(f) for f in found))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Parse SimonsVoss transponder PDFs into a self-contained "
                    "SQL file (standalone schema; NOT the Django app DB — use "
                    "the web upload or `manage.py loadpdfs` for that).")
    ap.add_argument("paths", nargs="+", help="PDF files, globs, or directories")
    ap.add_argument("-o", "--output", default="transponders.sql",
                    help="output .sql file (default: transponders.sql)")
    ap.add_argument("--no-ddl", action="store_true",
                    help="omit CREATE TABLE statements (emit inserts only)")
    ap.add_argument("--drop", action="store_true",
                    help="prepend DROP TABLE statements")
    args = ap.parse_args(argv)

    pdfs = collect_pdfs(args.paths)
    if not pdfs:
        print("No PDF files matched.", file=sys.stderr)
        return 1

    transponders: list[Transponder] = []
    for path in pdfs:
        try:
            tp = parse_pdf(path)
        except Exception as exc:  # keep going across a batch
            print(f"  ! {os.path.basename(path)}: failed to parse ({exc})",
                  file=sys.stderr)
            continue
        transponders.append(tp)
        label = tp.person_name or (f"ASTA {tp.asta_number}" if tp.asta_number else "—")
        print(f"  {os.path.basename(path):16} {tp.serial:10} {label:22} "
              f"{len(tp.authorizations):3} doors")

    if not transponders:
        print("Nothing parsed.", file=sys.stderr)
        return 1

    sql = build_sql(transponders, ddl=not args.no_ddl, drop=args.drop)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(sql)
    print(f"\nWrote {args.output}  ({len(transponders)} transponders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
