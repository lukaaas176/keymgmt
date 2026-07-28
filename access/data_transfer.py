from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.timezone import is_aware

from .group_labels import normalize_export_code
from .models import Group, Lock, Transponder

FORMAT_NAME = "schliessmatrix-backup"
FORMAT_VERSION = 1
MAX_BACKUP_BYTES = 10 * 1024 * 1024

ROOT_KEYS = {"format", "version", "exported_at", "locks", "groups", "transponders"}
LOCK_KEYS = {"serial", "door_name", "room_number", "location", "area"}
GROUP_KEYS = {"name", "export_code", "is_implicit", "doors"}
TRANSPONDER_KEYS = {
    "serial",
    "asta_number",
    "person_name",
    "locking_system",
    "printed_on",
    "source_file",
    "imported_at",
    "active_locks",
    "planned_locks",
    "removed_locks",
    "desired_locks",
    "groups",
}


class BackupValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ImportResult:
    mode: Literal["merge", "replace"]
    created_locks: int
    updated_locks: int
    created_groups: int
    updated_groups: int
    created_transponders: int
    updated_transponders: int


def _datetime_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@transaction.atomic
def build_backup(*, exported_at: datetime | None = None) -> dict[str, object]:
    locks = list(Lock.objects.order_by("serial"))
    groups = list(Group.objects.prefetch_related("doors").order_by("export_code"))
    transponders = list(
        Transponder.objects.prefetch_related(
            "locks", "planned_locks", "removed_locks", "desired_locks", "groups"
        ).order_by("serial")
    )
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": _datetime_text(exported_at or timezone.now()),
        "locks": [
            {
                "serial": lock.serial,
                "door_name": lock.door_name,
                "room_number": lock.room_number,
                "location": lock.location,
                "area": lock.area,
            }
            for lock in locks
        ],
        "groups": [
            {
                "name": group.name,
                "export_code": group.export_code,
                "is_implicit": group.is_implicit,
                "doors": sorted(lock.serial for lock in group.doors.all()),
            }
            for group in groups
        ],
        "transponders": [
            {
                "serial": transponder.serial,
                "asta_number": transponder.asta_number,
                "person_name": transponder.person_name,
                "locking_system": transponder.locking_system,
                "printed_on": (
                    transponder.printed_on.isoformat()
                    if transponder.printed_on
                    else None
                ),
                "source_file": transponder.source_file,
                "imported_at": _datetime_text(transponder.imported_at),
                "active_locks": sorted(lock.serial for lock in transponder.locks.all()),
                "planned_locks": sorted(
                    lock.serial for lock in transponder.planned_locks.all()
                ),
                "removed_locks": sorted(
                    lock.serial for lock in transponder.removed_locks.all()
                ),
                "desired_locks": sorted(
                    lock.serial for lock in transponder.desired_locks.all()
                ),
                "groups": sorted(
                    group.export_code for group in transponder.groups.all()
                ),
            }
            for transponder in transponders
        ],
    }


