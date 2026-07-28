# Dynamic Group Combination Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep atomic access groups internally while deriving one stable `AStA ...` combination label for every PDF export and transponder-list row.

**Architecture:** Add stable export metadata to `Group`, with pure naming helpers and one transactional metadata-update service in a focused `access/group_labels.py` module. All consumers receive one precomputed label from that module; Typst and Django templates only render it, so naming policy cannot diverge between views and exports.

**Tech Stack:** Python 3.14, Django 6.0, SQLite, Django templates, Alpine.js, Typst, Django `TestCase`

## Global Constraints

- Preserve `Group` as an atomic reusable door set and `Transponder.groups` as the stored combination.
- Do not create persisted combination groups such as `AStA LT`.
- `export_code` is uppercase ASCII alphanumeric, 1-4 characters, and case-insensitively unique.
- At most one group is marked implicit; its code is omitted only when another group is present.
- Sort component groups case-insensitively by full group name.
- Concatenate one-letter codes (`LT`); if any displayed code is longer, join all displayed codes with hyphens (`LA-T`).
- Prefix non-empty labels with `AStA `; ungrouped transponders have an empty derived label.
- Renaming a group never regenerates its export code.
- Reuse the same formatter in matrix, Soll/Ist, changes-worklist, and transponder-list output.
- Reuse the same formatter for dashboard transponder cards.
- Do not change Soll propagation or current/planned/removed lock semantics.
- Do not commit unless the user explicitly requests it.

---

## File Map

- Create `access/group_labels.py`: code normalization and generation, combination formatting, and atomic export-metadata updates.
- Create `access/migrations/0007_group_export_metadata.py`: staged schema/data migration for existing groups.
- Modify `access/models.py`: `Group.export_code`, `Group.is_implicit`, constraints, and automatic code generation for ORM callers.
- Modify `access/views.py`: group metadata endpoints, validation responses, group labels for list rows.
- Modify `access/urls.py`: route for updating export metadata.
- Modify `access/pdf_export.py`: provide one shared combination label in all PDF data modes.
- Modify `access/templates/access/base.html`: surface JSON error messages from AJAX requests.
- Modify `access/templates/access/group_list.html`: optional custom code on create and visible code chips.
- Modify `access/templates/access/group_detail.html`: editable code and implicit setting.
- Modify `access/templates/access/soll_matrix.html`: surface quick-create errors.
- Modify `access/templates/access/transponder_list.html`: responsive group-label display and search metadata.
- Modify `access/templates/access/dashboard.html`: compact group-label chip beside each grouped transponder's serial.
- Modify `access/templates/pdf/matrix.typ`: render the supplied combined label directly.
- Modify `access/templates/pdf/changes.typ`: show the label in each changed-transponder heading.
- Modify `access/tests.py`: domain, endpoint, export, rendering, responsive-list, and query-count coverage.
- Modify `README.md`: document atomic groups and derived combination labels.

---

### Task 1: Group Export Metadata And Naming Domain

**Files:**
- Create: `access/group_labels.py`
- Create: `access/migrations/0007_group_export_metadata.py`
- Modify: `access/models.py:1-45`
- Modify: `access/tests.py` near the Soll/group tests

**Interfaces:**
- Produces: `normalize_export_code(value: str) -> str`
- Produces: `derive_export_code(name: str, used_codes: Iterable[str]) -> str`
- Produces: `combined_group_label(groups: Iterable[Group]) -> str`
- Produces: `set_group_export_metadata(group: Group, *, export_code: str, is_implicit: bool) -> Group`
- Produces: `Group.export_code: str` and `Group.is_implicit: bool`

- [ ] **Step 1: Write failing domain tests**

Add a `GroupLabelTests(TestCase)` class to `access/tests.py`. Cover normalization, prefix stripping, collision fallback, exhausted generation, stable model codes, metadata constraints, implicit replacement, and formatting:

