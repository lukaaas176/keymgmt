"""Editing logic for the desired ("Soll") state.

The stored truth is ``Transponder.desired_locks``. Groups are reusable
door-sets whose membership keeps that set in sync:

* assigning a group adds its doors to the transponder's desired set;
* unassigning removes them, except doors still covered by another of the
  transponder's groups;
* editing a group's doors propagates to every member the same way;
* an individual door toggle edits ``desired_locks`` directly.

All operations are atomic and idempotent. Callers pass Lock/Group/Transponder
instances or serials; the batch helpers exist so a click-drag paints many
cells of one column in a single request.

Provenance caveat: ``desired_locks`` is a flat set with no per-door source,
so the two grant sources (group membership and individual toggles) are
indistinguishable once written. Unassigning a group removes its doors unless
another *group* still covers them — a door that was *individually* added and
also happens to belong to the unassigned group is removed. In this
group-centric workflow the overlap is rare; if it matters, add door
provenance (a through-model on ``desired_locks``) rather than heuristics here.
"""

from __future__ import annotations

from django.db import transaction

from .models import Group, Lock, Transponder


def _as_locks(locks) -> list[Lock]:
    """Accept Lock instances or serials; return a list of Lock instances."""
    locks = list(locks)
    if locks and isinstance(locks[0], str):
        return list(Lock.objects.filter(pk__in=locks))
    return locks


def _other_group_doors(tp: Transponder, exclude: Group) -> set[str]:
    """Serials desired via the transponder's *other* groups."""
    doors: set[str] = set()
    for g in tp.groups.exclude(pk=exclude.pk).prefetch_related("doors"):
        doors.update(g.doors.values_list("serial", flat=True))
    return doors


@transaction.atomic
def set_desired(tp: Transponder, locks, wished: bool) -> None:
    """Add/remove locks in a transponder's desired set (individual edit)."""
    locks = _as_locks(locks)
    if not locks:
        return
    (tp.desired_locks.add if wished else tp.desired_locks.remove)(*locks)


@transaction.atomic
def set_group_doors(group: Group, locks, included: bool) -> None:
    """Add/remove locks in a group's door-set, propagating to its members."""
    locks = _as_locks(locks)
    if not locks:
        return
    if included:
        group.doors.add(*locks)
        for tp in group.transponders.all():
            tp.desired_locks.add(*locks)
    else:
        group.doors.remove(*locks)
        for tp in group.transponders.all():
            keep = _other_group_doors(tp, group)
            drop = [lk for lk in locks if lk.serial not in keep]
            if drop:
                tp.desired_locks.remove(*drop)


@transaction.atomic
def assign_group(tp: Transponder, group: Group) -> None:
    tp.groups.add(group)
    tp.desired_locks.add(*group.doors.all())


@transaction.atomic
def unassign_group(tp: Transponder, group: Group) -> None:
    tp.groups.remove(group)
    keep = _other_group_doors(tp, group)
    drop = [lk for lk in group.doors.all() if lk.serial not in keep]
    if drop:
        tp.desired_locks.remove(*drop)


@transaction.atomic
def copy_current_to_desired(tp: Transponder) -> None:
    """Set desired = active ∪ planned (the 'copy current rights' shortcut).

    This makes the Soll an explicit, group-independent snapshot, so the
    transponder's group memberships are cleared too — otherwise a group would
    still claim doors the new desired set may no longer contain, and a later
    unassign of that group could strip a just-copied real right.
    """
    tp.desired_locks.set(list(tp.locks.all()) + list(tp.planned_locks.all()))
    tp.groups.clear()


@transaction.atomic
def clear_desired(tp: Transponder) -> None:
    tp.desired_locks.clear()
    tp.groups.clear()
