"""Import a parsed printout into the database (used by the upload view and the
loadpdfs management command).

Two printout formats are supported and auto-detected:

* **list** — "Berechtigungen für den Transponder": one PDF per transponder,
  every door it opens, keyed by lock serial. The richest source; imports
  replace the transponder's data and access set.
* **matrix** — "Schließmatrix": one grid for many transponders. Scanned
  copies reach us through a lossy OCR layer, so matrix imports are
  deliberately conservative: they *fill in* missing transponder data
  (names, ASTA numbers, new transponders) and *add* authorizations when the
  matrix contains door rows, but never overwrite or remove anything a
  list printout established.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata

from django.db import transaction

from . import matrix_parser, ocr, pdf_parser
from .models import Lock, Transponder


def _as_date(iso: str | None):
    if not iso:
        return None
    try:
        return dt.date.fromisoformat(iso)
    except ValueError:
        return None


def import_pdf(path: str, source_name: str, *, include_removed: bool = True) -> dict:
    """Detect the format of the file at `path` and import it.

    PDFs may be list or matrix printouts (auto-detected); images
    (screenshots/photos of a matrix) go through tesseract OCR. Returns a
    summary dict; check `result["format"]` for which shape it has. All
    operations are idempotent, so re-uploading a file is safe.

    ``include_removed`` (default on) captures hollow outline crosses — doors
    still programmed but withdrawn (pending removal) — into ``removed_locks``.
    Only native-PDF matrices carry that distinction.
    """
    if ocr.is_image(path):
        return _import_matrix(ocr.parse_matrix_image(path), source_name)
    if matrix_parser.detect_format(path) == "matrix":
        # A native-PDF matrix draws its marks as vector graphics, so the
        # text-only parser reads columns/doors but zero authorizations;
        # render such files instead.
        if ocr.is_native_matrix_pdf(path):
            return _import_matrix(
                ocr.parse_native_matrix(path, include_removed=include_removed),
                source_name,
            )
        return _import_matrix(matrix_parser.parse_matrix_pdf(path), source_name)
    return _import_list(path, source_name)


@transaction.atomic
def _import_list(path: str, source_name: str) -> dict:
    """Import one per-transponder list printout (replaces its data)."""
    data = pdf_parser.parse_pdf(path)

    lock_objs = []
    for a in data.authorizations:
        # Only fill blank lock fields; never overwrite metadata another
        # source already set with this printout's (possibly empty or
        # truncated) value.
        defaults = {
            k: v
            for k, v in (
                ("door_name", (a.door_name or "").strip()[:255]),
                ("room_number", (a.room_number or "").strip()[:64]),
                ("location", (a.location or "").strip()[:64]),
                ("area", (a.area or "").strip()[:64]),
            )
            if v
        }
        obj, was_new = Lock.objects.get_or_create(
            serial=a.lock_serial, defaults=defaults
        )
        if not was_new:
            changed = False
            for field, value in defaults.items():
                if value and not getattr(obj, field):
                    setattr(obj, field, value)
                    changed = True
            if changed:
                obj.save()
        lock_objs.append(obj)

    tp, created = Transponder.objects.update_or_create(
        serial=data.serial,
        defaults={
            "asta_number": data.asta_number,
            "person_name": data.person_name or "",
            "locking_system": data.locking_system or "",
            "printed_on": _as_date(data.printed_on),
            "source_file": source_name,
        },
    )
    tp.locks.set(lock_objs)

    parsed = len(data.authorizations)
    return {
        "format": "list",
        "serial": data.serial,
        "label": tp.label,
        "parsed": parsed,
        "stated": data.record_count,
        "created": created,
        "consistent": data.record_count is None or data.record_count == parsed,
    }


def match_known_serial(serial: str, known: set[str]) -> str | None:
    """Resolve a scan-read serial against the serials already known.

    Scanner OCR confuses lookalike glyphs (S↔5, T↔7, A↔4 ...) in ways
    that produce a well-formed but wrong serial. If exactly one known
    serial is a positional lookalike of the parsed one, that is far more
    likely the same physical transponder than a coincidental near-twin.
    """
    if serial in known:
        return serial
    candidates = [k for k in known if matrix_parser.lookalike_equal(serial, k)]
    return candidates[0] if len(candidates) == 1 else None


def _matrix_lock_serial(door_name: str) -> str:
    """Deterministic synthetic serial for a door only known from a matrix.

    The matrix format identifies doors by name, not by lock serial; the
    'MX:' prefix keeps these apart from real SimonsVoss serials and makes
    re-imports idempotent.
    """
    digest = hashlib.sha1(door_name.strip().casefold().encode()).hexdigest()
    return f"MX:{digest[:10].upper()}"


def _norm_door(name: str) -> str:
    """Door-name key for matching: casefolded alphanumerics with the 0/O
    lookalike collapsed (OCR reads '1.OG' as '1.0G' and vice versa)."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return "".join(c for c in s if c.isalnum()).replace("0", "o")


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, cut off at `cap` (returns cap when exceeded)."""
    if abs(len(a) - len(b)) >= cap:
        return cap
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) >= cap:
            return cap
        prev = cur
    return min(prev[-1], cap)


def _door_digits(name: str) -> str:
    """Digit sequence of a door name — its load-bearing identifier (room,
    floor, cabinet number). Taken from the *raw* casefolded name, before the
    _norm_door 0->o collapse, so a genuine zero still counts: 'Raum 100' and
    'Raum 1' are different numbers and must not fuzzy-merge. The 0/O OCR
    confusion ('1.OG' vs '1.0G') is handled on the normalized-name path,
    which matches before the fuzzy fallback that uses this guard."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s if c.isdigit())


