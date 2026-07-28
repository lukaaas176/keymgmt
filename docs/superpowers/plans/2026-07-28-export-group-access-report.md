# Export Group Access Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an authenticated report that groups group-derived Soll access by export combination, appends individual desired doors by transponder, and offers identical copyable Markdown and A4 PDF output.

**Architecture:** Extend `access/pdf_export.py` with one query-bounded report-data builder and one canonical Markdown renderer. A dedicated Typst template renders the same plain data, while thin Django views expose a report page and PDF download without changing existing matrix, diff, or changes exports.

**Tech Stack:** Python 3.14, Django 6, SQLite, Typst, Django templates, Tailwind Play CDN, Alpine.js, Django `TestCase`.

## Global Constraints

- Work directly in the current `main` worktree and preserve all existing changes.
- Do not modify unrelated current changes in `pyproject.toml`, `uv.lock`, or existing data-transfer files.
- Do not add dependencies or migrations.
- Group report sections by exact assigned group-code tuple, never by display label alone.
- Use `combined_group_label()` for normal titles and annotate only colliding titles.
- Export-group doors are the union of assigned `Group.doors`; individual desired exceptions stay out of that union.
- Individual appendix doors are `desired_locks` minus all doors inherited from assigned groups.
- Preserve every transponder serial exactly, including leading zeroes.
- Add `Ohne Gruppe` last; all desired doors of ungrouped transponders are individual.
- PDF output is fixed to A4 portrait with selectable text.
- Existing matrix, diff, changes, and website data-transfer behavior must remain unchanged.
- All website copy is German and responsive within the established visual system.
- Do not commit unless the user explicitly asks.

---

## File Structure

- Create `access/test_access_report.py`: focused builder, Markdown, PDF, endpoint, and template tests.
- Create `access/templates/pdf/access_report.typ`: A4 report layout and pagination.
- Create `access/templates/access/access_report.html`: copyable Markdown preview and PDF action.
- Modify `access/pdf_export.py`: report data, Markdown renderer, and Typst template selection.
- Modify `access/views.py`: report page and safe PDF endpoint.
- Modify `access/urls.py`: report and PDF routes before integer group-detail routes.
- Modify `access/templates/access/group_list.html`: `Zugangsübersicht` action.
- Modify `README.md`: document the new report and individual Soll appendix.

---

### Task 1: Query-Bounded Access Report Data

**Files:**
- Create: `access/test_access_report.py`
- Modify: `access/pdf_export.py`

**Interfaces:**
- Consumes: `combined_group_label(groups)` and `Lock.label`
- Produces: `build_access_report_data(*, today: datetime.date | None = None) -> dict`
- Produces data mode: `access_report`
- Produces section keys: `key`, `base_title`, `title`, `locations`, `serials`, `ungrouped`
- Produces appendix keys: `serial`, `label`, `title`, `locations`

- [ ] **Step 1: Write the failing exact-combination and door-union test**

Create two locks in different locations, an implicit `A` group, an `L` group,
and transponders assigned `A`, `A+L`, and no group. Give `A+L` one individual
desired lock that is not in either group. Assert hand-derived report data:

```python
class AccessReportDataTests(TestCase):
    def test_groups_by_exact_combination_and_uses_only_group_door_union(self):
        report = pdf_export.build_access_report_data(today=date(2026, 7, 28))

        self.assertEqual(report["mode"], "access_report")
        self.assertEqual(report["generated"], "2026-07-28")
        self.assertEqual(
            [(section["key"], section["title"]) for section in report["sections"]],
            [("A", "AStA A"), ("A+L", "AStA L"), ("", "Ohne Gruppe")],
        )
        combined = report["sections"][1]
        self.assertEqual(combined["serials"], ["010A0SC"])
        self.assertEqual(
            [lock["serial"] for location in combined["locations"] for lock in location["locks"]],
            ["DOOR-A", "DOOR-L"],
        )
        self.assertNotIn("INDIVIDUAL", {
            lock["serial"] for location in combined["locations"]
            for lock in location["locks"]
        })
```

Use exact literal serials and expected labels; do not derive expected output with
`combined_group_label()` inside the test.

- [ ] **Step 2: Run the focused test and verify the missing-function failure**

Run: `uv run python manage.py test access.test_access_report.AccessReportDataTests.test_groups_by_exact_combination_and_uses_only_group_door_union`

Expected: ERROR because `build_access_report_data` does not exist.

- [ ] **Step 3: Implement location grouping and the base report builder**

In `access/pdf_export.py`, add a private helper that deduplicates locks by serial,
uses location then area then `Ohne Standort`, and emits this exact shape:

