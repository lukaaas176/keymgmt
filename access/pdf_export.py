"""Export the access matrix as a tiled PDF, rendered with Typst.

Two stages, each usable and testable on its own:

* :func:`build_matrix_data` turns the DB into a plain, JSON-serialisable dict
  (ordered transponders, ordered doors, and a sparse mark map) — no I/O, no Typst;
* :func:`render_pdf` writes that dict next to the static ``matrix.typ``
  template and shells out to ``typst compile`` to produce the PDF bytes.

The template (access/templates/pdf/matrix.typ) owns all layout: it tiles the
full grid across A4 or A3 pages, so page size is a single ``--input`` switch.
Mark weights: ``2`` = active (bold ×, programmed), ``1`` = planned (thin ×).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .group_labels import combined_group_label
from .models import Lock, Transponder

_PDF_DIR = Path(__file__).resolve().parent / "templates" / "pdf"
TEMPLATE = _PDF_DIR / "matrix.typ"
CHANGES_TEMPLATE = _PDF_DIR / "changes.typ"
SCOPES = ("all", "active", "planned")
SIZES = ("a4", "a3")
MODES = ("matrix", "diff", "changes")

ACTIVE, PLANNED = 2, 1


def _transponders_and_doors(*relations):
    transponders = list(
        Transponder.objects.prefetch_related(*relations).order_by(
            "asta_number", "person_name", "serial"
        )
    )
    doors = list(Lock.objects.order_by("location", "door_name", "serial"))
    row_of = {lk.serial: i for i, lk in enumerate(doors)}
    return transponders, doors, row_of


def _meta(transponders, doors, **extra):
    return {
        "title": "Schließmatrix",
        "generated": extra.pop("today", dt.date.today()).isoformat(),
        "transponders": [
            {
                "label": t.label,
                "serial": t.serial,
                "asta": t.asta_number,
                "group": combined_group_label(t.groups.all()),
            }
            for t in transponders
        ],
        "doors": [
            {
                "name": d.door_name or d.serial,
                "serial": d.serial,
                "location": d.location or d.area or "",
            }
            for d in doors
        ],
        **extra,
    }


def build_matrix_data(scope: str = "all", *, today: dt.date | None = None) -> dict:
    """Assemble the matrix into a JSON-serialisable dict.

    ``scope`` restricts the marks: ``all`` keeps both weights, ``active`` only
    programmed grants, ``planned`` only pending ones. Transponder order matches the
    app (ASTA number, then name, then serial); doors sort by location.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")

    transponders, doors, row_of = _transponders_and_doors(
        "locks", "planned_locks", "groups"
    )
    marks: dict[str, int] = {}
    for ci, tp in enumerate(transponders):
        active = {lk.serial for lk in tp.locks.all()}
        planned = {lk.serial for lk in tp.planned_locks.all()}
        for serial in active | planned:
            ri = row_of.get(serial)
            if ri is None:
                continue
            weight = ACTIVE if serial in active else PLANNED
            if scope == "active" and weight != ACTIVE:
                continue
            if scope == "planned" and weight != PLANNED:
                continue
            marks[f"{ci}-{ri}"] = weight

    return _meta(
        transponders,
        doors,
        mode="matrix",
        scope=scope,
        today=today or dt.date.today(),
        marks=marks,
    )


def build_diff_data(*, today: dt.date | None = None, hide_empty: bool = False) -> dict:
    """Assemble the Soll/Ist (wish vs. configured) diff.

    Each non-blank cell maps to ``[weight, wished]``: ``weight`` is the
    configured state (2 active, 1 planned, 0 none) and ``wished`` is 1 if the
    door is in ``desired_locks``. The template colours a cell green when the
    two agree (configured ⇔ wished) and red when a door must still be added
    (wished, weight 0) or removed (weight > 0, not wished).

    ``hide_empty`` drops doors that carry no rights at all — no transponder has
    them active, planned, wished, or as a pending removal (hollow ×) — so the
    diff shows only doors that are programmed or in the Soll somewhere.
    """
    transponders, doors, row_of = _transponders_and_doors(
        "locks", "planned_locks", "desired_locks", "removed_locks", "groups"
    )
    # One pass to read each transponder's sets and collect the doors in use.
    tp_sets = []
    used: set[str] = set()
    for tp in transponders:
        active = {lk.serial for lk in tp.locks.all()}
        planned = {lk.serial for lk in tp.planned_locks.all()}
        desired = {lk.serial for lk in tp.desired_locks.all()}
        removed = {lk.serial for lk in tp.removed_locks.all()}
        tp_sets.append((active, planned, desired))
        used |= active | planned | desired | removed

    if hide_empty:
        doors = [d for d in doors if d.serial in used]
        row_of = {d.serial: i for i, d in enumerate(doors)}

    marks: dict[str, list[int]] = {}
    n_ok = n_add = n_remove = 0
    for ci, (active, planned, desired) in enumerate(tp_sets):
        for serial in active | planned | desired:
            ri = row_of.get(serial)
            if ri is None:
                continue
            weight = (
                ACTIVE if serial in active else (PLANNED if serial in planned else 0)
            )
            wished = 1 if serial in desired else 0
            marks[f"{ci}-{ri}"] = [weight, wished]
            if weight and wished:
                n_ok += 1
            elif wished:
                n_add += 1
            else:
                n_remove += 1

    return _meta(
        transponders,
        doors,
        mode="diff",
        scope="diff",
        today=today or dt.date.today(),
        marks=marks,
        counts={"ok": n_ok, "add": n_add, "remove": n_remove},
    )


