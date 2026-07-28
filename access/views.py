import json
import os
import tempfile
from collections import defaultdict

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from . import ocr, pdf_export, soll
from .forms import LockForm, TransponderForm
from .group_labels import (
    combined_group_label,
    derive_export_code,
    normalize_export_code,
    set_group_export_metadata,
)
from .models import Group, Lock, Transponder
from .services import import_pdf

ACCENT_RGB = "79,70,229"  # indigo-600, used for heatmap intensity


# --- upload -----------------------------------------------------------------


def upload(request):
    """Handle the multi-file upload posted from the dashboard."""
    if request.method != "POST":
        return redirect("dashboard")

    files = request.FILES.getlist("pdfs")
    if not files:
        messages.warning(request, "Choose at least one file to upload.")
        return redirect("dashboard")

    # Import options (checkbox with a hidden "off" default → respects unchecking).
    include_removed = request.POST.get("include_removed", "on") == "on"

    added = updated = 0
    for f in files:
        suffix = os.path.splitext(f.name)[1].lower()
        if suffix != ".pdf" and not ocr.is_image(f.name):
            messages.error(request, f"{f.name}: not a PDF or image, skipped.")
            continue
        if ocr.is_image(f.name) and not ocr.tesseract_available():
            messages.error(
                request,
                f"{f.name}: importing images requires the tesseract OCR "
                f"binary (e.g. `brew install tesseract`).",
            )
            continue
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
                for chunk in f.chunks():
                    tmp.write(chunk)
                tmp.flush()
                r = import_pdf(tmp.name, f.name, include_removed=include_removed)
        except Exception as exc:  # noqa: BLE001 - surface to the user
            messages.error(request, f"{f.name}: could not be read ({exc}).")
            continue

        if r["format"] == "matrix":
            added, updated = added + r["created"], updated + r["updated"]
            parts = [f"{r['persons']} transponders ({r['created']} new)"]
            if r["corrected"]:
                fixes = ", ".join(f"{a}→{b}" for a, b in r["corrected"])
                parts.append(
                    f"{len(r['corrected'])} scan-read serial(s) "
                    f"matched to known transponders: {fixes}"
                )
            if r["doors"]:
                auth = f"{r['doors']} doors / {r['marks']} authorizations"
                if r.get("planned_marks") or r.get("removed_marks"):
                    bits = [f"{r['active_marks']} active"]
                    if r.get("planned_marks"):
                        bits.append(f"{r['planned_marks']} planned")
                    if r.get("removed_marks"):
                        bits.append(f"{r['removed_marks']} pending removal")
                    auth += " (" + ", ".join(bits) + ")"
                parts.append(auth)
            if r["doors_matched_fuzzy"]:
                parts.append(
                    f"{len(r['doors_matched_fuzzy'])} door name(s) "
                    f"matched to existing locks despite OCR noise"
                )
            messages.success(request, f"{f.name}: locking matrix — {'; '.join(parts)}.")
            for w in r["warnings"]:
                messages.warning(request, f"{f.name}: {w}")
        else:
            added, updated = added + r["created"], updated + (not r["created"])
            note = (
                ""
                if r["consistent"]
                else (
                    f" — but the printout states {r['stated']} rows, so please re-check"
                )
            )
            messages.success(
                request, f"{f.name}: {r['label']} — {r['parsed']} doors{note}."
            )

    if added or updated:
        messages.info(request, f"{added} added, {updated} updated.")
    return redirect("dashboard")


# --- pdf export -------------------------------------------------------------


def export_pdf(request):
    """Stream the locking matrix as a tiled PDF.

    ``?size=a4|a3``; ``?mode=diff`` renders the Soll/Ist comparison, otherwise
    ``?scope=all|active|planned`` selects which marks the matrix shows.
    ``?empty=hide`` (diff only) drops doors with no rights anywhere.
    """
    mode = request.GET.get("mode")
    mode = mode if mode in pdf_export.MODES else "matrix"
    size = request.GET.get("size")
    # A flat change list reads best on portrait A4; the matrix defaults to A3.
    size = size if size in pdf_export.SIZES else ("a4" if mode == "changes" else "a3")
    scope = request.GET.get("scope")
    scope = scope if scope in pdf_export.SCOPES else "all"
    hide_empty = request.GET.get("empty") == "hide"
    try:
        pdf = pdf_export.export_matrix_pdf(
            size=size, scope=scope, mode=mode, hide_empty=hide_empty
        )
    except RuntimeError as exc:  # typst missing / compile failed
        messages.error(request, f"PDF export failed: {exc}")
        return redirect("dashboard")
    tag = mode if mode in ("diff", "changes") else scope
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = (
        f'attachment; filename="schliessmatrix-{tag}-{size}.pdf"'
    )
    return resp