```python
def _report_locations(locks) -> list[dict]:
    by_serial = {lock.serial: lock for lock in locks}
    by_location = defaultdict(list)
    for lock in by_serial.values():
        location = lock.location or lock.area or "Ohne Standort"
        by_location[location].append({"serial": lock.serial, "label": lock.label})
    ordered_locations = sorted(
        by_location,
        key=lambda value: (value == "Ohne Standort", value.casefold(), value),
    )
    return [
        {
            "name": location,
            "locks": sorted(
                by_location[location],
                key=lambda lock: (lock["label"].casefold(), lock["serial"].casefold(), lock["serial"]),
            ),
        }
        for location in ordered_locations
    ]
```

Query transponders once with
`prefetch_related("groups__doors", "desired_locks").order_by("serial")`. Build a
dictionary keyed by tuples of sorted export codes. Store the prefetched group
objects and transponders for each key, union group doors by serial, and build
sections with ungrouped last.

- [ ] **Step 4: Write failing collision and deterministic-order tests**

Create an `A+L` transponder and an `L`-only transponder so both normal titles are
`AStA L`. Assert separate sections titled `AStA L (A+L)` and `AStA L (L)` with
different lock unions. Add locations and labels differing by case, plus serials
with leading zeroes, and assert exact stable ordering and serial preservation.

- [ ] **Step 5: Implement collision-only title annotation**

Count `base_title` occurrences after exact-key grouping. Keep a non-colliding
title unchanged. For a duplicated title, set:

```python
section["title"] = f"{section['base_title']} ({section['key']})"
```

Use `+`-joined sorted codes for `key`; use an empty key only for `Ohne Gruppe`,
which is never collision-annotated.

- [ ] **Step 6: Write failing individual desired-door appendix tests**

Cover all of these in literal assertions:

- grouped transponder: desired group door is excluded, desired extra door is included;
- grouped transponder with no extra desired door is omitted;
- ungrouped transponder: every desired door is included;
- appendix title is `SERIAL · label` when label differs and only `SERIAL` otherwise;
- entries, locations, and locks use deterministic ordering.

- [ ] **Step 7: Implement the appendix from prefetched data**

For each transponder, compute inherited serials from prefetched `groups__doors`,
then filter prefetched `desired_locks`. Feed the difference to
`_report_locations()`, skip empty differences, and append:

```python
{
    "serial": transponder.serial,
    "label": transponder.label,
    "title": (
        transponder.serial
        if transponder.label == transponder.serial
        else f"{transponder.serial} · {transponder.label}"
    ),
    "locations": locations,
}
```

- [ ] **Step 8: Add the fixed-query-count test**

Use `CaptureQueriesContext(connection)` around one report, add five groups,
doors, and transponders with desired locks, capture a second report, and assert
both query counts match. This protects the fixed transponder + nested-prefetch
shape without coupling the test to an absolute transaction/savepoint count.

- [ ] **Step 9: Run data-builder tests**

Run: `uv run python manage.py test access.test_access_report.AccessReportDataTests`

Expected: all data-builder tests PASS.

---

### Task 2: Canonical Markdown Renderer

**Files:**
- Modify: `access/pdf_export.py`
- Modify: `access/test_access_report.py`

**Interfaces:**
- Consumes: `build_access_report_data() -> dict`
- Produces: `render_access_report_markdown(data: dict) -> str`
- Produces: `_markdown_escape(value: str) -> str`

- [ ] **Step 1: Write the failing complete Markdown contract test**

Pass a small literal report dictionary directly to the renderer and assert the
entire string, including blank lines, two-space nested indentation, heading
order, serial bullets, appendix, and one final newline:

```python
expected = """# Zugangsübersicht nach Exportgruppe

## AStA L

### Türen
- **MUC.G43.EG**
  - Haupteingang 008 West

### Transponder
- 010A0SC

# Zusätzliche individuelle Türen

## 02UA77F · Rossmeier, Justus
- **MUC.G43.UG**
  - Raum -103 Lager
"""
self.assertEqual(pdf_export.render_access_report_markdown(data), expected)
```

- [ ] **Step 2: Run the Markdown test and verify failure**

Run: `uv run python manage.py test access.test_access_report.AccessReportMarkdownTests.test_renders_canonical_markdown`

Expected: ERROR because `render_access_report_markdown` does not exist.

- [ ] **Step 3: Implement the minimal deterministic renderer**

Append lines in this exact order: report H1; each section H2; `Türen` H3;
location and nested lock bullets or `_Keine Gruppentüren._`; `Transponder` H3;
serial bullets; appendix H1; appendix transponder H2 and locations, or
`_Keine zusätzlichen individuellen Türen._`. Join with `"\n"`, strip only
surplus trailing blank lines, and add exactly one final newline.

- [ ] **Step 4: Write failing escaping and empty-state tests**

