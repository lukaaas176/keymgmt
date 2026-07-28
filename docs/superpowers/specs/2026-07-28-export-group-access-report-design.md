# Export Group Access Report

## Goal

Add a website report that communicates the complete group-defined target access
in a mail-friendly form. Users can copy canonical Markdown or download an A4 PDF
with identical content.

The report groups by derived export labels such as `AStA A`, `AStA L`, and
`AStA LT`. It does not emit separate atomic-group sections. Each export-group
section lists its group-derived doors first and its exact transponder serials
second. A final appendix lists additional individual desired doors by
transponder.

## Scope And Definitions

An **export group** is one exact tuple of groups assigned to one or more
transponders. Its visible title is produced by the existing
`combined_group_label()` formatter.

The export group's doors are the union of `Group.doors` across every group in
that exact tuple. Individual transponder exceptions are not included in this
door union.

An **additional individual door** is a door in a transponder's `desired_locks`
that is not present in the union of doors inherited from that transponder's
assigned groups. This is Soll data, not current active/planned/removed state.

The report preserves transponder serials exactly as stored, including leading
zeroes.

## Architecture

Extend `access/pdf_export.py` with one shared report-data builder and a canonical
Markdown renderer. The builder returns plain ordered data that contains:

- report title and generation date;
- export-group sections with an exact membership key, display title, grouped
  doors, and transponder serials;
- individual-door appendix entries grouped by transponder.

The Markdown renderer and Typst template consume this same data. Neither output
re-queries models or implements independent grouping, collision, or ordering
rules.

Add a dedicated Typst template at
`access/templates/pdf/access_report.typ`. Existing Typst subprocess handling is
reused by selecting the template for the new report mode.

## Export Group Construction

Load all transponders with `groups__doors` and `desired_locks` prefetched. Group
transponders by a stable tuple of assigned group export codes sorted
case-insensitively. The membership tuple, not the display label, is the internal
identity.

For each membership tuple:

1. Derive the normal title with `combined_group_label()`.
2. Union and deduplicate every assigned group's doors by lock serial.
3. Group those locks by `Lock.location`; use `Lock.area` when location is blank,
   then `Ohne Standort` when both are blank.
4. Display each lock using `Lock.label`, which adds a room number only when it
   provides information not already present in the door name.
5. Sort locations case-insensitively, with `Ohne Standort` last.
6. Sort locks within a location by label case-insensitively, then serial.
7. Sort transponder serials case-insensitively, then by the original serial.

Sort export-group sections by their normal derived label, then by membership
tuple. Add the ungrouped section last under the title `Ohne Gruppe`. Its
group-derived door list is empty, and it includes all ungrouped transponder
serials.

## Display Label Collisions

Different membership tuples can produce the same derived label. For example,
an implicit `A` plus `L` and `L` alone may both display as `AStA L` while having
different door unions.

Never merge those sections. Detect duplicate normal titles after grouping and
annotate only colliding sections with their complete membership codes:

- `AStA L (A+L)`
- `AStA L (L)`

Non-colliding labels remain unchanged. `Ohne Gruppe` is not collision-annotated.

## Individual Door Appendix

After all export-group sections, add `Zusätzliche individuelle Türen`.

For each transponder:

1. Union the doors inherited from its assigned groups.
2. Subtract that union from `desired_locks`.
3. Skip the transponder when the difference is empty.
4. Use the exact serial as the subsection's primary identity and append
   ` · <label>` when `Transponder.label` differs from the serial.
5. Group and sort doors with the same location and lock ordering rules used by
   export-group sections.

Sort appendix entries by transponder serial case-insensitively, then by the
original serial. For ungrouped transponders, every desired lock is individual.
If no transponder has additional individual doors, render
`Keine zusätzlichen individuellen Türen.` as an explicit empty state.

## Canonical Markdown

The copied text follows this structure:

```markdown
# Zugangsübersicht nach Exportgruppe

## AStA L

### Türen
- **MUC.G43.EG**
  - Haupteingang 008 West
  - Notausgang Ost 001

### Transponder
- 02UEHS1
- 03UAG03

# Zusätzliche individuelle Türen

## 02UA77F · Rossmeier, Justus
- **MUC.G43.UG**
  - Raum -103 Lager
```

Each export-group section always contains `Türen` before `Transponder`. An empty
group-derived door list renders `_Keine Gruppentüren._`. An empty report renders
`_Keine Exportgruppen._` before the individual appendix.

Escape backslashes first, then backticks, asterisks, underscores, and square
brackets in all database-derived headings and list values. Also escape `#`, `>`,
`-`, `+`, and `=` when one is the first non-whitespace character of a value. The
renderer returns one final newline and uses two-space indentation for nested
door bullets so the source remains readable as Markdown and plain email text.

## PDF

The PDF is A4 portrait and uses selectable text. It contains:

- `Zugangsübersicht nach Exportgruppe` and the generation date;
- one section per export group in canonical order;
- location bands and group-derived door lists;
- exact serial lists after the doors in each section;
- the complete individual-door appendix;
- page number and generation date in the footer.

The Typst template owns pagination and visual hierarchy but no grouping logic.
It renders the same empty states as Markdown. The download filename is
`zugangsuebersicht-YYYY-MM-DD.pdf` and is generated by the server.

## Website Flow

Add a `Zugangsübersicht` action to the group list. It opens
`/groups/access-report/`, an authenticated report page containing:

- a short explanation of group-derived and individual Soll access;
- the canonical Markdown in a large read-only textarea;
- a `Markdown kopieren` button;
- a `PDF herunterladen` link.

The copy action uses `navigator.clipboard.writeText()` and reports success in an
ARIA live status. The textarea remains focusable and manually selectable as a
fallback when clipboard access is unavailable. The layout follows existing
Tailwind/Alpine conventions and stacks controls and preview on narrow screens.

The dedicated authenticated PDF endpoint is
`/groups/access-report.pdf`. Existing matrix/diff/changes export URLs and
controls remain unchanged.

## Error Handling

- Database-derived report construction is read-only.
- A missing or failing Typst compiler redirects to the report page and shows
  `PDF-Export fehlgeschlagen. Bitte Typst-Installation prüfen.` The server logs
  the detailed exception; command arguments and filesystem paths are not shown
  to the user.
- Clipboard failure leaves the textarea available, selects its content when
  possible, and displays a concise instruction to copy manually.
- Empty groups, empty door unions, no transponders, and no individual doors are
  valid report states, not errors.

## Testing

Add focused tests for:

- exact-membership grouping and derived export labels;
- union and deduplication of assigned group doors;
- exclusion of individual desired doors from export-group door unions;
- location fallback and deterministic location/lock/serial ordering;
- `Ohne Gruppe` placement and all-desired-is-individual behavior;
- collision-only membership annotation;
- individual desired doors minus inherited group doors;
- appendix title labels and omission of transponders with no individual doors;
- canonical Markdown hierarchy, indentation, escaping, final newline, and empty
  states;
- fixed query count without per-group or per-transponder queries;
- Typst rendering of normal, collision, ungrouped, and individual sections;
- authenticated report and PDF endpoints, safe filename, content type, and
  missing-Typst errors;
- group-list action, responsive preview/actions, clipboard success state, ARIA
  status, and manual-copy fallback;
- regression coverage for matrix, diff, changes, website data import/export,
  and existing group-label behavior.

## Out Of Scope

- atomic-group-only report sections;
- current Ist, planned, or pending-removal individual access;
- individual doors mixed into export-group door unions;
- editing group or transponder assignments from the report;
- arbitrary report filtering or page-size selection;
- attaching or sending email from the application;
- changing existing combined-label rules.
