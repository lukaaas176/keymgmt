from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction

if TYPE_CHECKING:
    from .models import Group


CODE_RE = re.compile(r"^[A-Z0-9]{1,4}\Z")


def _ascii_upper(value: str) -> str:
    return (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .upper()
    )


def normalize_export_code(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("Der Export-Code muss Text sein.")
    code = _ascii_upper(value.strip())
    if not CODE_RE.fullmatch(code):
        raise ValidationError(
            "Der Export-Code muss 1-4 Buchstaben oder Ziffern enthalten."
        )
    return code


def derive_export_code(name: str, used_codes: Iterable[str]) -> str:
    semantic_name = re.sub(r"^AStA\s+", "", name.strip(), flags=re.IGNORECASE)
    stem = "".join(char for char in _ascii_upper(semantic_name) if char.isalnum())
    occupied = {code.upper() for code in used_codes}
    for length in range(1, min(4, len(stem)) + 1):
        candidate = stem[:length]
        if candidate not in occupied:
            return candidate
    raise ValidationError(
        "Kein eindeutiger Export-Code ableitbar; bitte einen eigenen "
        "Export-Code angeben."
    )


def combined_group_label(groups: Iterable[Group]) -> str:
    ordered = sorted(groups, key=lambda group: str(group.name).casefold())
    if not ordered:
        return ""
    displayed = (
        [group for group in ordered if not group.is_implicit]
        if len(ordered) > 1
        else ordered
    )
    codes = [str(group.export_code) for group in displayed]
    suffix = (
        "".join(codes) if all(len(code) == 1 for code in codes) else "-".join(codes)
    )
    return f"AStA {suffix}"


@transaction.atomic
def set_group_export_metadata(
    group: Group, *, export_code: str, is_implicit: bool
) -> Group:
    from .models import Group

    list(Group.objects.select_for_update().values_list("pk", flat=True))
    locked = Group.objects.get(pk=group.pk)
    if is_implicit:
        Group.objects.filter(is_implicit=True).exclude(pk=locked.pk).update(
            is_implicit=False
        )
    locked.export_code = normalize_export_code(export_code)
    locked.is_implicit = is_implicit
    locked.full_clean()
    locked.save(update_fields=["export_code", "is_implicit"])
    return locked