Use values containing `\\`, backticks, `*`, `_`, `[`, `]`, plus values beginning
with `#`, `>`, `-`, `+`, and `=`. Assert each reserved character is escaped once.
Also assert an empty report contains `_Keine Exportgruppen._`, and a section
without doors contains `_Keine Gruppentüren._`.

- [ ] **Step 5: Implement exact escaping rules**

Escape backslashes first, then backticks, asterisks, underscores, and square
brackets everywhere. Escape `#`, `>`, `-`, `+`, and `=` only when one is the
first non-whitespace character:

```python
def _markdown_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for char in ("`", "*", "_", "[", "]"):
        escaped = escaped.replace(char, f"\\{char}")
    return re.sub(r"^(\s*)([#>+\-=])", r"\1\\\2", escaped)
```

- [ ] **Step 6: Run builder and Markdown tests**

Run: `uv run python manage.py test access.test_access_report.AccessReportDataTests access.test_access_report.AccessReportMarkdownTests`

Expected: all tests PASS.

---

### Task 3: Selectable A4 Typst PDF

**Files:**
- Create: `access/templates/pdf/access_report.typ`
- Modify: `access/pdf_export.py`
- Modify: `access/test_access_report.py`

**Interfaces:**
- Consumes report data with `mode == "access_report"`
- Produces: `ACCESS_REPORT_TEMPLATE`
- Produces: `export_access_report_pdf(*, today: datetime.date | None = None) -> bytes`

- [ ] **Step 1: Write the failing template-selection test**

Patch `subprocess.run`, `shutil.which`, and temporary output bytes at the existing
`render_pdf()` boundary. Pass `{"mode": "access_report", ...}` and assert the
copied Typst source is named `access_report.typ`, while existing `changes` still
selects `changes.typ` and matrix/diff still select `matrix.typ`.

- [ ] **Step 2: Run the selection test and verify failure**

Run: `uv run python manage.py test access.test_access_report.AccessReportPdfTests.test_render_selects_access_report_template`

Expected: FAIL because access reports still select `matrix.typ`.

- [ ] **Step 3: Add template selection and the export wrapper**

Define:

```python
ACCESS_REPORT_TEMPLATE = _PDF_DIR / "access_report.typ"

def export_access_report_pdf(*, today: dt.date | None = None) -> bytes:
    return render_pdf(build_access_report_data(today=today), "a4")
