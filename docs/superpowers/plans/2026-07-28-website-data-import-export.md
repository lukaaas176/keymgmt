# Website Data Import And Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an authenticated website workflow that exports all Schliessmatrix domain data as versioned JSON and imports it with either exact replacement or imported-record-wins merge semantics.

**Architecture:** A focused `access/data_transfer.py` module owns the format, strict validation, serialization, and transactional restore operations. Thin Django views expose download and upload endpoints, while one responsive template explains both import modes and uses the existing message system for results.

**Tech Stack:** Python 3.14, Django 6, SQLite, Django templates, Tailwind Play CDN, Alpine.js, Django `TestCase`.

## Global Constraints

- Work directly in the current `main` worktree and preserve all existing changes.
- Do not add dependencies or migrations.
- Export domain data only; never include users, password hashes, sessions, permissions, content types, migrations, or configuration.
- Allow every authenticated user to import and export through the existing `LoginRequiredMiddleware`.
- Use a strict UTF-8 JSON format named `schliessmatrix-backup`, schema version `1`.
- Reject uploads over 10 MiB before JSON parsing.
- Validate all content and retained-record references before the first mutation.
- Run each restore in one `transaction.atomic()` block; failures must leave domain data unchanged.
- Keep existing PDF/image import and PDF report export behavior unchanged.
- Use German website copy and preserve the existing visual language on desktop and mobile.
- Do not commit unless the user explicitly asks.

---

## File Structure

- Create `access/data_transfer.py`: constants, typed result/error objects, deterministic export, strict parser, validation, replace, and merge logic.
- Create `access/test_data_transfer.py`: focused domain and HTTP tests for the feature.
- Create `access/templates/access/data_transfer.html`: authenticated export/import page.
- Modify `access/views.py`: thin page, export, and import views.
- Modify `access/urls.py`: three data-transfer routes.
- Modify `access/templates/access/base.html`: desktop and mobile `Daten` navigation links.
- Modify `README.md`: document the portable backup workflow and distinguish it from PDF import/export.

---

### Task 1: Deterministic Domain Export

**Files:**
- Create: `access/data_transfer.py`
- Create: `access/test_data_transfer.py`

**Interfaces:**
- Produces: `FORMAT_NAME = "schliessmatrix-backup"`
- Produces: `FORMAT_VERSION = 1`
- Produces: `MAX_BACKUP_BYTES = 10 * 1024 * 1024`
- Produces: `build_backup(*, exported_at: datetime | None = None) -> dict[str, object]`
- Produces: `encode_backup(*, exported_at: datetime | None = None) -> bytes`

- [ ] **Step 1: Write a complete export fixture and failing schema test**

Create `DataTransferExportTests.setUp()` with two locks, two groups, and one
transponder. Populate every scalar field and every relationship:

```python
from datetime import date, datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from . import data_transfer
from .models import Group, Lock, Transponder


class DataTransferExportTests(TestCase):
    def setUp(self):
        self.lock_a = Lock.objects.create(
            serial="DC-1",
            door_name="Eingang",
            room_number="0.01",
            location="MUC.1.EG",
            area="Allgemein",
        )
        self.lock_b = Lock.objects.create(serial="DC-2", door_name="Lager")
        self.base = Group.objects.create(
            name="AStA Allgemein", export_code="A", is_implicit=True
        )
        self.store = Group.objects.create(name="AStA Lager", export_code="L")
        self.base.doors.add(self.lock_a)
        self.store.doors.add(self.lock_b)
        self.tp = Transponder.objects.create(
            serial="T-1",
            asta_number=7,
            person_name="Muster, Mia",
            locking_system="Anlage 1",
            printed_on=date(2026, 7, 20),
            source_file="matrix.pdf",
        )
        self.tp.locks.add(self.lock_a)
        self.tp.planned_locks.add(self.lock_b)
        self.tp.removed_locks.add(self.lock_b)
        self.tp.desired_locks.add(self.lock_a, self.lock_b)
        self.tp.groups.add(self.base, self.store)

    def test_build_backup_contains_all_domain_fields_and_relationships(self):
        exported_at = datetime(2026, 7, 28, 12, 0, tzinfo=dt_timezone.utc)
        backup = data_transfer.build_backup(exported_at=exported_at)

        self.assertEqual(backup["format"], "schliessmatrix-backup")
        self.assertEqual(backup["version"], 1)
        self.assertEqual(backup["exported_at"], "2026-07-28T12:00:00Z")
        self.assertEqual(backup["locks"][0], {
            "serial": "DC-1", "door_name": "Eingang", "room_number": "0.01",
            "location": "MUC.1.EG", "area": "Allgemein",
        })
        self.assertEqual(backup["groups"][0]["doors"], ["DC-1"])
        row = backup["transponders"][0]
        self.assertEqual(row["printed_on"], "2026-07-20")
        self.assertEqual(row["active_locks"], ["DC-1"])
        self.assertEqual(row["planned_locks"], ["DC-2"])
        self.assertEqual(row["removed_locks"], ["DC-2"])
        self.assertEqual(row["desired_locks"], ["DC-1", "DC-2"])
        self.assertEqual(row["groups"], ["A", "L"])
```