# --- dashboard --------------------------------------------------------------


def dashboard(request):
    transponders = list(
        Transponder.objects.annotate(n=Count("locks"))
        .prefetch_related("groups")
        .order_by("asta_number", "person_name", "serial")
    )
    for transponder in transponders:
        transponder.group_label = combined_group_label(transponder.groups.all())
    ctx = {
        "transponder_count": Transponder.objects.count(),
        "lock_count": Lock.objects.count(),
        "grant_count": Transponder.locks.through.objects.count(),
        "transponders": transponders,
        "nav": "dashboard",
    }
    return render(request, "access/dashboard.html", ctx)


# --- transponders -----------------------------------------------------------


def transponder_list(request):
    transponders = list(
        Transponder.objects.annotate(n=Count("locks"))
        .prefetch_related("groups")
        .order_by("asta_number", "person_name", "serial")
    )
    for transponder in transponders:
        transponder.group_label = combined_group_label(transponder.groups.all())
    return render(
        request,
        "access/transponder_list.html",
        {"transponders": transponders, "nav": "transponders"},
    )


@ensure_csrf_cookie
def transponder_detail(request, serial):
    tp = get_object_or_404(
        Transponder.objects.prefetch_related(
            "groups__doors", "locks", "planned_locks", "removed_locks", "desired_locks"
        ),
        pk=serial,
    )
    # Read every relation once, from the prefetch cache (.all()); derive the
    # serial sets from those objects instead of re-querying with values_list.
    active_locks = list(tp.locks.all())
    planned_locks = list(tp.planned_locks.all())
    removed_locks = list(tp.removed_locks.all())
    desired_locks = list(tp.desired_locks.all())
    active = {lk.serial for lk in active_locks}
    planned = {lk.serial for lk in planned_locks}
    removed = {lk.serial for lk in removed_locks}
    desired = {lk.serial for lk in desired_locks}
    tp_group_ids = {g.id for g in tp.groups.all()}
    # Which doors are inherited from a group (and from which) — inherited doors
    # are shown distinctly and locked in the editor, never as individual grants.
    inherited_from = defaultdict(list)
    for g in tp.groups.all():
        for lk in g.doors.all():
            inherited_from[lk.serial].append(g.name)
    # "Doors by location": the full picture per door — the current programming
    # (active × / planned × / hollow ×) *and* the Soll-driven planned change.
    # Each door gets a source `state` and, when the Soll disagrees with it, a
    # `soll` annotation (add / remove / keep / unwished).
    relevant = {}
    for lk in active_locks + planned_locks + removed_locks + desired_locks:
        relevant.setdefault(lk.serial, lk)
    grouped_map = defaultdict(list)
    for s, lk in relevant.items():
        a, p, r, d = s in active, s in planned, s in removed, s in desired
        if a:
            state, soll = "active", ("remove" if not d else "")
        elif p:
            state, soll = "planned", ("unwished" if not d else "")
        elif r:
            state, soll = "removed", ("keep" if d else "")
        else:  # only in the Soll → a planned addition
            state, soll = "soll_add", ""
        grouped_map[lk.location or "—"].append(
            {"lock": lk, "state": state, "soll": soll}
        )
    grouped = sorted(
        (loc, sorted(v, key=lambda x: (x["lock"].door_name, x["lock"].serial)))
        for loc, v in grouped_map.items()
    )
    # All doors by building, tagged with desired + current state, for the editor.
    sections = []
    for loc, locks in _door_sections():
        rows = [
            {
                "lock": lk,
                "on": lk.serial in desired,
                "inherited": lk.serial in inherited_from,
                "via": ", ".join(inherited_from.get(lk.serial, [])),
                "state": "active"
                if lk.serial in active
                else ("planned" if lk.serial in planned else ""),
            }
            for lk in locks
        ]
        sections.append({"location": loc, "rows": rows})
    all_groups = [
        {"id": g.id, "name": g.name, "on": g.id in tp_group_ids}
        for g in Group.objects.all()
    ]
    return render(
        request,
        "access/transponder_detail.html",
        {
            "tp": tp,
            "grouped": grouped,
            "sections": sections,
            "all_groups": all_groups,
            "desired_total": len(desired),
            "active_total": len(active),
            "planned_total": len(planned),
            "removed_total": len(removed),
            "lock_total": len(active | planned),
            "nav": "transponders",
        },
    )