```python
class GroupLabelTests(TestCase):
    def test_custom_code_normalization_uppercases_but_rejects_punctuation(self):
        from django.core.exceptions import ValidationError
        from access.group_labels import normalize_export_code

        self.assertEqual(normalize_export_code(" ws "), "WS")
        self.assertEqual(normalize_export_code("ök"), "OK")
        with self.assertRaises(ValidationError):
            normalize_export_code("L-")

    def test_code_generation_uses_shortest_available_prefix(self):
        from access.group_labels import derive_export_code

        self.assertEqual(derive_export_code("AStA Lager", set()), "L")
        self.assertEqual(derive_export_code("AStA Lager", {"L"}), "LA")
        self.assertEqual(derive_export_code("Ökologie", set()), "O")

    def test_code_generation_rejects_exhausted_prefixes(self):
        from django.core.exceptions import ValidationError
        from access.group_labels import derive_export_code

        with self.assertRaisesMessage(ValidationError, "eigenen Export-Code"):
            derive_export_code("Lager", {"L", "LA", "LAG", "LAGE"})

    def test_model_generates_code_once_and_keeps_it_on_rename(self):
        group = Group.objects.create(name="AStA Lager")
        self.assertEqual(group.export_code, "L")
        group.name = "Materiallager"
        group.save()
        group.refresh_from_db()
        self.assertEqual(group.export_code, "L")

    def test_codes_are_normalized_and_case_insensitively_unique(self):
        from django.core.exceptions import ValidationError

        Group.objects.create(name="Lager", export_code="l")
        duplicate = Group(name="Labor", export_code="L")
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_setting_implicit_replaces_previous_group(self):
        from access.group_labels import set_group_export_metadata

        first = Group.objects.create(name="Allgemein", export_code="A")
        second = Group.objects.create(name="Basis", export_code="B")
        set_group_export_metadata(first, export_code="A", is_implicit=True)
        set_group_export_metadata(second, export_code="B", is_implicit=True)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_implicit)
        self.assertTrue(second.is_implicit)

    def test_combined_label_sorts_omits_implicit_and_separates_long_codes(self):
        from access.group_labels import combined_group_label

        base = Group.objects.create(
            name="AStA Allgemein", export_code="A", is_implicit=True)
        lager = Group.objects.create(name="AStA Lager", export_code="LA")
        technik = Group.objects.create(name="AStA Technik", export_code="T")
        self.assertEqual(combined_group_label([]), "")
        self.assertEqual(combined_group_label([base]), "AStA A")
        self.assertEqual(
            combined_group_label([technik, base, lager]), "AStA LA-T")
        self.assertEqual(combined_group_label([technik, lager]), "AStA LA-T")
```

- [ ] **Step 2: Run the domain tests and verify they fail**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.GroupLabelTests -v2
```

Expected: FAIL because `access.group_labels` and the new model fields do not exist.

- [ ] **Step 3: Implement the focused naming module**

Create `access/group_labels.py` with pure helpers and one transactional service. Keep model imports local to the service to avoid a circular import from `models.py`:

```python
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction

if TYPE_CHECKING:
    from .models import Group


CODE_RE = re.compile(r"^[A-Z0-9]{1,4}$")


def _ascii_upper(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore").decode("ascii").upper()


def normalize_export_code(value: str) -> str:
    code = _ascii_upper(value.strip())
    if not CODE_RE.fullmatch(code):
        raise ValidationError("Der Export-Code muss 1-4 Buchstaben oder Ziffern enthalten.")
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
        "Kein eindeutiger Export-Code ableitbar; bitte einen eigenen Export-Code angeben.")


def combined_group_label(groups: Iterable[Group]) -> str:
    ordered = sorted(groups, key=lambda group: group.name.casefold())
    if not ordered:
        return ""
    displayed = ([group for group in ordered if not group.is_implicit]
                 if len(ordered) > 1 else ordered)
    codes = [group.export_code for group in displayed]
    suffix = "".join(codes) if all(len(code) == 1 for code in codes) else "-".join(codes)
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
            is_implicit=False)
    locked.export_code = normalize_export_code(export_code)
    locked.is_implicit = is_implicit
    locked.full_clean()
    locked.save(update_fields=["export_code", "is_implicit"])
    return locked