- [ ] **Step 2: Run the focused test and verify the missing module failure**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferExportTests.test_build_backup_contains_all_domain_fields_and_relationships`

Expected: ERROR because `access.data_transfer` does not exist.

- [ ] **Step 3: Implement constants and deterministic serialization**

In `access/data_transfer.py`, use explicit field maps rather than Django's
fixture serializer. Sort locks and transponders by serial and groups by
case-folded export code. Sort every relationship identifier list. Normalize UTC
datetimes to a trailing `Z`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from .models import Group, Lock, Transponder

FORMAT_NAME = "schliessmatrix-backup"
FORMAT_VERSION = 1
MAX_BACKUP_BYTES = 10 * 1024 * 1024


def _datetime_text(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


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
                "serial": tp.serial,
                "asta_number": tp.asta_number,
                "person_name": tp.person_name,
                "locking_system": tp.locking_system,
                "printed_on": tp.printed_on.isoformat() if tp.printed_on else None,
                "source_file": tp.source_file,
                "imported_at": _datetime_text(tp.imported_at),
                "active_locks": sorted(lock.serial for lock in tp.locks.all()),
                "planned_locks": sorted(lock.serial for lock in tp.planned_locks.all()),
                "removed_locks": sorted(lock.serial for lock in tp.removed_locks.all()),
                "desired_locks": sorted(lock.serial for lock in tp.desired_locks.all()),
                "groups": sorted(group.export_code for group in tp.groups.all()),
            }
            for tp in transponders
        ],
    }


def encode_backup(*, exported_at: datetime | None = None) -> bytes:
    return json.dumps(
        build_backup(exported_at=exported_at),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
```

- [ ] **Step 4: Add deterministic-order, auth-exclusion, and query-bound tests**

Assert serial/code ordering, sorted relationship arrays, valid UTF-8 JSON, and
that creating a Django user never introduces an auth key or username into the
backup. Use `CaptureQueriesContext(connection)` to capture one export, add five
records, capture a second export, and assert both captures have the same length.
This tests the fixed prefetch shape without coupling the test to savepoint
queries added by Django's `TestCase` transaction wrapper.

- [ ] **Step 5: Run export tests**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferExportTests`

Expected: all export tests PASS.

---

### Task 2: Strict JSON And Domain Validation

**Files:**
- Modify: `access/data_transfer.py`
- Modify: `access/test_data_transfer.py`

**Interfaces:**
- Consumes: `FORMAT_NAME`, `FORMAT_VERSION`, `MAX_BACKUP_BYTES`
- Produces: `class BackupValidationError(ValueError)` with a German user-facing message
- Produces: `parse_backup(content: bytes, *, allow_external_references: bool = False) -> dict[str, object]`
- Guarantees: returned records have normalized uppercase group codes and parsed `date`/`datetime` values

- [ ] **Step 1: Write failing strict-format tests**

Add `DataTransferValidationTests` with a minimal valid backup factory. Cover:

```python
def minimal_backup(self):
    return {
        "format": "schliessmatrix-backup",
        "version": 1,
        "exported_at": "2026-07-28T12:00:00Z",
        "locks": [{
            "serial": "DC-1", "door_name": "Eingang", "room_number": "",
            "location": "", "area": "",
        }],
        "groups": [{
            "name": "Allgemein", "export_code": "A", "is_implicit": True,
            "doors": ["DC-1"],
        }],
        "transponders": [{
            "serial": "T-1", "asta_number": 1, "person_name": "Mia",
            "locking_system": "", "printed_on": None, "source_file": "",
            "imported_at": "2026-07-28T10:00:00Z",
            "active_locks": ["DC-1"], "planned_locks": [],
            "removed_locks": [], "desired_locks": ["DC-1"], "groups": ["A"],
        }],
    }