# --- locks ------------------------------------------------------------------


def lock_list(request):
    locks = Lock.objects.annotate(n=Count("transponders")).order_by(
        "location", "door_name", "serial"
    )
    return render(request, "access/lock_list.html", {"locks": locks, "nav": "locks"})


@ensure_csrf_cookie
def lock_detail(request, serial):
    lock = get_object_or_404(Lock, pk=serial)
    lock_group_ids = set(lock.groups.values_list("id", flat=True))
    all_groups = [
        {"id": g.id, "name": g.name, "on": g.id in lock_group_ids}
        for g in Group.objects.all()
    ]

    order = ("asta_number", "person_name", "serial")
    active = list(lock.transponders.order_by(*order))
    planned = list(lock.planned_transponders.order_by(*order))
    removed = list(lock.removed_transponders.order_by(*order))  # hollow ×
    desirers = list(
        lock.desired_transponders.prefetch_related("groups").order_by(*order)
    )
    desired_serials = {t.serial for t in desirers}

    # "Im Soll einzelner Transponder": only those who want this door
    # *individually* — not through a group that already contains it. Read groups
    # from the prefetch cache (.all(), not .values_list which re-queries).
    individual_desirers = [
        t for t in desirers if not ({g.id for g in t.groups.all()} & lock_group_ids)
    ]

    # Current holders (active ∪ removed are both programmed now) split by fate.
    def item(t, badge=None):
        return {"tp": t, "badge": badge}

    sort_key = lambda it: (
        it["tp"].asta_number if it["tp"].asta_number is not None else 10**9,
        it["tp"].person_name or "",
        it["tp"].serial,
    )
    # A hollow-× door that IS wished is being re-granted → "Bleibt" (mirrors the
    # soll="keep" tag on the transponder page), not "Wird entfernt".
    keep = [item(t) for t in active if t.serial in desired_serials] + [
        item(t, "Soll: behalten") for t in removed if t.serial in desired_serials
    ]
    keep.sort(key=sort_key)
    remove = [
        item(t, "entzogen")
        for t in removed  # hollow ×, unwished
        if t.serial not in desired_serials
    ] + [item(t, "nicht im Soll") for t in active if t.serial not in desired_serials]
    remove.sort(key=sort_key)
    status_cols = [
        {
            "title": "Bleibt",
            "hint": "aktiv und im Soll",
            "items": keep,
            "dot": "bg-emerald-500",
            "ring": "ring-emerald-200 bg-emerald-50/40",
            "count_cls": "text-emerald-600",
        },
        {
            "title": "Wird entfernt",
            "hint": "hohles × (entzogen) oder nicht im Soll",
            "items": remove,
            "dot": "bg-rose-500",
            "ring": "ring-rose-200 bg-rose-50/60",
            "count_cls": "text-rose-600",
        },
        {
            "title": "Geplant",
            "hint": "wird am Terminal programmiert",
            "items": [item(t) for t in planned],
            "dot": "bg-amber-400",
            "ring": "ring-amber-200 bg-amber-50/60",
            "count_cls": "text-amber-600",
        },
    ]

    return render(
        request,
        "access/lock_detail.html",
        {
            "lock": lock,
            "holder_total": len(active),
            "all_groups": all_groups,
            "desirers": individual_desirers,
            "addable": Transponder.objects.exclude(serial__in=desired_serials).order_by(
                *order
            ),
            "status_cols": status_cols,
            "has_status": bool(keep or remove or planned),
            "nav": "locks",
        },
    )


# --- lock / transponder create · edit · delete ------------------------------


def lock_create(request):
    form = LockForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        lock = form.save()
        messages.success(request, f"Tür {lock.serial} angelegt.")
        return redirect("lock_detail", serial=lock.serial)
    return render(
        request,
        "access/object_form.html",
        {
            "form": form,
            "title": "Neue Tür",
            "nav": "locks",
            "back_url": reverse("lock_list"),
        },
    )