```

- [ ] **Step 4: Add model fields, generation, and constraints**

Update `Group` in `access/models.py`. Use `Lower` for case-insensitive uniqueness and a conditional unique constraint for the single implicit group:

```python
from django.db import models
from django.db.models.functions import Lower

from .group_labels import derive_export_code, normalize_export_code


class Group(models.Model):
    name = models.CharField(max_length=128, unique=True)
    export_code = models.CharField(max_length=4)
    is_implicit = models.BooleanField(default=False)
    doors = models.ManyToManyField(Lock, related_name="groups", blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(export_code__regex=r"^[A-Z0-9]{1,4}$"),
                name="access_group_export_code_format",
            ),
            models.UniqueConstraint(
                Lower("export_code"),
                name="access_group_export_code_ci_unique",
            ),
            models.UniqueConstraint(
                fields=["is_implicit"],
                condition=models.Q(is_implicit=True),
                name="access_group_single_implicit",
            ),
        ]

    def clean(self):
        super().clean()
        if self.export_code:
            self.export_code = normalize_export_code(self.export_code)

    def save(self, *args, **kwargs):
        if self.export_code:
            self.export_code = normalize_export_code(self.export_code)
        else:
            used = type(self).objects.exclude(pk=self.pk).values_list(
                "export_code", flat=True)
            self.export_code = derive_export_code(self.name, used)
        return super().save(*args, **kwargs)
```

Keep the existing `Group` docstring, `doors`, ordering, `__str__`, and `label` property.

- [ ] **Step 5: Add the staged schema/data migration**

Create `access/migrations/0007_group_export_metadata.py`. Do not import runtime helpers in a data migration; copy the small normalization/derivation algorithm so old migrations remain reproducible. Operations must be ordered as follows:

```python
operations = [
    migrations.AddField(
        model_name="group",
        name="export_code",
        field=models.CharField(max_length=4, null=True),
    ),
    migrations.AddField(
        model_name="group",
        name="is_implicit",
        field=models.BooleanField(default=False),
    ),
    migrations.RunPython(backfill_group_metadata, migrations.RunPython.noop),
    migrations.AlterField(
        model_name="group",
        name="export_code",
        field=models.CharField(max_length=4),
    ),
    migrations.AddConstraint(
        model_name="group",
        constraint=models.CheckConstraint(
            condition=models.Q(export_code__regex=r"^[A-Z0-9]{1,4}$"),
            name="access_group_export_code_format",
        ),
    ),
    migrations.AddConstraint(
        model_name="group",
        constraint=models.UniqueConstraint(
            Lower("export_code"),
            name="access_group_export_code_ci_unique",
        ),
    ),
    migrations.AddConstraint(
        model_name="group",
        constraint=models.UniqueConstraint(
            fields=("is_implicit",),
            condition=models.Q(is_implicit=True),
            name="access_group_single_implicit",
        ),
    ),
]
```

Implement the historical backfill directly in the migration:

```python
def backfill_group_metadata(apps, schema_editor):
    Group = apps.get_model("access", "Group")
    used = set()
    for group in Group.objects.order_by(Lower("name")):
        semantic = re.sub(
            r"^AStA\s+", "", group.name.strip(), flags=re.IGNORECASE)
        ascii_name = unicodedata.normalize("NFKD", semantic).encode(
            "ascii", "ignore").decode("ascii").upper()
        stem = "".join(char for char in ascii_name if char.isalnum())
        code = next(
            (stem[:length] for length in range(1, min(4, len(stem)) + 1)
             if stem[:length] not in used),
            None,
        )
        if code is None:
            raise RuntimeError(
                f"Cannot derive a unique export code for group {group.name!r}")
        group.export_code = code
        group.is_implicit = group.name.casefold() == "asta allgemein"
        group.save(update_fields=["export_code", "is_implicit"])
        used.add(code)