def build_changes_data(*, today: dt.date | None = None) -> dict:
    """Assemble the reprogramming worklist: every *outstanding change*.

    For each transponder, compares the wish (``desired_locks``) against the
    configured state (active ∪ planned): doors are **added** (wished, not yet
    active/planned) or **removed** (active/planned but unwished). Each remove
    carries a ``note``: ``"geplant"`` when it was only a pending (thin ×) grant.

    A hollow × (``removed_locks`` — already withdrawn at the source, pending
    removal) is deliberately left out when it is *not* wished: the source is
    already removing it, so there is nothing for us to do. A hollow × that *is*
    wished surfaces as an **add** (it must be re-authorised), like any other
    unmet wish. Transponders that already match their wish are skipped.
    """
    transponders, doors, _ = _transponders_and_doors(
        "locks", "planned_locks", "desired_locks", "groups"
    )
    dmeta = {
        d.serial: {
            "name": d.door_name or d.serial,
            "location": d.location or d.area or "",
            "serial": d.serial,
        }
        for d in doors
    }

    def info(serial):
        # Return a fresh dict per call: entries are mutated per-transponder
        # (the ``note`` below), and the same door appears across many
        # transponders — a shared reference would leak one's note onto all.
        base = dmeta.get(serial)
        return (
            dict(base) if base else {"name": serial, "location": "", "serial": serial}
        )

    def ordered(serials):
        return sorted(
            (info(s) for s in serials), key=lambda d: (d["location"], d["name"])
        )

    changes = []
    tot_add = tot_remove = 0
    for tp in transponders:
        active = {lk.serial for lk in tp.locks.all()}
        planned = {lk.serial for lk in tp.planned_locks.all()}
        desired = {lk.serial for lk in tp.desired_locks.all()}
        configured = active | planned
        adds = ordered(desired - configured)
        removes = ordered(configured - desired)
        if not adds and not removes:
            continue
        for d in removes:
            d["note"] = (
                "geplant"
                if d["serial"] in planned and d["serial"] not in active
                else ""
            )
        tot_add += len(adds)
        tot_remove += len(removes)
        changes.append(
            {
                "label": tp.label,
                "serial": tp.serial,
                "asta": tp.asta_number,
                "group": combined_group_label(tp.groups.all()),
                "add": adds,
                "remove": removes,
            }
        )

    return {
        "title": "Ausstehende Änderungen",
        "generated": (today or dt.date.today()).isoformat(),
        "mode": "changes",
        "counts": {"add": tot_add, "remove": tot_remove, "transponders": len(changes)},
        "changes": changes,
    }


def render_pdf(data: dict, size: str = "a3") -> bytes:
    """Render ``data`` to PDF bytes via the Typst CLI.

    Raises ``RuntimeError`` if the ``typst`` binary is missing or the compile
    fails (the compiler's stderr is surfaced).
    """
    if size not in SIZES:
        raise ValueError(f"size must be one of {SIZES}, got {size!r}")
    if shutil.which("typst") is None:
        raise RuntimeError(
            "The 'typst' binary is not installed — install it (e.g. "
            "`brew install typst`) to export PDFs."
        )

    mode = data.get("mode", "matrix")
    template = CHANGES_TEMPLATE if mode == "changes" else TEMPLATE
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "data.json").write_text(json.dumps(data), encoding="utf-8")
        src = tmp / template.name
        shutil.copyfile(template, src)
        out = tmp / "out.pdf"
        proc = subprocess.run(
            [
                "typst",
                "compile",
                "--input",
                f"size={size}",
                "--input",
                f"mode={mode}",
                str(src),
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"typst compile failed:\n{proc.stderr.strip()}")
        return out.read_bytes()


def export_matrix_pdf(
    size: str = "a3", scope: str = "all", mode: str = "matrix", hide_empty: bool = False
) -> bytes:
    """Convenience: build the current DB's matrix (or diff) and render it.

    ``hide_empty`` only affects the diff: drop doors with no rights anywhere.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if mode == "changes":
        data = build_changes_data()
    elif mode == "diff":
        data = build_diff_data(hide_empty=hide_empty)
    else:
        data = build_matrix_data(scope)
    return render_pdf(data, size)