def lock_edit(request, serial):
    lock = get_object_or_404(Lock, pk=serial)
    form = LockForm(request.POST or None, instance=lock)
    form.fields["serial"].disabled = True  # the serial is the identity
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Tür {lock.serial} gespeichert.")
        return redirect("lock_detail", serial=lock.serial)
    return render(
        request,
        "access/object_form.html",
        {
            "form": form,
            "title": "Tür bearbeiten",
            "nav": "locks",
            "back_url": reverse("lock_detail", args=[lock.serial]),
        },
    )


@require_POST
def lock_delete(request, serial):
    lock = get_object_or_404(Lock, pk=serial)
    lock.delete()  # M2M join rows cascade away
    messages.success(request, f"Tür {serial} gelöscht.")
    return redirect("lock_list")


def transponder_create(request):
    form = TransponderForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        tp = form.save()
        messages.success(request, f"Transponder {tp.serial} angelegt.")
        return redirect("transponder_detail", serial=tp.serial)
    return render(
        request,
        "access/object_form.html",
        {
            "form": form,
            "title": "Neuer Transponder",
            "nav": "transponders",
            "back_url": reverse("transponder_list"),
        },
    )


def transponder_edit(request, serial):
    tp = get_object_or_404(Transponder, pk=serial)
    form = TransponderForm(request.POST or None, instance=tp)
    form.fields["serial"].disabled = True
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Transponder {tp.serial} gespeichert.")
        return redirect("transponder_detail", serial=tp.serial)
    return render(
        request,
        "access/object_form.html",
        {
            "form": form,
            "title": "Transponder bearbeiten",
            "nav": "transponders",
            "back_url": reverse("transponder_detail", args=[tp.serial]),
        },
    )


@require_POST
def transponder_delete(request, serial):
    tp = get_object_or_404(Transponder, pk=serial)
    tp.delete()
    messages.success(request, f"Transponder {serial} gelöscht.")
    return redirect("transponder_list")


# --- overlap / groups -------------------------------------------------------


def overlap(request):
    """Similarity heatmap, identical-access clone groups, and access tiers.

    Computed over one of three scoped door-sets per transponder, so the toggle is a
    single switch:

    * ``active``  — doors the transponder opens now (default);
    * ``planned`` — the end state once pending updates are written
      (active ∪ planned);
    * ``diff``    — only the pending grants (planned), i.e. who shares the
      same upcoming changes.
    """
    scope = request.GET.get("scope")
    if scope not in ("active", "planned", "diff"):
        scope = "active"
    tps = list(
        Transponder.objects.prefetch_related("locks", "planned_locks").order_by(
            "asta_number", "person_name", "serial"
        )
    )

    # Read the door sets from the prefetch cache (.all(), not .values_list /
    # .exists(), which re-query per transponder), and derive planned_pending in the
    # same pass.
    sets = {}
    planned_pending = False
    for tp in tps:
        active_doors = {lk.serial for lk in tp.locks.all()}
        planned_doors = {lk.serial for lk in tp.planned_locks.all()}
        if planned_doors:
            planned_pending = True
        sets[tp.serial] = {
            "active": active_doors,
            "planned": active_doors | planned_doors,
            "diff": planned_doors,
        }[scope]

    # Pairwise Jaccard similarity -> heatmap cells.
    matrix = []
    for a in tps:
        row = []
        for b in tps:
            sa, sb = sets[a.serial], sets[b.serial]
            shared = len(sa & sb)
            union = len(sa | sb) or 1
            jac = shared / union
            row.append(
                {
                    "shared": shared,
                    "pct": round(jac * 100),
                    "alpha": round(0.08 + 0.92 * jac, 3),  # keep faint cells visible
                    "dark": jac >= 0.55,
                    "self": a.serial == b.serial,
                }
            )
        matrix.append(row)

    # Transponders whose access set is identical -> "clones". Skip empty
    # sets: transponders that share "no access" (e.g. no pending change under
    # scope=diff) are not a meaningful group. Carry the scoped door count so
    # the template need not fall back to the (scope-blind) active .locks.
    by_set = defaultdict(list)
    for tp in tps:
        doors = sets[tp.serial]
        if doors:
            by_set[frozenset(doors)].append(tp)
    clone_groups = [
        {"transponders": g, "doors": len(dset)}
        for dset, g in by_set.items()
        if len(g) > 1
    ]
    clone_groups.sort(key=lambda grp: len(grp["transponders"]), reverse=True)

    # Locks grouped by the exact set of holders (in scope) -> tiers. Invert
    # the scoped door-sets so a planned view reflects planned holders too.
    all_locks = {lk.serial: lk for lk in Lock.objects.all()}
    holders_by_lock = defaultdict(set)
    for tp in tps:
        for lock_serial in sets[tp.serial]:
            holders_by_lock[lock_serial].add(tp.serial)
    by_holders = defaultdict(list)
    for lock_serial, holders in holders_by_lock.items():
        by_holders[frozenset(holders)].append(all_locks[lock_serial])
    name = {tp.serial: tp.label for tp in tps}
    tiers = []
    for holders, locks in by_holders.items():
        tiers.append(
            {
                "holders": sorted(name[s] for s in holders),
                "holder_count": len(holders),
                "locks": sorted(locks, key=lambda x: (x.location, x.door_name)),
                "lock_count": len(locks),
            }
        )
    tiers.sort(key=lambda t: (-t["lock_count"], -t["holder_count"]))

    # Data for client-side "shared doors" panel when a cell is clicked.
    tp_data = [
        {"serial": tp.serial, "label": tp.label, "locks": sorted(sets[tp.serial])}
        for tp in tps
    ]
    lock_names = {lk.serial: lk.label for lk in all_locks.values()}

    return render(
        request,
        "access/overlap.html",
        {
            "tps": tps,
            "rows": list(zip(tps, matrix)),
            "clone_groups": clone_groups,
            "tiers": tiers[:12],
            "tp_data": tp_data,
            "lock_names": lock_names,
            "accent_rgb": ACCENT_RGB,
            "nav": "overlap",
            "scope": scope,
            "planned_pending": planned_pending,
        },
    )