```

Import `re`, `unicodedata`, and `Lower` in the migration. This produces `A`, `L`, `S`, `T`, and `U` for the current records.

- [ ] **Step 6: Run focused tests and migration consistency checks**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.GroupLabelTests -v2
KEYMGMT_DEBUG=1 uv run python manage.py makemigrations --check --dry-run
```

Expected: all domain tests PASS and Django reports `No changes detected`.

- [ ] **Step 7: Review checkpoint**

Inspect `git diff --check` and the Task 1 diff. Confirm no Soll logic changed and no combination `Group` records are introduced. Do not commit unless explicitly requested.

---

### Task 2: Group Metadata Endpoints And Management UI

**Files:**
- Modify: `access/views.py:642-693`
- Modify: `access/urls.py:28-32`
- Modify: `access/templates/access/base.html:119-127`
- Modify: `access/templates/access/group_list.html`
- Modify: `access/templates/access/group_detail.html`
- Modify: `access/templates/access/soll_matrix.html:141-156`
- Modify: `access/tests.py:1575-1594` and nearby group tests

**Interfaces:**
- Consumes: `derive_export_code`, `normalize_export_code`, and `set_group_export_metadata` from Task 1.
- Produces: `POST /groups/<pk>/metadata/` accepting `{"export_code": str, "is_implicit": bool}`.
- Produces: group creation accepting optional `export_code` while continuing to accept name-only requests.

- [ ] **Step 1: Write failing endpoint and page tests**

Extend `SollEditingTests` with explicit success and validation cases:

```python
def test_group_create_generates_or_accepts_custom_code(self):
    auto = self._post("/groups/create/", {"name": "New"})
    self.assertEqual(auto.status_code, 200)
    self.assertEqual(auto.json()["export_code"], "N")
    custom = self._post(
        "/groups/create/", {"name": "Workshop", "export_code": "ws"})
    self.assertEqual(custom.status_code, 200)
    self.assertEqual(custom.json()["export_code"], "WS")

def test_group_metadata_update_replaces_implicit_group(self):
    first = self._post(
        f"/groups/{self.g1.pk}/metadata/",
        {"export_code": "ONE", "is_implicit": True},
    )
    second = self._post(
        f"/groups/{self.g2.pk}/metadata/",
        {"export_code": "TWO", "is_implicit": True},
    )
    self.assertEqual(first.status_code, 200)
    self.assertEqual(second.status_code, 200)
    self.g1.refresh_from_db()
    self.g2.refresh_from_db()
    self.assertFalse(self.g1.is_implicit)
    self.assertTrue(self.g2.is_implicit)

def test_group_metadata_rejects_duplicate_and_malformed_codes(self):
    duplicate = self._post(
        f"/groups/{self.g2.pk}/metadata/",
        {"export_code": self.g1.export_code.lower(), "is_implicit": False},
    )
    malformed = self._post(
        f"/groups/{self.g2.pk}/metadata/",
        {"export_code": "TOO-LONG", "is_implicit": False},
    )
    self.assertEqual(duplicate.status_code, 400)
    self.assertIn("error", duplicate.json())
    self.assertEqual(malformed.status_code, 400)
    self.assertIn("error", malformed.json())

def test_group_pages_show_export_metadata(self):
    list_response = self.client.get("/groups/")
    detail_response = self.client.get(f"/groups/{self.g1.pk}/")
    self.assertContains(list_response, self.g1.export_code)
    self.assertContains(detail_response, 'name="export_code"')
    self.assertContains(detail_response, "Implizit in Kombinationen")
```

Also update `test_group_crud_endpoints` to assert that renaming preserves the generated code.