```

Write individual tests for invalid UTF-8, empty JSON, malformed JSON, duplicate
object keys, wrong format, versions `0` and `2`, unknown/missing keys, wrong
types, overlong strings, invalid/null-required fields, malformed dates and
datetimes, non-uppercase code normalization, duplicate serials/codes/names,
duplicate relationship values, multiple implicit groups, and missing lock/group
references in replace mode.

- [ ] **Step 2: Run validation tests and verify failure**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferValidationTests`

Expected: FAIL because `parse_backup` and `BackupValidationError` do not exist.

- [ ] **Step 3: Implement duplicate-safe JSON parsing and exact key checks**

Use an `object_pairs_hook` that rejects duplicate object keys, exact key sets for
the root and each record type, and these helpers:

```python
class BackupValidationError(ValueError):
    pass


ROOT_KEYS = {"format", "version", "exported_at", "locks", "groups", "transponders"}
LOCK_KEYS = {"serial", "door_name", "room_number", "location", "area"}
GROUP_KEYS = {"name", "export_code", "is_implicit", "doors"}
TRANSPONDER_KEYS = {
    "serial", "asta_number", "person_name", "locking_system", "printed_on",
    "source_file", "imported_at", "active_locks", "planned_locks",
    "removed_locks", "desired_locks", "groups",
}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupValidationError(f"Doppeltes Feld: {key}.")
        result[key] = value
    return result
```

Reject `len(content) == 0` and `len(content) > MAX_BACKUP_BYTES` before decode.
Decode with strict UTF-8, parse with `json.loads(..., object_pairs_hook=...)`, and
map `UnicodeDecodeError`, `json.JSONDecodeError`, and non-object roots to concise
German messages.

- [ ] **Step 4: Implement scalar, identity, date, and relationship validation**

Use model field limits exactly: lock serial 32, door name 255, room/location/area
64; group name 128 and normalized code 1-4; transponder serial 32, person name
255, locking system 64, source file 255. Reject booleans as integers for
`asta_number`. Parse dates with `django.utils.dateparse.parse_date`, datetimes
with `parse_datetime`, and require timezone-aware `imported_at` and
`exported_at` values.

Normalize group codes using `normalize_export_code()`. Build case-folded sets to
reject duplicate group names and codes. Build identity maps and validate every
relationship list as a unique list of strings. With the default
`allow_external_references=False`, `parse_backup()` validates references against
imported identities only. With `True`, it defers missing-reference rejection to
Task 4's transaction-scoped retained-record validator while still validating
the relationship list's type and uniqueness.

- [ ] **Step 5: Add exact 10 MiB boundary tests**

Patch `MAX_BACKUP_BYTES` to a small value in tests. Assert content of exactly
the limit reaches JSON parsing and content one byte over raises
`BackupValidationError` containing `10 MiB`.

- [ ] **Step 6: Run validation and export tests**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferValidationTests access.test_data_transfer.DataTransferExportTests`

Expected: all tests PASS.

---

### Task 3: Atomic Replace Restore

**Files:**
- Modify: `access/data_transfer.py`
- Modify: `access/test_data_transfer.py`

**Interfaces:**
- Consumes: `parse_backup(content: bytes, *, allow_external_references: bool = False) -> dict[str, object]`
- Produces: `ImportResult` frozen dataclass with `mode`, `created_locks`, `updated_locks`, `created_groups`, `updated_groups`, `created_transponders`, `updated_transponders`
- Produces: `restore_backup(content: bytes, *, mode: str, replace_confirmed: bool = False) -> ImportResult`
- Accepts modes: exactly `merge` and `replace`

- [ ] **Step 1: Write failing full round-trip and replacement tests**

Export the Task 1 fixture with a fixed timestamp, create unrelated replacement
records, then call:

```python
result = data_transfer.restore_backup(
    content, mode="replace", replace_confirmed=True
)
self.assertEqual(result.mode, "replace")
self.assertEqual(result.created_locks, 2)
self.assertFalse(Lock.objects.filter(serial="UNRELATED").exists())
self.assertEqual(
    data_transfer.build_backup(exported_at=fixed)["locks"],
    original["locks"],
)
```

Compare all three record collections, every relationship set, `printed_on`, and
the preserved `imported_at`. Create an auth user before restore and assert that
the same user remains afterward.

- [ ] **Step 2: Write failing confirmation, invalid-mode, and rollback tests**

Assert replace mode without `replace_confirmed=True` raises a German
`BackupValidationError` before deletion. Assert an unknown mode is rejected.
Patch the relationship writer to raise `IntegrityError` after records are
created, then assert the pre-import locks, groups, transponders, and
relationships are unchanged.

- [ ] **Step 3: Run replace tests and verify failure**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferReplaceTests`