# --- individual (non-group) access -----------------------------------------


def individual_access(request):
    """Look up every transponder's *individual* access — doors it holds that are
    **not** provided by any group it belongs to.

    Two bases via ``?scope``: ``ist`` (currently programmed — active ∪ planned ∪
    hollow-×) or ``soll`` (the target ``desired`` state — the default).
    Group-inherited doors are excluded either way. Each door is tagged by its
    programming reality: ``active`` (programmed), ``planned`` (pending),
    ``removed`` (hollow ×, pending removal), or ``add`` (in the Soll but not yet
    programmed).
    """
    scope = "ist" if request.GET.get("scope") in ("ist", "active") else "soll"
    tps = Transponder.objects.prefetch_related(
        "locks", "planned_locks", "removed_locks", "desired_locks", "groups__doors"
    ).order_by("asta_number", "person_name", "serial")
    rows = []
    n_grants = 0
    for tp in tps:
        active_locks = list(tp.locks.all())
        planned_locks = list(tp.planned_locks.all())
        removed_locks = list(tp.removed_locks.all())
        desired_locks = list(tp.desired_locks.all())
        active = {lk.serial for lk in active_locks}
        planned = {lk.serial for lk in planned_locks}
        removed = {lk.serial for lk in removed_locks}
        desired = {lk.serial for lk in desired_locks}
        group_doors = set()
        for g in tp.groups.all():
            group_doors |= {lk.serial for lk in g.doors.all()}
        # Ist = everything currently programmed or pending; Soll = the wish.
        basis = desired if scope == "soll" else (active | planned | removed)
        individual = basis - group_doors
        if not individual:
            continue
        by_serial = {
            lk.serial: lk
            for lk in active_locks + planned_locks + removed_locks + desired_locks
        }
        doors = []
        for serial in individual:
            lk = by_serial.get(serial)
            if lk is None:
                continue
            state = (
                "active"
                if serial in active
                else "planned"
                if serial in planned
                else "removed"
                if serial in removed
                else "add"
            )
            doors.append({"lock": lk, "state": state})
        doors.sort(key=lambda d: (d["lock"].location or "", d["lock"].label))
        n_grants += len(doors)
        rows.append(
            {
                "tp": tp,
                "groups": list(tp.groups.all()),
                "doors": doors,
                "n": len(doors),
                "n_current": len(basis),
            }
        )
    return render(
        request,
        "access/individual_access.html",
        {
            "rows": rows,
            "n_transponders": len(rows),
            "n_grants": n_grants,
            "scope": scope,
            "nav": "individual",
        },
    )


# --- Soll editing -----------------------------------------------------------


def _json_body(request):
    """Parsed JSON request body, or None if it is not valid JSON."""
    try:
        return json.loads(request.body or b"{}")
    except ValueError, TypeError:
        return None