- [ ] **Step 2: Run endpoint tests and verify they fail**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.SollEditingTests -v2
```

Expected: FAIL because metadata routing, response fields, and UI controls do not exist.

- [ ] **Step 3: Implement validated create and metadata endpoints**

In `access/views.py`, import `ValidationError`, `IntegrityError`, and Task 1 helpers. Add a small error converter and refactor creation as follows:

```python
def _group_error(exc):
    message = (exc.messages[0] if isinstance(exc, ValidationError)
               else "Name oder Export-Code bereits vergeben.")
    return JsonResponse({"error": message}, status=400)


@require_POST
def group_create(request):
    data = _json_body(request)
    name = ((data.get("name") if isinstance(data, dict) else None) or "").strip()
    if not name:
        return JsonResponse({"error": "Name erforderlich."}, status=400)
    existing = Group.objects.filter(name=name).first()
    if existing is not None:
        return JsonResponse({
            "ok": True,
            "id": existing.pk,
            "name": existing.name,
            "export_code": existing.export_code,
        })
    supplied = (data.get("export_code") or "").strip()
    try:
        code = (normalize_export_code(supplied) if supplied else
                derive_export_code(
                    name, Group.objects.values_list("export_code", flat=True)))
        group = Group(name=name, export_code=code)
        group.full_clean()
        group.save()
    except (ValidationError, IntegrityError) as exc:
        return _group_error(exc)
    return JsonResponse({
        "ok": True,
        "id": group.pk,
        "name": group.name,
        "export_code": group.export_code,
    })
```

Also make `group_rename` call `full_clean()` and `save(update_fields=["name"])` inside the same exception handling. Do not assign or regenerate `export_code` in the rename endpoint.

Add the metadata endpoint:

```python
@require_POST
def group_metadata(request, pk):
    group = get_object_or_404(Group, pk=pk)
    data = _json_body(request)
    if (not isinstance(data, dict)
            or not isinstance(data.get("is_implicit"), bool)):
        return JsonResponse({"error": "Ungültige Gruppenmetadaten."}, status=400)
    try:
        updated = set_group_export_metadata(
            group,
            export_code=data.get("export_code", ""),
            is_implicit=data["is_implicit"],
        )
    except (ValidationError, IntegrityError) as exc:
        return _group_error(exc)
    return JsonResponse({
        "ok": True,
        "export_code": updated.export_code,
        "is_implicit": updated.is_implicit,
    })
```

Register it as `groups/<int:pk>/metadata/` with URL name `group_metadata`.

- [ ] **Step 4: Surface server validation messages in shared AJAX code**

Change `sollPost()` in `base.html` so it parses JSON before checking `r.ok`:

```javascript
const data = await r.json().catch(() => ({}));
if (!r.ok) throw new Error(data.error || 'Speichern fehlgeschlagen.');
return data;
```

This preserves existing callers while allowing group-management code to show a precise server message.

- [ ] **Step 5: Add create, display, and edit controls**

In `group_list.html`:

- Add Alpine state `newCode`.
- Add an optional `maxlength="4"` input labeled `Export-Code (optional)`.
- Send `{name: n, export_code: this.newCode.trim()}`.
- Catch errors and call `alert(error.message)`.
- Render `{{ g.export_code }}` as a chip on every group card.

In `group_detail.html`, add an export-metadata panel with:

```html
<input x-model="exportCode" name="export_code" maxlength="4">
<label>
  <input x-model="implicit" type="checkbox">
  Implizit in Kombinationen
</label>
<button @click="saveMetadata()" class="btn-primary">Export speichern</button>
```

Initialize `exportCode` and `implicit` from the Django object, call the new metadata URL, update the local normalized code from the response, and alert `error.message` on failure. Keep rename and door editing separate so changing export presentation cannot affect Soll propagation.

In `soll_matrix.html`, wrap quick create, rename, and delete requests in `try/catch`; alert the server message and reload only after a successful request.

- [ ] **Step 6: Run the group endpoint/page tests**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.SollEditingTests -v2
```