Expected: FAIL because `restore_backup` and `ImportResult` do not exist.

- [ ] **Step 4: Implement result type and replace orchestration**

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ImportResult:
    mode: Literal["merge", "replace"]
    created_locks: int
    updated_locks: int
    created_groups: int
    updated_groups: int
    created_transponders: int
    updated_transponders: int


def restore_backup(content: bytes, *, mode: str,
                   replace_confirmed: bool = False) -> ImportResult:
    if mode not in {"merge", "replace"}:
        raise BackupValidationError("Unbekannter Importmodus.")
    if mode == "replace" and not replace_confirmed:
        raise BackupValidationError("Das Ersetzen aller Daten muss bestätigt werden.")
    backup = parse_backup(content, allow_external_references=mode == "merge")
    with transaction.atomic():
        if mode == "replace":
            return _replace_backup(backup)
        return _merge_backup(backup)
```

Implement `_replace_backup()` by deleting `Transponder`, then `Group`, then
`Lock`; creating scalar records; and setting M2M relations only after all scalar
records exist. Resolve imported references from in-memory serial/code maps.

- [ ] **Step 5: Preserve `imported_at` and restore relationships exactly**

`auto_now_add` sets a fresh value during create, so update each created
transponder's `imported_at` with `QuerySet.update()` after creation and assign the
parsed value back to the in-memory instance. Set `locks`, `planned_locks`,
`removed_locks`, `desired_locks`, `groups`, and each group's `doors` with `.set()`.
Do not call `soll.assign_group()`, because the backup explicitly contains the
desired set and must restore it exactly.

- [ ] **Step 6: Run replace tests**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferReplaceTests`

Expected: all replace tests PASS.

---

### Task 4: Imported-Record-Wins Merge Restore

**Files:**
- Modify: `access/data_transfer.py`
- Modify: `access/test_data_transfer.py`

**Interfaces:**
- Consumes: `restore_backup(..., mode="merge")`
- Produces: transaction-scoped retained-reference and group-identity validation
- Guarantees: imported scalar fields and relationships replace matching records; unrelated records remain unchanged

- [ ] **Step 1: Write failing merge creation and precedence tests**

Seed one matching and one unrelated lock, group, and transponder. Import a backup
that updates matching scalar fields and gives matching records different
relationships. Assert imported records win, absent records are created, unrelated
records and their relationships remain unchanged, and `ImportResult` reports
created versus updated counts accurately.

- [ ] **Step 2: Write retained-reference and implicit-group tests**

Use a merge backup whose imported group references an existing lock omitted from
the file and whose imported transponder references an existing group omitted
from the file. Assert both references resolve. Cover these implicit cases:

- imported implicit group clears the retained old implicit group;
- no imported implicit group leaves a retained implicit group unchanged;
- imported update of the currently implicit group to `false` clears that group.

- [ ] **Step 3: Write conflict and rollback tests**

Assert the entire merge is rejected when an imported export code matches one
existing group while its imported name case-insensitively matches a different
existing group. Assert missing references absent from both the file and retained
database are rejected. Patch the relationship writer to fail and assert all
pre-merge values and relationships survive.