def _door_sections():
    """All locks grouped by building category (location), ordered."""
    sections = defaultdict(list)
    for lk in Lock.objects.order_by("location", "door_name", "serial"):
        sections[lk.location or "—"].append(lk)
    return sorted(sections.items())


@ensure_csrf_cookie
def soll_matrix(request):
    """Groups × doors matrix (+ optional transponder columns) for editing Soll."""
    groups = list(Group.objects.prefetch_related("doors"))
    tp_serials = [s for s in request.GET.get("tp", "").split(",") if s]
    seen, order = set(), []
    for s in tp_serials:  # de-dup, preserve order
        if s not in seen:
            seen.add(s)
            order.append(s)
    tp_by_serial = {
        t.serial: t
        for t in Transponder.objects.filter(serial__in=order).prefetch_related(
            "groups", "desired_locks"
        )
    }
    tp_cols = [tp_by_serial[s] for s in order if s in tp_by_serial]

    # Read door/desired/group sets from the prefetch caches (.all()), not
    # values_list (which re-queries per group / per column).
    group_doors = {g.id: {lk.serial for lk in g.doors.all()} for g in groups}
    tp_desired = {
        t.serial: {lk.serial for lk in t.desired_locks.all()} for t in tp_cols
    }
    # Doors a transponder gets via its groups — these are inherited (shown
    # distinctly, and not individually toggleable in the editor).
    tp_inherited = {}
    for t in tp_cols:
        inh = set()
        for g in t.groups.all():
            inh |= group_doors.get(g.id, set())
        tp_inherited[t.serial] = inh
    columns = [{"kind": "group", "id": g.id, "name": g.name} for g in groups] + [
        {"kind": "tp", "id": t.serial, "name": t.label} for t in tp_cols
    ]
    sections = []
    for loc, locks in _door_sections():
        rows = []
        for lk in locks:
            cells = [
                {"kind": "group", "id": g.id, "on": lk.serial in group_doors[g.id]}
                for g in groups
            ]
            cells += [
                {
                    "kind": "tp",
                    "id": t.serial,
                    "on": lk.serial in tp_desired[t.serial],
                    "inherited": lk.serial in tp_inherited[t.serial],
                }
                for t in tp_cols
            ]
            rows.append({"lock": lk, "cells": cells})
        sections.append({"location": loc, "rows": rows})

    shown = {t.serial for t in tp_cols}
    tp_options = [
        {"serial": t.serial, "label": t.label}
        for t in Transponder.objects.order_by("asta_number", "person_name", "serial")
        if t.serial not in shown
    ]
    return render(
        request,
        "access/soll_matrix.html",
        {
            "groups": groups,
            "tp_cols": tp_cols,
            "columns": columns,
            "sections": sections,
            "tp_options": tp_options,
            "tp_param": ",".join(t.serial for t in tp_cols),
            "nav": "soll",
        },
    )


@require_POST
def soll_toggle(request):
    """Batch-apply cell toggles. Body: {"ops":[{kind,id,on,locks:[serial]}]}.

    The whole batch runs in one transaction so it is all-or-nothing, matching
    the client's all-or-nothing revert on failure.
    """
    data = _json_body(request)
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        return JsonResponse({"error": "bad request"}, status=400)
    with transaction.atomic():
        for op in data["ops"]:
            locks = list(Lock.objects.filter(serial__in=op.get("locks", [])))
            if not locks:
                continue
            kind, cid, on = op.get("kind"), op.get("id"), bool(op.get("on"))
            try:
                if kind == "group":
                    g = Group.objects.filter(pk=cid).first()
                    if g:
                        soll.set_group_doors(g, locks, on)
                elif kind == "tp":
                    t = Transponder.objects.filter(pk=cid).first()
                    if t:
                        soll.set_desired(t, locks, on)
            except ValueError, TypeError:
                continue  # unparseable id -> skip this op
    return JsonResponse({"ok": True})


@require_POST
def soll_group_assign(request):
    d = _json_body(request)
    if not isinstance(d, dict) or "transponder" not in d or "group" not in d:
        return JsonResponse({"error": "bad request"}, status=400)
    tp = get_object_or_404(Transponder, pk=d["transponder"])
    g = get_object_or_404(Group, pk=d["group"])
    (soll.assign_group if d.get("assigned") else soll.unassign_group)(tp, g)
    return JsonResponse({"ok": True, "desired": tp.desired_locks.count()})


