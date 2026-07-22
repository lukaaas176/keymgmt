"""Import an LSM *CSV* matrix export, enriched with the active/planned split
from the matching native PDF.

The CSV of a Schließmatrix carries what the PDF cannot: every lock's real
serial (``DC-…``), untruncated door names, and room/floor/location metadata.
What it lacks is the bold-vs-thin cross distinction — its marks are a flat
``x``. The native PDF reader supplies exactly that, so the two are merged:

* geometry and identity come from the CSV (real serials, full names);
* every ``x`` is split into an active (bold ×) or planned (thin ×) grant by
  looking up the PDF's mark state at the *same grid position*.

The CSV and the PDF are the same export in two shapes, emitted in the same
row/column order, so cells are matched by index — which also keeps two
distinct locks that happen to share a door name (each its own serial) apart.
"""

from __future__ import annotations

import csv
import io
import re

from django.db import transaction

from .models import Lock, Transponder

# CSV layout (0-based): header rows 0-5, door rows from 6; person columns from
# column 8. The serial of each person column is on row 5; door metadata sits in
# columns 0-7 of each door row.
_MARK_COL0 = 8
_SERIAL_ROW = 5
_DOOR_ROW0 = 6
_SYS, _AREA, _SERIAL, _NAME, _STANDORT, _GEBAEUDE, _ETAGE, _RAUM = range(8)


def _norm(s: str) -> str:
    """Collapse the export's runs of spaces to single spaces."""
    return re.sub(r"\s+", " ", (s or "").strip())


def _read(path: str) -> list[list[str]]:
    with open(path, "rb") as fh:
        txt = fh.read().decode("utf-16")
    txt = txt.replace("\r\n", "\n").replace("\r", "\n")
    return list(csv.reader(io.StringIO(txt), delimiter=";"))


def _cell(row: list[str], i: int) -> str:
    """A cell, tolerating rows shorter than the metadata block."""
    return row[i].strip() if i < len(row) else ""


def parse_asta_csv(path: str) -> dict:
    """Parse an LSM CSV export into person columns, door rows and marks.

    Returns a dict with:

    * ``person_serials`` — serial per column, left to right;
    * ``doors`` — one dict per row: ``serial``, ``door_name``, ``room_number``,
      ``location`` (Standort.Gebäude.Etage), ``area``;
    * ``marks`` — set of ``(col_index, row_index)`` where the cell holds an x.
    """
    rows = _read(path)
    ncol = max(len(r) for r in rows)
    person_serials, col_index = [], []
    for c in range(_MARK_COL0, ncol):
        ser = rows[_SERIAL_ROW][c].strip() if c < len(rows[_SERIAL_ROW]) else ""
        if ser:
            person_serials.append(ser)
            col_index.append(c)

    doors, marks = [], set()
    for r in rows[_DOOR_ROW0:]:
        if not _cell(r, _NAME):
            continue
        ri = len(doors)
        loc = ".".join(p for p in (_cell(r, _STANDORT), _cell(r, _GEBAEUDE),
                                   _norm(_cell(r, _ETAGE))) if p)
        doors.append({
            "serial": _cell(r, _SERIAL),
            "door_name": _norm(_cell(r, _NAME))[:255],
            "room_number": _norm(_cell(r, _RAUM))[:64],
            "location": loc[:64],
            "area": _norm(_cell(r, _AREA))[:64],
        })
        for ci, c in enumerate(col_index):
            if c < len(r) and r[c].strip().lower() == "x":
                marks.add((ci, ri))
    return {"person_serials": person_serials, "doors": doors, "marks": marks}


def _door_align_key(name: str) -> str:
    """Alphanumeric skeleton of a door name, for position alignment."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


@transaction.atomic
def import_asta_csv(csv_path: str, pdf_path: str, source_name: str) -> dict:
    """Rebuild transponders, locks and grants from a CSV + its native PDF.

    Wipes and repopulates both tables — the export is the current, complete
    truth for this system, and the additive matrix importer would otherwise
    union it onto stale rights. Locks and door metadata come from the CSV,
    the active/planned split from the PDF at each matching cell. Atomic, so a
    mid-rebuild failure rolls the wipe back instead of leaving an empty DB.
    """
    from . import ocr

    data = parse_asta_csv(csv_path)
    res = ocr.parse_native_matrix(pdf_path)

    # Cross-check that the two exports line up before trusting an index join.
    pdf_persons = sorted(res.persons, key=lambda p: p.column)
    if [p.serial for p in pdf_persons] != data["person_serials"]:
        raise ValueError("CSV and PDF person columns differ — cannot merge "
                         "(re-export both from the same matrix).")
    pdf_doors = sorted(res.doors, key=lambda d: d.row)
    if len(pdf_doors) != len(data["doors"]):
        raise ValueError(f"CSV has {len(data['doors'])} door rows but the PDF "
                         f"has {len(pdf_doors)} — cannot merge.")
    # Doors join by position, so verify the two orders agree (the PDF name may
    # be truncated, so compare on the common prefix). Otherwise a divergent
    # re-export sort would silently bind grants to the wrong lock.
    for i, (cd, pd) in enumerate(zip(data["doors"], pdf_doors)):
        a, b = _door_align_key(cd["door_name"]), _door_align_key(pd.name)
        n = min(len(a), len(b))
        if n < 4 or a[:n] != b[:n]:
            raise ValueError(
                f"CSV and PDF door rows are out of order at row {i}: "
                f"{cd['door_name']!r} vs {pd.name!r} — cannot merge.")
    serials = [d["serial"] for d in data["doors"]]
    if not all(serials):
        raise ValueError("CSV has a door row with a blank lock serial — "
                         "cannot merge.")
    if len(set(serials)) != len(serials):
        raise ValueError("CSV has duplicate door serials — cannot merge.")

    # PDF mark state at 0-based (col, row): column/row are 1-based and share
    # the CSV's order (verified above / by row count).
    state = {(c - 1, r - 1): res.mark_states.get((c, r), "active")
             for (c, r) in res.marks}

    Transponder.objects.all().delete()
    Lock.objects.all().delete()

    locks = []
    for d in data["doors"]:
        locks.append(Lock.objects.create(
            serial=d["serial"], door_name=d["door_name"],
            room_number=d["room_number"], location=d["location"],
            area=d["area"]))

    transponders = []
    for p in pdf_persons:
        transponders.append(Transponder.objects.create(
            serial=p.serial, asta_number=p.asta_number,
            person_name=p.person_name or "", source_file=source_name))

    ActiveTh = Transponder.locks.through
    PlannedTh = Transponder.planned_locks.through
    active_rows, planned_rows = [], []
    for (ci, ri) in data["marks"]:
        tp, lk = transponders[ci], locks[ri]
        if state.get((ci, ri), "active") == "active":
            active_rows.append(ActiveTh(transponder_id=tp.serial,
                                        lock_id=lk.serial))
        else:
            planned_rows.append(PlannedTh(transponder_id=tp.serial,
                                          lock_id=lk.serial))
    ActiveTh.objects.bulk_create(active_rows)
    PlannedTh.objects.bulk_create(planned_rows)
    active_links, planned_links = len(active_rows), len(planned_rows)

    return {
        "transponders": len(transponders),
        "locks": len(locks),
        "marks": len(data["marks"]),
        "active": active_links,
        "planned": planned_links,
    }