- [ ] **Step 4: Run merge tests and verify failure**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferMergeTests`

Expected: FAIL because merge currently has no implementation.

- [ ] **Step 5: Implement transaction-scoped database validation**

At the beginning of `_merge_backup()`, evaluate `select_for_update()` querysets
for all locks, groups, and transponders. Build existing maps by serial, normalized
export code, and case-folded group name. Validate imported relationship
references against imported plus retained maps. Reject one imported group when
its code and name resolve to two different existing rows.

- [ ] **Step 6: Implement scalar upserts and exact imported relationships**

For every imported record, update all scalar fields on a match or create a new
record. Count creations and updates separately. If any imported group is
implicit, clear `is_implicit` from all groups before setting imported values;
otherwise preserve retained implicit metadata except where that same imported
group explicitly sets itself false.

After scalar upserts, rebuild maps and apply `.set()` only to imported groups and
transponders. Relationships belonging exclusively to unrelated records remain
untouched. Preserve imported `Transponder.imported_at` with `QuerySet.update()`.

- [ ] **Step 7: Run all domain transfer tests**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferExportTests access.test_data_transfer.DataTransferValidationTests access.test_data_transfer.DataTransferReplaceTests access.test_data_transfer.DataTransferMergeTests`

Expected: all domain tests PASS.

---

### Task 5: Authenticated HTTP Endpoints

**Files:**
- Modify: `access/views.py`
- Modify: `access/urls.py`
- Modify: `access/test_data_transfer.py`

**Interfaces:**
- Consumes: `data_transfer.encode_backup()` and `data_transfer.restore_backup()`
- Produces: `data_page(request)` at `/data/`
- Produces: `data_export(request)` at `/data/export/`
- Produces: POST-only `data_import(request)` at `/data/import/`

- [ ] **Step 1: Write failing page and download endpoint tests**

Assert `/data/` returns 200 and uses `access/data_transfer.html`. Assert
`/data/export/` returns parseable UTF-8 JSON with:

```python
self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
self.assertRegex(
    response["Content-Disposition"],
    r'^attachment; filename="schliessmatrix-backup-\d{4}-\d{2}-\d{2}\.json"$',
)
self.assertIn("private", response["Cache-Control"])
self.assertIn("no-store", response["Cache-Control"])
```

Also assert user-supplied query parameters never appear in the filename.

- [ ] **Step 2: Write failing upload endpoint tests**

Post a `SimpleUploadedFile("backup.json", content, "application/json")` with
`mode="merge"`; assert redirect to `/data/`, imported data, and a German success
message containing created/updated counts. Cover no file, multiple files, bad
mode, invalid JSON, replace without `confirm_replace`, successful replace, and
GET `/data/import/` returning 405.

- [ ] **Step 3: Write login and CSRF tests**

Extend the existing login-gate setup or add a focused class under
`@override_settings(MIDDLEWARE=GATE_MIDDLEWARE)`. Assert anonymous requests to all
three routes redirect to login and a normal authenticated non-staff user can use
all three. Use `Client(enforce_csrf_checks=True)` to assert import POST without a
CSRF token returns 403.