# Orientation words that distinguish otherwise-identical doors. Whole-word
# swaps between these (Ost<->West) sit only 1-2 edits apart, so plain edit
# distance would wrongly merge 'Raum 0321 Ost' into 'Raum 0321 West'.
_ORIENT = {
    "ost",
    "west",
    "nord",
    "sued",
    "sud",
    "no",
    "nw",
    "so",
    "sw",
    "nordost",
    "nordwest",
    "suedost",
    "suedwest",
    "sudost",
    "sudwest",
    "links",
    "rechts",
    "mitte",
    "oben",
    "unten",
    "innen",
    "aussen",
}
_ORIENT_SUFFIX = (
    "nordost",
    "nordwest",
    "suedost",
    "suedwest",
    "sudost",
    "sudwest",
    "ost",
    "west",
    "nord",
    "sued",
    "sud",
)


def _door_sides(name: str) -> frozenset[str]:
    """Side markers that make two similar door names *different* doors:
    orientation words (Ost/West/Nord/Süd…) and standalone single-letter
    sub-door tags (the 'A' in 'Raum -1368 A', the 'O'/'W' in 'Raum 201 O').
    Two names whose side markers differ are never fuzzy-merged, however
    small their edit distance. Süd folds to 'sud' once diacritics drop."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    out = set()
    for t in re.findall(r"[a-z0-9]+", s):
        if len(t) == 1 and t.isalpha():
            out.add(t)  # sub-door tag: A / B / O / W
        elif t in _ORIENT:
            out.add(t)
        else:  # orientation glued to a number
            for o in _ORIENT_SUFFIX:  # e.g. '0321ost'
                if t.endswith(o) and t[: -len(o)].isdigit():
                    out.add(o)
                    break
    return frozenset(out)


def match_lock_by_name(name: str, locks) -> Lock | None:
    """Find the lock a matrix door row refers to.

    Matrix rows carry no lock serial, and an OCR'd door name may differ
    from the list-printout spelling by a couple of glyphs
    ('Besprechunasraum'). Match exactly, then on the normalized form,
    then by a unique minimal edit distance ≤ 2 over normalized names —
    but only when the two names carry the SAME digits and the SAME side
    markers, so that 'Herd 1.OG' vs 'Herd 2.OG' (a digit apart) and
    'Raum 201 O' vs 'Raum 201 W' (Ost vs West, one letter apart) are never
    merged. A missed match just creates a new MX: lock; a wrong match
    corrupts an existing one, so precision is preferred here.
    """
    for lk in locks:
        if lk.door_name.casefold() == name.casefold():
            return lk
    key = _norm_door(name)
    if not key:
        return None
    norm = {lk: _norm_door(lk.door_name) for lk in locks if lk.door_name}
    exact = [lk for lk, nk in norm.items() if nk == key]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    digits = _door_digits(name)
    sides = _door_sides(name)
    scored = [
        (_edit_distance(key, nk), lk)
        for lk, nk in norm.items()
        if _door_digits(lk.door_name) == digits and _door_sides(lk.door_name) == sides
    ]
    scored.sort(key=lambda t: t[0])
    if (
        scored
        and scored[0][0] <= 2
        and (len(scored) == 1 or scored[1][0] > scored[0][0])
    ):
        return scored[0][1]
    return None


@transaction.atomic
def _import_matrix(data: matrix_parser.MatrixResult, source_name: str) -> dict:
    """Import one parsed Schließmatrix (fills gaps, never overwrites)."""
    warnings = list(data.warnings)

    # Doors: match a row against locks that existed *before* this import,
    # else create an MX: lock. Two rows of one matrix are two distinct
    # doors by definition (just like two columns are two distinct transponders),
    # so a row must never fuzzy-match a lock this same import just created
    # — that would merge e.g. 'Raum 0321 Ost' into 'Raum 0321 West'. Rows
    # with an *identical* name still collapse to one lock: the synthetic
    # serial is a deterministic function of the name.
    existing_locks = list(Lock.objects.all())
    lock_by_row: dict[int, Lock] = {}
    doors_created = 0
    doors_matched_fuzzy: list[tuple[str, str]] = []
    for d in data.doors:
        lk = match_lock_by_name(d.name, existing_locks)
        if lk is not None and str(lk.door_name).casefold() != d.name.casefold():
            doors_matched_fuzzy.append((d.name, str(lk.door_name)))
        if lk is None:
            lk, was_new = Lock.objects.get_or_create(
                serial=_matrix_lock_serial(d.name),
                defaults={
                    "door_name": d.name,
                    "room_number": d.room_number,
                    "location": d.floor,
                },
            )
            doors_created += was_new
        lock_by_row[d.row] = lk

    # Split each column's marks into active (opens now) and planned (opens
    # after the terminal update). Without mark states — list printouts,
    # image OCR — every mark is a current authorization.
    active_by_column: dict[int, list[Lock]] = {}
    planned_by_column: dict[int, list[Lock]] = {}
    removed_by_column: dict[int, list[Lock]] = {}
    by_state = {"planned": planned_by_column, "remove": removed_by_column}
    for col, row in data.marks:
        if row not in lock_by_row:
            continue
        state = data.mark_states.get((col, row), "active")
        bucket = by_state.get(state, active_by_column)
        bucket.setdefault(col, []).append(lock_by_row[row])

    # Transponders. Lookalike corrections only make sense against serials
    # that existed before this file: two columns of one matrix are two
    # different transponders by definition, so they must never merge into each
    # other. Exact reads claim their serials first, so a correction can
    # never steal a serial that another column of this file matches
    # exactly.
    pre_known = set(Transponder.objects.values_list("serial", flat=True))
    used_targets = {p.serial for p in data.persons if p.serial in pre_known}
    created = updated = 0
    corrected: list[tuple[str, str]] = []
    skipped: list[str] = []

    for p in data.persons:
        serial = p.serial
        if data.ocr_scan and serial not in pre_known:
            m = match_known_serial(serial, pre_known - used_targets)
            if m:
                corrected.append((serial, m))
                serial = m
        used_targets.add(serial)

        if not p.serial_valid and serial not in pre_known:
            skipped.append(p.raw_serial)
            warnings.append(
                f"column {p.column}: unreadable serial {p.raw_serial!r} "
                f"({p.person_name or 'no name'}) — not imported"
            )
            continue

        tp, was_created = Transponder.objects.get_or_create(
            serial=serial, defaults={"source_file": source_name}
        )
        changed = False
        if p.asta_number is not None and tp.asta_number is None:
            tp.asta_number = p.asta_number
            changed = True
        if p.person_name and not tp.person_name:
            tp.person_name = p.person_name
            changed = True
        if changed:
            tp.save()
            updated += not was_created
        created += was_created

        active = active_by_column.get(p.column)
        planned = planned_by_column.get(p.column)
        removed = removed_by_column.get(p.column)
        # Each door carries exactly one state in this matrix (bold/thin/hollow),
        # so activating/planning/removing a door supersedes whatever a prior
        # import recorded for it — clear the other two buckets to keep the three
        # sets mutually exclusive (else a re-authorised hollow × would linger in
        # both locks and removed_locks and be double-counted downstream).
        if active:
            tp.locks.add(*active)
            tp.planned_locks.remove(*active)
            tp.removed_locks.remove(*active)
        if planned:
            # A planned grant that is already active (in this matrix or from
            # an earlier import) needs no second, pending listing.
            already_active = set(tp.locks.all())
            tp.planned_locks.add(*[lk for lk in planned if lk not in already_active])
            tp.removed_locks.remove(*planned)
        if removed:
            # Hollow × — still programmed but withdrawn (pending removal).
            tp.removed_locks.add(*removed)
            tp.locks.remove(*removed)
            tp.planned_locks.remove(*removed)

    n_active = sum(
        1 for k in data.marks if data.mark_states.get(k, "active") == "active"
    )
    n_removed = sum(1 for k in data.marks if data.mark_states.get(k) == "remove")
    return {
        "format": "matrix",
        "persons": len(data.persons),
        "expected": data.expected_columns,
        "created": created,
        "updated": updated,
        "corrected": corrected,
        "skipped": skipped,
        "doors": len(data.doors),
        "doors_created": doors_created,
        "doors_matched_fuzzy": doors_matched_fuzzy,
        "marks": len(data.marks),
        "active_marks": n_active,
        "planned_marks": len(data.marks) - n_active - n_removed,
        "removed_marks": n_removed,
        "ocr_scan": data.ocr_scan,
        "consistent": data.consistent and not skipped,
        "warnings": warnings,
        "label": f"locking matrix · {len(data.persons)} transponders",
    }