def encode_backup(*, exported_at: datetime | None = None) -> bytes:
    return json.dumps(
        build_backup(exported_at=exported_at),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupValidationError(f"Doppeltes Feld: {key}.")
        result[key] = value
    return result


def _require_object(value, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise BackupValidationError(f"{label} muss ein Objekt sein.")
    missing = keys - value.keys()
    unknown = value.keys() - keys
    if missing:
        raise BackupValidationError(f"{label}: Pflichtfeld fehlt: {min(missing)}.")
    if unknown:
        raise BackupValidationError(f"{label}: Unbekanntes Feld: {min(unknown)}.")
    return value


def _require_list(value, label: str) -> list:
    if not isinstance(value, list):
        raise BackupValidationError(f"{label} muss eine Liste sein.")
    return value


def _string(value, label: str, max_length: int, *, blank: bool = True) -> str:
    if not isinstance(value, str):
        raise BackupValidationError(f"{label} muss Text sein.")
    if not blank and not value:
        raise BackupValidationError(f"{label} darf nicht leer sein.")
    if len(value) > max_length:
        raise BackupValidationError(
            f"{label} darf höchstens {max_length} Zeichen enthalten."
        )
    return value


def _code(value, label: str) -> str:
    try:
        return normalize_export_code(value)
    except ValidationError as exc:
        raise BackupValidationError(f"{label}: {exc.messages[0]}") from exc


def _date(value, label: str):
    if value is None:
        return None
    if not isinstance(value, str) or (parsed := parse_date(value)) is None:
        raise BackupValidationError(f"{label} ist kein gültiges ISO-Datum.")
    return parsed


def _datetime(value, label: str) -> datetime:
    if not isinstance(value, str) or (parsed := parse_datetime(value)) is None:
        raise BackupValidationError(f"{label} ist kein gültiger ISO-Zeitpunkt.")
    if not is_aware(parsed):
        raise BackupValidationError(f"{label} muss eine Zeitzone enthalten.")
    return parsed


def _identity_list(
    value,
    label: str,
    max_length: int,
    *,
    codes: bool = False,
) -> list[str]:
    items = _require_list(value, label)
    normalized = []
    seen = set()
    for item in items:
        normalized_item = (
            _code(item, label)
            if codes
            else _string(item, label, max_length, blank=False)
        )
        key = normalized_item.casefold() if codes else normalized_item
        if key in seen:
            raise BackupValidationError(f"{label} enthält doppelte Einträge.")
        seen.add(key)
        normalized.append(normalized_item)
    return normalized


def _parse_lock(raw, index: int) -> dict:
    record = _require_object(raw, LOCK_KEYS, f"Tür {index}")
    return {
        "serial": _string(record["serial"], f"Tür {index}.serial", 32, blank=False),
        "door_name": _string(record["door_name"], f"Tür {index}.door_name", 255),
        "room_number": _string(record["room_number"], f"Tür {index}.room_number", 64),
        "location": _string(record["location"], f"Tür {index}.location", 64),
        "area": _string(record["area"], f"Tür {index}.area", 64),
    }


def _parse_group(raw, index: int) -> dict:
    record = _require_object(raw, GROUP_KEYS, f"Gruppe {index}")
    if not isinstance(record["is_implicit"], bool):
        raise BackupValidationError(
            f"Gruppe {index}.is_implicit muss wahr/falsch sein."
        )
    return {
        "name": _string(record["name"], f"Gruppe {index}.name", 128, blank=False),
        "export_code": _code(record["export_code"], f"Gruppe {index}.export_code"),
        "is_implicit": record["is_implicit"],
        "doors": _identity_list(record["doors"], f"Gruppe {index}.doors", 32),
    }


def _parse_transponder(raw, index: int) -> dict:
    record = _require_object(raw, TRANSPONDER_KEYS, f"Transponder {index}")
    asta_number = record["asta_number"]
    if asta_number is not None and (
        not isinstance(asta_number, int) or isinstance(asta_number, bool)
    ):
        raise BackupValidationError(
            f"Transponder {index}.asta_number muss eine Zahl oder null sein."
        )
    if asta_number is not None and not -(2**31) <= asta_number < 2**31:
        raise BackupValidationError(
            f"Transponder {index}.asta_number liegt außerhalb des gültigen Bereichs."
        )
    prefix = f"Transponder {index}"
    return {
        "serial": _string(record["serial"], f"{prefix}.serial", 32, blank=False),
        "asta_number": asta_number,
        "person_name": _string(record["person_name"], f"{prefix}.person_name", 255),
        "locking_system": _string(
            record["locking_system"], f"{prefix}.locking_system", 64
        ),
        "printed_on": _date(record["printed_on"], f"{prefix}.printed_on"),
        "source_file": _string(record["source_file"], f"{prefix}.source_file", 255),
        "imported_at": _datetime(record["imported_at"], f"{prefix}.imported_at"),
        "active_locks": _identity_list(
            record["active_locks"], f"{prefix}.active_locks", 32
        ),
        "planned_locks": _identity_list(
            record["planned_locks"], f"{prefix}.planned_locks", 32
        ),
        "removed_locks": _identity_list(
            record["removed_locks"], f"{prefix}.removed_locks", 32
        ),
        "desired_locks": _identity_list(
            record["desired_locks"], f"{prefix}.desired_locks", 32
        ),
        "groups": _identity_list(record["groups"], f"{prefix}.groups", 4, codes=True),
    }


def _reject_duplicate(records: list[dict], field: str, label: str, *, fold=False):
    seen = set()
    for record in records:
        value = record[field]
        key = value.casefold() if fold else value
        if key in seen:
            raise BackupValidationError(f"{label} ist doppelt: {value}.")
        seen.add(key)


def _validate_imported_references(backup: dict) -> None:
    lock_serials = {record["serial"] for record in backup["locks"]}
    group_codes = {record["export_code"] for record in backup["groups"]}
    lock_fields = (
        "active_locks",
        "planned_locks",
        "removed_locks",
        "desired_locks",
    )
    for group in backup["groups"]:
        missing = set(group["doors"]) - lock_serials
        if missing:
            raise BackupValidationError(f"Unbekannte Türreferenz: {min(missing)}.")
    for transponder in backup["transponders"]:
        for field in lock_fields:
            missing = set(transponder[field]) - lock_serials
            if missing:
                raise BackupValidationError(f"Unbekannte Türreferenz: {min(missing)}.")
        missing_groups = set(transponder["groups"]) - group_codes
        if missing_groups:
            raise BackupValidationError(
                f"Unbekannte Gruppenreferenz: {min(missing_groups)}."
            )


def parse_backup(
    content: bytes, *, allow_external_references: bool = False
) -> dict[str, object]:
    if not content:
        raise BackupValidationError("Die Sicherungsdatei ist leer.")
    if len(content) > MAX_BACKUP_BYTES:
        raise BackupValidationError("Die Sicherungsdatei ist größer als 10 MiB.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupValidationError(
            "Die Sicherungsdatei ist nicht UTF-8-kodiert."
        ) from exc
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
    except BackupValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise BackupValidationError(
            "Die Sicherungsdatei enthält ungültiges JSON."
        ) from exc

    root = _require_object(raw, ROOT_KEYS, "Sicherung")
    if root["format"] != FORMAT_NAME:
        raise BackupValidationError("Die Datei ist keine Schließmatrix-Sicherung.")
    if root["version"] != FORMAT_VERSION or isinstance(root["version"], bool):
        raise BackupValidationError("Diese Sicherungsversion wird nicht unterstützt.")

    backup = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": _datetime(root["exported_at"], "exported_at"),
        "locks": [
            _parse_lock(record, index)
            for index, record in enumerate(_require_list(root["locks"], "locks"), 1)
        ],
        "groups": [
            _parse_group(record, index)
            for index, record in enumerate(_require_list(root["groups"], "groups"), 1)
        ],
        "transponders": [
            _parse_transponder(record, index)
            for index, record in enumerate(
                _require_list(root["transponders"], "transponders"), 1
            )
        ],
    }
    _reject_duplicate(backup["locks"], "serial", "Tür-Seriennummer")
    _reject_duplicate(backup["groups"], "export_code", "Gruppen-Export-Code", fold=True)
    _reject_duplicate(backup["groups"], "name", "Gruppenname", fold=True)
    _reject_duplicate(backup["transponders"], "serial", "Transponder-Seriennummer")
    if sum(group["is_implicit"] for group in backup["groups"]) > 1:
        raise BackupValidationError("Es darf nur eine implizite Gruppe geben.")
    if not allow_external_references:
        _validate_imported_references(backup)
    return backup


def _set_relationships(
    backup: dict,
    locks_by_serial: dict[str, Lock],
    groups_by_code: dict[str, Group],
    transponders_by_serial: dict[str, Transponder],
) -> None:
    for record in backup["groups"]:
        group = groups_by_code[record["export_code"]]
        group.doors.set(locks_by_serial[serial] for serial in record["doors"])

    lock_fields = {
        "active_locks": "locks",
        "planned_locks": "planned_locks",
        "removed_locks": "removed_locks",
        "desired_locks": "desired_locks",
    }
    for record in backup["transponders"]:
        transponder = transponders_by_serial[record["serial"]]
        for source_field, relation_name in lock_fields.items():
            getattr(transponder, relation_name).set(
                locks_by_serial[serial] for serial in record[source_field]
            )
        transponder.groups.set(groups_by_code[code] for code in record["groups"])


def _replace_backup(backup: dict) -> ImportResult:
    Transponder.objects.all().delete()
    Group.objects.all().delete()
    Lock.objects.all().delete()

    locks_by_serial = {}
    for record in backup["locks"]:
        lock = Lock.objects.create(**record)
        locks_by_serial[lock.serial] = lock

    groups_by_code = {}
    for record in backup["groups"]:
        group = Group.objects.create(
            name=record["name"],
            export_code=record["export_code"],
            is_implicit=record["is_implicit"],
        )
        groups_by_code[group.export_code] = group

    transponders_by_serial = {}
    for record in backup["transponders"]:
        transponder = Transponder.objects.create(
            serial=record["serial"],
            asta_number=record["asta_number"],
            person_name=record["person_name"],
            locking_system=record["locking_system"],
            printed_on=record["printed_on"],
            source_file=record["source_file"],
        )
        Transponder.objects.filter(pk=transponder.pk).update(
            imported_at=record["imported_at"]
        )
        transponder.imported_at = record["imported_at"]
        transponders_by_serial[transponder.serial] = transponder

    _set_relationships(backup, locks_by_serial, groups_by_code, transponders_by_serial)
    return ImportResult(
        mode="replace",
        created_locks=len(locks_by_serial),
        updated_locks=0,
        created_groups=len(groups_by_code),
        updated_groups=0,
        created_transponders=len(transponders_by_serial),
        updated_transponders=0,
    )


def _validate_merge_references(
    backup: dict,
    locks_by_serial: dict[str, Lock],
    groups_by_code: dict[str, Group],
    groups_by_name: dict[str, Group],
) -> None:
    available_locks = set(locks_by_serial) | {
        record["serial"] for record in backup["locks"]
    }
    available_groups = set(groups_by_code) | {
        record["export_code"] for record in backup["groups"]
    }
    for record in backup["groups"]:
        by_code = groups_by_code.get(record["export_code"])
        by_name = groups_by_name.get(record["name"].casefold())
        if by_name is not None and (by_code is None or by_name.pk != by_code.pk):
            raise BackupValidationError(
                f"Der Gruppenname {record['name']} gehört zu einem anderen Export-Code."
            )
        missing = set(record["doors"]) - available_locks
        if missing:
            raise BackupValidationError(f"Unbekannte Türreferenz: {min(missing)}.")

    lock_fields = (
        "active_locks",
        "planned_locks",
        "removed_locks",
        "desired_locks",
    )
    for record in backup["transponders"]:
        for field in lock_fields:
            missing = set(record[field]) - available_locks
            if missing:
                raise BackupValidationError(f"Unbekannte Türreferenz: {min(missing)}.")
        missing_groups = set(record["groups"]) - available_groups
        if missing_groups:
            raise BackupValidationError(
                f"Unbekannte Gruppenreferenz: {min(missing_groups)}."
            )


def _merge_backup(backup: dict) -> ImportResult:
    existing_locks = list(Lock.objects.select_for_update())
    existing_groups = list(Group.objects.select_for_update())
    existing_transponders = list(Transponder.objects.select_for_update())
    locks_by_serial = {lock.serial: lock for lock in existing_locks}
    groups_by_code = {group.export_code.upper(): group for group in existing_groups}
    groups_by_name = {group.name.casefold(): group for group in existing_groups}
    transponders_by_serial = {
        transponder.serial: transponder for transponder in existing_transponders
    }
    _validate_merge_references(backup, locks_by_serial, groups_by_code, groups_by_name)

    created_locks = updated_locks = 0
    for record in backup["locks"]:
        lock = locks_by_serial.get(record["serial"])
        if lock is None:
            lock = Lock.objects.create(**record)
            created_locks += 1
        else:
            for field in ("door_name", "room_number", "location", "area"):
                setattr(lock, field, record[field])
            lock.save(update_fields=["door_name", "room_number", "location", "area"])
            updated_locks += 1
        locks_by_serial[lock.serial] = lock

    if any(record["is_implicit"] for record in backup["groups"]):
        Group.objects.filter(is_implicit=True).update(is_implicit=False)
        for group in existing_groups:
            group.is_implicit = False

    created_groups = updated_groups = 0
    for record in backup["groups"]:
        group = groups_by_code.get(record["export_code"])
        if group is None:
            group = Group.objects.create(
                name=record["name"],
                export_code=record["export_code"],
                is_implicit=record["is_implicit"],
            )
            created_groups += 1
        else:
            group.name = record["name"]
            group.export_code = record["export_code"]
            group.is_implicit = record["is_implicit"]
            group.full_clean()
            group.save(update_fields=["name", "export_code", "is_implicit"])
            updated_groups += 1
        groups_by_code[group.export_code] = group

    created_transponders = updated_transponders = 0
    for record in backup["transponders"]:
        transponder = transponders_by_serial.get(record["serial"])
        scalar_fields = (
            "asta_number",
            "person_name",
            "locking_system",
            "printed_on",
            "source_file",
        )
        if transponder is None:
            transponder = Transponder.objects.create(
                serial=record["serial"],
                **{field: record[field] for field in scalar_fields},
            )
            created_transponders += 1
        else:
            for field in scalar_fields:
                setattr(transponder, field, record[field])
            transponder.save(update_fields=list(scalar_fields))
            updated_transponders += 1
        Transponder.objects.filter(pk=transponder.pk).update(
            imported_at=record["imported_at"]
        )
        transponder.imported_at = record["imported_at"]
        transponders_by_serial[transponder.serial] = transponder

    _set_relationships(backup, locks_by_serial, groups_by_code, transponders_by_serial)
    return ImportResult(
        mode="merge",
        created_locks=created_locks,
        updated_locks=updated_locks,
        created_groups=created_groups,
        updated_groups=updated_groups,
        created_transponders=created_transponders,
        updated_transponders=updated_transponders,
    )


def restore_backup(
    content: bytes, *, mode: str, replace_confirmed: bool = False
) -> ImportResult:
    if mode not in {"merge", "replace"}:
        raise BackupValidationError("Unbekannter Importmodus.")
    if mode == "replace" and not replace_confirmed:
        raise BackupValidationError(
            "Das Ersetzen aller Daten muss ausdrücklich bestätigt werden."
        )
    backup = parse_backup(content, allow_external_references=mode == "merge")
    with transaction.atomic():
        if mode == "replace":
            return _replace_backup(backup)
        return _merge_backup(backup)