- [ ] **Step 4: Run endpoint tests and verify failure**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferViewTests access.test_data_transfer.DataTransferAuthTests`

Expected: FAIL because routes and views do not exist.

- [ ] **Step 5: Add routes and thin views**

Add these named routes before variable object-detail routes:

```python
path("data/", views.data_page, name="data_page"),
path("data/export/", views.data_export, name="data_export"),
path("data/import/", views.data_import, name="data_import"),
```

Implement `data_export()` with `HttpResponse`, a server-generated date filename,
`Cache-Control: private, no-store`, and `Pragma: no-cache`. Implement
`data_import()` with `@require_POST`, exactly one `request.FILES.getlist("backup")`
entry, mode/confirmation extraction, and `BackupValidationError` handling. Catch
unexpected `DatabaseError`, log it with `logger.exception`, show a generic German
message, and redirect without exposing exception text.

- [ ] **Step 6: Add concise result messages**

For merge, display created and updated counts for doors, groups, and
transponders. For replace, display restored totals. Keep all message text in the
view because it is HTTP presentation policy, while `ImportResult` remains domain
data.

- [ ] **Step 7: Run endpoint and domain tests**

Run: `uv run python manage.py test access.test_data_transfer`

Expected: all data-transfer tests PASS.

---

### Task 6: Responsive Data Transfer Page And Navigation

**Files:**
- Create: `access/templates/access/data_transfer.html`
- Modify: `access/templates/access/base.html`
- Modify: `access/test_data_transfer.py`

**Interfaces:**
- Consumes named URLs: `data_page`, `data_export`, `data_import`
- Produces navigation state: `nav == "data"`

- [ ] **Step 1: Write failing template and navigation tests**

Assert the page contains `Daten sichern`, `Daten importieren`,
`Zusammenführen`, `Alle Daten ersetzen`, `accept="application/json,.json"`, and
the no-account explanation. Assert both desktop and mobile nav contain a
`data-page-link`, and the data page marks both links active through
`nav-active`.

- [ ] **Step 2: Write failing replace-warning behavior test**

Assert Alpine state defaults to merge, the replace confirmation block uses
`x-show="mode === 'replace'"`, and the checkbox is named `confirm_replace` with
value `on`. This verifies the server field expected by Task 5 without requiring a
browser JavaScript test.

- [ ] **Step 3: Run UI tests and verify failure**

Run: `uv run python manage.py test access.test_data_transfer.DataTransferTemplateTests`

Expected: FAIL because the template and nav links do not exist.

- [ ] **Step 4: Build the responsive page using existing components**

Extend `access/base.html`. Use a page heading plus a one-column mobile/two-column
large-screen grid. The export card explains that locks, transponders, groups,
and rights are included while accounts are excluded. The import form uses
`enctype="multipart/form-data"`, a JSON file input, two clear radio-card mode
choices, an Alpine-controlled rose warning panel for replace mode, and the
server-enforced confirmation checkbox.

Use the existing `.card`, `.btn-primary`, `.btn-ghost`, `.eyebrow`, and
`.h-title` classes. Keep native radio and file-input controls accessible by
keyboard. Do not add animation beyond the existing short Alpine transition, and
respect the global reduced-motion rule.

- [ ] **Step 5: Add desktop and mobile navigation links**

Add `Daten` after `Soll` in both nav blocks, with `data-page-link` attributes and
the same active-state expression used by other routes:

```django
<a href="{% url 'data_page' %}" data-page-link
   class="nav-link {% if nav == 'data' %}nav-active{% endif %}">Daten</a>
```

- [ ] **Step 6: Run UI and endpoint tests**

Run: `uv run python manage.py test access.test_data_transfer`

Expected: all data-transfer tests PASS.

---

### Task 7: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Verify: `access/data_transfer.py`
- Verify: `access/test_data_transfer.py`
- Verify: `access/views.py`
- Verify: `access/urls.py`
- Verify: `access/templates/access/data_transfer.html`
- Verify: `access/templates/access/base.html`

**Interfaces:**
- Consumes the completed website workflow
- Produces user-facing operating documentation and final verification evidence

- [ ] **Step 1: Update README workflow documentation**

Add `Daten` to the Views section. Explain that website backups are versioned
domain JSON, exclude login accounts, and support merge or exact replacement.
Keep this distinct from SimonsVoss printout imports, PDF reports, and the
deployment-level SQLite backup described in `DEPLOY.md`.

- [ ] **Step 2: Run formatting**

Run: `uv run ruff format access/data_transfer.py access/test_data_transfer.py access/views.py access/urls.py`

Expected: files format successfully.

- [ ] **Step 3: Run lint**

Run: `uv run ruff check access/data_transfer.py access/test_data_transfer.py access/views.py access/urls.py`

Expected: no lint errors.

- [ ] **Step 4: Run focused data-transfer tests**

Run: `uv run python manage.py test access.test_data_transfer`

Expected: all focused tests PASS.

- [ ] **Step 5: Run the complete application test suite**

Run: `uv run python manage.py test access`

Expected: all tests PASS, including existing PDF/image imports and PDF exports.

- [ ] **Step 6: Run Django system checks**

Run: `KEYMGMT_DEBUG=1 uv run python manage.py check`

Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 7: Inspect the final diff without changing unrelated work**

Run: `git status --short && git diff --check && git diff -- access/data_transfer.py access/test_data_transfer.py access/views.py access/urls.py access/templates/access/data_transfer.html access/templates/access/base.html README.md docs/superpowers/specs/2026-07-28-website-data-import-export-design.md docs/superpowers/plans/2026-07-28-website-data-import-export.md`

Expected: only intended feature files are shown, `git diff --check` is silent,
and no unrelated changes were reverted or overwritten.