Expected: PASS.

- [ ] **Step 7: Review checkpoint**

Run `git diff --check`, inspect endpoint error paths, and verify code edits never mutate doors, memberships, or desired locks. Do not commit unless explicitly requested.

---

### Task 3: Flatten Labels In Every PDF Export

**Files:**
- Modify: `access/pdf_export.py:24-201`
- Modify: `access/templates/pdf/matrix.typ:105-116`
- Modify: `access/templates/pdf/changes.typ:65-76`
- Modify: `access/tests.py:1438-1518`

**Interfaces:**
- Consumes: `combined_group_label(groups) -> str` from Task 1.
- Produces: matrix/diff transponder metadata with `group: str` instead of `groups: list[str]`.
- Produces: each changes-worklist entry with `group: str`.

- [ ] **Step 1: Replace the old export-name test with failing flattened-label tests**

Replace `test_export_includes_group_names` and extend the changes-data test:

```python
def test_all_export_modes_use_flattened_group_label(self):
    base = Group.objects.create(
        name="AStA Allgemein", export_code="A", is_implicit=True)
    lager = Group.objects.create(name="AStA Lager", export_code="L")
    technik = Group.objects.create(name="AStA Technik", export_code="T")
    self.a.groups.add(technik, base, lager)

    matrix = {item["serial"]: item for item in
              self.pdf_export.build_matrix_data("all")["transponders"]}
    diff = {item["serial"]: item for item in
            self.pdf_export.build_diff_data()["transponders"]}
    self.a.desired_locks.clear()
    changes = {item["serial"]: item for item in
               self.pdf_export.build_changes_data()["changes"]}

    self.assertEqual(matrix["AAA"]["group"], "AStA LT")
    self.assertEqual(diff["AAA"]["group"], "AStA LT")
    self.assertEqual(changes["AAA"]["group"], "AStA LT")
    self.assertEqual(matrix["BBB"]["group"], "")
    self.assertNotIn("groups", matrix["AAA"])
```