@require_POST
def transponder_soll_action(request, serial):
    tp = get_object_or_404(Transponder, pk=serial)
    d = _json_body(request) or {}
    if d.get("action") == "copy":
        soll.copy_current_to_desired(tp)
    elif d.get("action") == "clear":
        soll.clear_desired(tp)
    return JsonResponse({"ok": True, "desired": tp.desired_locks.count()})


# --- groups -----------------------------------------------------------------


def _group_error(exc):
    message = (
        exc.messages[0]
        if isinstance(exc, ValidationError)
        else "Name oder Export-Code bereits vergeben."
    )
    return JsonResponse({"error": message}, status=400)


@require_POST
@transaction.atomic
def group_create(request):
    data = _json_body(request)
    name = ((data.get("name") if isinstance(data, dict) else None) or "").strip()
    if not name:
        return JsonResponse({"error": "Name erforderlich."}, status=400)
    supplied_value = data.get("export_code", "")
    if not isinstance(supplied_value, str):
        return JsonResponse({"error": "Der Export-Code muss Text sein."}, status=400)
    try:
        supplied = (
            normalize_export_code(supplied_value) if supplied_value.strip() else ""
        )
    except ValidationError as exc:
        return _group_error(exc)
    list(Group.objects.select_for_update().values_list("pk", flat=True))
    existing = Group.objects.filter(name=name).first()
    if existing is not None:
        if supplied and supplied != existing.export_code:
            return JsonResponse(
                {"error": "Diese Gruppe hat bereits einen anderen Export-Code."},
                status=400,
            )
        return JsonResponse(
            {
                "ok": True,
                "id": existing.pk,
                "name": existing.name,
                "export_code": existing.export_code,
            }
        )
    try:
        code = (
            supplied
            if supplied
            else derive_export_code(
                name, Group.objects.values_list("export_code", flat=True)
            )
        )
        group = Group(name=name, export_code=code)
        group.full_clean()
        group.save()
    except (ValidationError, IntegrityError) as exc:
        return _group_error(exc)
    return JsonResponse(
        {
            "ok": True,
            "id": group.pk,
            "name": group.name,
            "export_code": group.export_code,
        }
    )


@require_POST
def group_rename(request, pk):
    g = get_object_or_404(Group, pk=pk)
    d = _json_body(request) or {}
    name = (d.get("name") or "").strip()
    if name:
        g.name = name
        try:
            g.full_clean()
            g.save(update_fields=["name"])
        except (ValidationError, IntegrityError) as exc:
            return _group_error(exc)
    return JsonResponse({"ok": True, "name": g.name})


@require_POST
def group_metadata(request, pk):
    group = get_object_or_404(Group, pk=pk)
    data = _json_body(request)
    if not isinstance(data, dict) or not isinstance(data.get("is_implicit"), bool):
        return JsonResponse({"error": "Ungültige Gruppenmetadaten."}, status=400)
    try:
        updated = set_group_export_metadata(
            group,
            export_code=data.get("export_code", ""),
            is_implicit=data["is_implicit"],
        )
    except (ValidationError, IntegrityError) as exc:
        return _group_error(exc)
    return JsonResponse(
        {
            "ok": True,
            "export_code": updated.export_code,
            "is_implicit": updated.is_implicit,
        }
    )


@require_POST
def group_delete(request, pk):
    get_object_or_404(Group, pk=pk).delete()
    return JsonResponse({"ok": True})


@ensure_csrf_cookie
def group_list(request):
    # distinct=True: without it the two M2M joins cross-multiply and both
    # counts collapse to doors×transponders.
    groups = Group.objects.annotate(
        n=Count("doors", distinct=True), m=Count("transponders", distinct=True)
    ).order_by("name")
    return render(request, "access/group_list.html", {"groups": groups, "nav": "soll"})


@ensure_csrf_cookie
def group_detail(request, pk):
    group = get_object_or_404(Group, pk=pk)
    in_group = set(group.doors.values_list("serial", flat=True))
    sections = []
    for loc, locks in _door_sections():
        sections.append(
            {
                "location": loc,
                "rows": [{"lock": lk, "on": lk.serial in in_group} for lk in locks],
            }
        )
    return render(
        request,
        "access/group_detail.html",
        {
            "group": group,
            "sections": sections,
            "members": group.transponders.order_by(
                "asta_number", "person_name", "serial"
            ),
            "nav": "soll",
        },
    )