```

Select `ACCESS_REPORT_TEMPLATE` when `data.get("mode") == "access_report"`.
Do not add this mode to `MODES`; the existing matrix endpoint and management
command remain unchanged.

- [ ] **Step 4: Build the dedicated Typst template**

The template reads `data.json`, fixes page paper to `a4`, sets selectable system
text, and renders:

- 18pt report title plus generated date;
- each section in a breakable block with 14pt group title;
- `Türen` before location bands and door bullets;
- `Transponder` followed by monospaced exact serial lines;
- `Ohne Gruppe` and all empty states from data;
- a page break before `Zusätzliche individuelle Türen` when group sections exist;
- appendix entries with transponder title, location bands, and door bullets;
- footer date and `Seite N / total`.

Use JSON strings directly as Typst text values; do not parse Markdown or rebuild
grouping in Typst.

- [ ] **Step 5: Add the real Typst integration test**

Under `@unittest.skipUnless(HAVE_TYPST, "typst binary not installed")`, render
normal data containing a collision, `Ohne Gruppe`, and one individual appendix
entry. Assert bytes start with `%PDF` and exceed 1,000 bytes. Render empty data
too, proving all empty-state branches compile.

- [ ] **Step 6: Run PDF tests**

Run: `uv run python manage.py test access.test_access_report.AccessReportPdfTests`

Expected: all PDF tests PASS; real compile tests skip only when Typst is absent.

---

### Task 4: Authenticated Report Page And Download

**Files:**
- Create: `access/templates/access/access_report.html`
- Modify: `access/views.py`
- Modify: `access/urls.py`
- Modify: `access/templates/access/group_list.html`
- Modify: `access/test_access_report.py`

**Interfaces:**
- Consumes: `build_access_report_data()`, `render_access_report_markdown()`, `export_access_report_pdf()`
- Produces: `access_report(request)` at `/groups/access-report/`
- Produces: `access_report_pdf(request)` at `/groups/access-report.pdf`

- [ ] **Step 1: Write failing route and page tests**

Assert GET `/groups/access-report/` returns 200, uses
`access/access_report.html`, exposes canonical Markdown in a read-only textarea,
and includes links/actions named `Markdown kopieren` and `PDF herunterladen`.
Assert the group list contains one `Zugangsübersicht` link to the report route.

- [ ] **Step 2: Write failing PDF endpoint and error tests**

Patch only `pdf_export.export_access_report_pdf` at the view boundary. For PDF
bytes, assert `application/pdf` and the exact server filename pattern
`zugangsuebersicht-YYYY-MM-DD.pdf`. For `RuntimeError`, capture the log, assert a
redirect to the report page and the exact safe German message
`PDF-Export fehlgeschlagen. Bitte Typst-Installation prüfen.`, and assert private
exception text is absent from the response.

- [ ] **Step 3: Write failing authentication tests**

Use the project's `LoginRequiredMiddleware` test setup. Assert anonymous access
to both routes redirects to login and an ordinary authenticated non-staff user
can view and download the report.

- [ ] **Step 4: Run endpoint tests and verify route failures**

Run: `uv run python manage.py test access.test_access_report.AccessReportViewTests access.test_access_report.AccessReportAuthTests`

Expected: FAIL with 404 responses because routes do not exist.

- [ ] **Step 5: Add routes and thin views**

Register fixed routes before `groups/<int:pk>/`:

```python
path("groups/access-report/", views.access_report, name="access_report"),
path("groups/access-report.pdf", views.access_report_pdf, name="access_report_pdf"),
```

The page view builds data once and passes `report` plus `markdown`. The PDF view
calls the export wrapper, catches `RuntimeError`, logs with `logger.exception`,
shows only the approved safe message, and redirects. Successful responses use a
server-generated `Content-Disposition` filename.

- [ ] **Step 6: Write failing copy/fallback and responsive markup tests**

Assert the textarea is `readonly`, focusable, and referenced by Alpine; the copy
button calls an async Clipboard API action; success text lives in an
`aria-live="polite"` status; failure calls textarea `focus()` and `select()` and
shows a manual-copy instruction. Assert actions stack by default and switch to a
row at a small-screen breakpoint.

- [ ] **Step 7: Build the report page in the incumbent visual system**

Start `access/templates/access/access_report.html` with
`{% extends "access/base.html" %}`. Use one heading/action row and one substantial
preview surface, not a grid of equal cards. Put the Markdown in a large
monospaced read-only textarea with high contrast and visible focus. Implement
Alpine state:

```javascript
async copyMarkdown() {
  try {
    await navigator.clipboard.writeText(this.$refs.markdown.value);
    this.status = 'Markdown kopiert.';
  } catch (_error) {
    this.$refs.markdown.focus();
    this.$refs.markdown.select();
    this.status = 'Kopieren nicht möglich. Text ist markiert; bitte manuell kopieren.';
  }
}
```

Add the `Zugangsübersicht` action beside `Zur Matrix` on the group list without
changing group creation behavior.

- [ ] **Step 8: Run all access-report tests**

Run: `uv run python manage.py test access.test_access_report`

Expected: all access-report tests PASS.

---

### Task 5: Documentation And Full Verification

**Files:**
- Modify: `README.md`
- Verify: `access/pdf_export.py`
- Verify: `access/test_access_report.py`
- Verify: `access/views.py`
- Verify: `access/urls.py`
- Verify: `access/templates/pdf/access_report.typ`
- Verify: `access/templates/access/access_report.html`
- Verify: `access/templates/access/group_list.html`

**Interfaces:**
- Consumes the completed report workflow
- Produces user documentation and final verification evidence

- [ ] **Step 1: Document the access report**

Add the report to the README Views section. State that it groups exact export
combinations, lists inherited group doors before exact serials, includes an
individual desired-door appendix, and offers copyable Markdown plus A4 PDF.

- [ ] **Step 2: Format changed Python files**

Run: `uv run ruff format access/pdf_export.py access/test_access_report.py access/views.py access/urls.py`

Expected: formatting completes successfully.

- [ ] **Step 3: Lint changed Python files**

Run: `uv run ruff check access/pdf_export.py access/test_access_report.py access/views.py access/urls.py`

Expected: no lint errors.

- [ ] **Step 4: Run focused report tests**

Run: `uv run python manage.py test access.test_access_report`

Expected: all focused tests PASS.

- [ ] **Step 5: Run the complete application test suite**

Run: `uv run python manage.py test access`

Expected: all tests PASS, including matrix, diff, changes, data transfer, group
labels, and the new report.

- [ ] **Step 6: Run Django system checks**

Run: `KEYMGMT_DEBUG=1 uv run python manage.py check`

Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 7: Run the UI detector once**

Run: `node "/Users/santos/.config/opencode/skills/impeccable/scripts/detect.mjs" --json "access/templates/access/access_report.html" "access/templates/access/group_list.html"`

Expected: inspect every finding, fix new report-page issues, and preserve inherited
global choices rather than redesigning unrelated UI. Do not run the detector a
second time.

- [ ] **Step 8: Inspect final workspace integrity**

Run: `git diff --check && git status --short --branch`

Expected: `git diff --check` is silent; only intended report files plus
pre-existing unrelated changes remain, and no existing work was reverted.