- [ ] **Step 2: Run the PDF data tests and verify they fail**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.PdfExportTests -v2
```

Expected: FAIL because exports still expose `groups` and changes entries have no group metadata.

- [ ] **Step 3: Use the formatter in all PDF data builders**

In `access/pdf_export.py`:

- Import `combined_group_label`.
- Change `_meta()` to emit `"group": combined_group_label(t.groups.all())`.
- Keep `groups` prefetched in matrix and diff builders.
- Add `groups` to `build_changes_data()` prefetch relations.
- Add `"group": combined_group_label(tp.groups.all())` to every changes entry.

The resulting metadata shape is:

```python
{
    "label": tp.label,
    "serial": tp.serial,
    "asta": tp.asta_number,
    "group": combined_group_label(tp.groups.all()),
}
```

- [ ] **Step 4: Render the supplied string in both Typst templates**

In `matrix.typ`, replace list joining with:

```typst
let g = c.group
```

Keep the existing blank check, truncation, rotation, and group-band dimensions.

In `changes.typ`, add the group after serial/ASTA metadata in the heading bar:

```typst
if t.group != "" {
  text(fill: rgb("#4f46e5"), size: 8pt, weight: "medium")[ · #t.group]
}
```

- [ ] **Step 5: Run data and Typst rendering tests**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.PdfExportTests -v2
```

Expected: all data tests PASS. If Typst is installed, matrix, diff, and changes rendering tests also PASS and produce `%PDF` bytes; otherwise existing decorators report skips.

- [ ] **Step 6: Review checkpoint**

Run `git diff --check` and confirm templates contain no naming policy beyond rendering/truncation. Do not commit unless explicitly requested.

---

### Task 4: Group Labels In The Transponder List And Documentation

**Files:**
- Modify: `access/views.py:146-150`
- Modify: `access/templates/access/transponder_list.html:20-44`
- Modify: `access/tests.py` near list/page tests
- Modify: `README.md:81-116`

**Interfaces:**
- Consumes: `combined_group_label(groups) -> str` from Task 1.
- Produces: transient `transponder.group_label: str` for list rendering.

- [ ] **Step 1: Write failing transponder-list tests**

Add a focused `TransponderListTests` class:

```python
class TransponderListTests(TestCase):
    def setUp(self):
        self.tp = Transponder.objects.create(
            serial="LIST-BASE", person_name="Listenkarte")

    def test_list_shows_and_searches_combined_group_label(self):
        base = Group.objects.create(
            name="AStA Allgemein", export_code="A", is_implicit=True)
        lager = Group.objects.create(name="AStA Lager", export_code="L")
        self.tp.groups.set([lager, base])

        response = self.client.get("/transponders/")

        self.assertContains(response, "AStA L", count=2)
        self.assertContains(response, "Gruppe")
        self.assertContains(response, "asta l")

    def test_list_prefetches_groups_without_n_plus_one(self):
        groups = [
            Group.objects.create(name=f"Group {index}") for index in range(3)
        ]
        for index in range(3):
            transponder = Transponder.objects.create(serial=f"LIST-{index}")
            transponder.groups.add(groups[index])

        with self.assertNumQueries(2):
            response = self.client.get("/transponders/")
            self.assertEqual(response.status_code, 200)

    def test_list_marks_ungrouped_rows(self):
        response = self.client.get("/transponders/")
        self.assertContains(response, "&mdash;", html=False)
```

The label count is two because desktop and narrow-screen markup both render it.

- [ ] **Step 2: Run the list tests and verify they fail**

Run the exact new test class or methods, for example:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.TransponderListTests -v2
```

Expected: FAIL because the view does not prefetch/compute group labels and the template has no group presentation.

- [ ] **Step 3: Prefetch and compute labels in the list view**

Change `transponder_list()` to evaluate one annotated, prefetched queryset and attach the transient display value:

```python
def transponder_list(request):
    transponders = list(
        Transponder.objects.annotate(n=Count("locks"))
        .prefetch_related("groups")
        .order_by("asta_number", "person_name", "serial")
    )
    for transponder in transponders:
        transponder.group_label = combined_group_label(transponder.groups.all())
    return render(request, "access/transponder_list.html", {
        "transponders": transponders,
        "nav": "transponders",
    })
```

- [ ] **Step 4: Add responsive label presentation and search**

In `transponder_list.html`:

- Include `{{ t.group_label|lower }}` in `data-search`.
- Add a desktop `Gruppe` header/cell using `hidden md:table-cell`.
- Render `<span class="chip">{{ t.group_label }}</span>` when non-empty.
- Render a muted `&mdash;` when empty.
- Add a `md:hidden` label beneath the holder name so mobile users see the same information.

Keep `Türen` right-aligned and do not hide or replace existing owner, serial, or ASTA information.

- [ ] **Step 5: Update README behavior and model documentation**

Update `README.md` to state:

- Transponder rows show a compact derived programming-group label.
- Groups are atomic reusable door sets with stable export codes.
- A transponder's multiple atomic groups flatten to one display/export label.
- Matrix, Soll/Ist, and changes PDFs all use the same label.

Do not document combined labels as persisted groups.

- [ ] **Step 6: Run focused and full verification**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.TransponderListTests -v2
KEYMGMT_DEBUG=1 uv run python manage.py makemigrations --check --dry-run
KEYMGMT_DEBUG=1 uv run python manage.py check
KEYMGMT_DEBUG=1 uv run python manage.py test access -v1
git diff --check
```

Expected: focused and full suites PASS, Django reports no model changes and no system-check issues, and `git diff --check` emits no output. Typst tests may skip only when the binary is unavailable.

- [ ] **Step 7: Final review checkpoint**

Compare the implementation against `docs/superpowers/specs/2026-07-28-group-combination-export-design.md`. Confirm the four consumers implemented through Task 4 use `combined_group_label`, ungrouped output is blank in PDFs and muted in the list, and no unrelated worktree changes were modified. Do not commit unless explicitly requested.

---

### Task 5: Group Labels On Dashboard Transponder Cards

**Files:**
- Modify: `access/views.py:150-161`
- Modify: `access/templates/access/dashboard.html:92-120`
- Modify: `access/tests.py` near `TransponderListTests`
- Modify: `README.md:81-99`

**Interfaces:**
- Consumes: `combined_group_label(groups) -> str` from Task 1.
- Produces: transient `transponder.group_label: str` for dashboard card rendering.

- [ ] **Step 1: Write failing dashboard tests**

Add a focused test class using real database relations and rendered HTML:

```python
class DashboardGroupLabelTests(TestCase):
    def setUp(self):
        self.grouped = Transponder.objects.create(
            serial="DASH-GROUPED", person_name="Gruppiert")
        self.ungrouped = Transponder.objects.create(
            serial="DASH-EMPTY", person_name="Ohne Gruppe")
        base = Group.objects.create(
            name="AStA Allgemein", export_code="A", is_implicit=True)
        lager = Group.objects.create(name="AStA Lager", export_code="L")
        self.grouped.groups.set([lager, base])

    def test_dashboard_shows_group_chip_only_for_grouped_transponders(self):
        response = self.client.get("/")

        self.assertContains(response, "AStA L", count=1)
        self.assertContains(response, "data-group-label", count=1)

    def test_dashboard_prefetches_groups_without_n_plus_one(self):
        for index in range(3):
            transponder = Transponder.objects.create(serial=f"DASH-{index}")
            transponder.groups.add(Group.objects.create(name=f"Dash Group {index}"))

        with self.assertNumQueries(5):
            response = self.client.get("/")
            self.assertEqual(response.status_code, 200)
```

The five dashboard queries are the transponder query, one group-prefetch query,
and the existing transponder, lock, and active-grant summary counts. The exact
bound proves additional cards do not add queries.

- [ ] **Step 2: Run dashboard tests and verify they fail**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.DashboardGroupLabelTests -v2
```

Expected: the rendering test fails because cards have no group label, and the
query test records four queries because the dashboard does not fetch groups.

- [ ] **Step 3: Prefetch and compute dashboard labels**

Evaluate the existing annotated queryset and attach the same transient value as
the full transponder list:

```python
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
```

- [ ] **Step 4: Render the optional chip beside the serial**

Replace the serial-only paragraph in each dashboard card with:

```html
<p class="mt-1 flex flex-wrap gap-1.5">
  <span class="chip">{{ t.serial }}</span>
  {% if t.group_label %}<span class="chip" data-group-label>{{ t.group_label }}</span>{% endif %}
</p>
```

Do not render a dash or empty chip for ungrouped transponders. Keep owner name,
door count, card link, card order, and responsive grid unchanged.

- [ ] **Step 5: Update dashboard documentation**

Change the README dashboard/transponder description to state that both dashboard
cards and the full transponder list display the same compact derived group label.

- [ ] **Step 6: Run focused and full verification**

Run:

```bash
KEYMGMT_DEBUG=1 uv run python manage.py test access.tests.DashboardGroupLabelTests -v2
KEYMGMT_DEBUG=1 uv run python manage.py makemigrations --check --dry-run
KEYMGMT_DEBUG=1 uv run python manage.py check
KEYMGMT_DEBUG=1 uv run python manage.py test access -v1
ruff format --check access/views.py access/tests.py
git diff --check
```

Expected: both dashboard tests and the full suite PASS, Django reports no model
changes or system-check issues, both Python files are formatted, and
`git diff --check` emits no output.

- [ ] **Step 7: Review checkpoint**

Confirm the dashboard and transponder-list views both call
`combined_group_label` on prefetched groups, the group chip is absent for empty
combinations, and no other dashboard behavior changed. Do not commit unless
explicitly requested.
