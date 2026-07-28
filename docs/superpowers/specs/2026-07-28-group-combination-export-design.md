# Dynamic Group Combination Labels

## Goal

Keep groups as reusable atomic door sets while presenting each transponder's
combination as one compact, stable label. Use the same label in every PDF export
and in the transponder list so different target programmings are easy to scan.

Examples:

- `Allgemein` -> `AStA A`
- `Allgemein` + `Lager` -> `AStA L`
- `Allgemein` + `Lager` + `Technik` -> `AStA LT`

Combined groups are derived for display and export only. They are not persisted
as additional `Group` records.

## Existing Behavior

`Group` is a named many-to-many door set, and a transponder may belong to
multiple groups. Group assignments feed the transponder's flat desired-lock set
through `access/soll.py`. Matrix and Soll/Ist PDF data currently carry every
assigned group name and render them as a comma-separated list. The changes PDF
does not currently display groups.

This design does not change group door inheritance, assignments, or Soll
propagation.

## Data Model

Add two fields to `Group`:

- `export_code`: a stable, uppercase code of 1-4 ASCII letters or digits. Codes
  are case-insensitively unique.
- `is_implicit`: marks the one group whose code is omitted when it appears in a
  combination with any other group. At most one group may be implicit.

The database enforces code uniqueness and at most one implicit group. Model and
endpoint validation provide user-facing errors before database constraints are
reached.

`export_code` is generated when a group is created and remains unchanged when
the group is renamed. A user may explicitly edit it later.

## Code Generation

Generate a code from the group name as follows:

1. Remove a leading `AStA ` prefix case-insensitively.
2. Normalize the remaining name to uppercase ASCII alphanumeric characters.
3. Try its prefixes from one through four characters, choosing the shortest
   code not already used case-insensitively.
4. If no prefix is available, reject creation with a validation message asking
   for a custom code.

For example, `AStA Lager` first tries `L`; if occupied, it tries `LA`, `LAG`,
then `LAGE`. Existing codes are never changed to accommodate a new group.

## Combination Formatter

Put combination naming in one shared Python function. It accepts an iterable of
groups and returns the display/export label.

1. Return an empty string when no groups are assigned.
2. Sort groups case-insensitively by full group name.
3. If multiple groups are present, omit the group marked `is_implicit`.
4. If the implicit group is the only group, retain its code.
5. If all remaining codes contain one character, concatenate them directly.
6. If any remaining code contains multiple characters, join all codes with
   hyphens to prevent ambiguous splits between multi-character codes.
7. Prefix the suffix with `AStA `.

Examples:

- implicit `A` alone -> `AStA A`
- implicit `A` + `L` -> `AStA L`
- implicit `A` + `L` + `T` -> `AStA LT`
- `L` + `T` without the implicit group -> `AStA LT`
- `LA` + `T` -> `AStA LA-T`

The implicit setting affects this label only. It does not imply membership or
change any access rights. Labels are compact display values, not globally
unique database identifiers.

## Group Management

Creating a group still requires only a name; the server generates its initial
code. The create form also accepts an optional custom code for names whose
one-to-four-character prefixes are exhausted. The group detail view displays
the code and the implicit setting and lets the user edit either.

Updates follow these rules:

- Normalize custom codes to uppercase before validation.
- Reject blank, malformed, or duplicate codes with HTTP 400 JSON responses.
- Keep the code stable on rename.
- When a group is marked implicit, atomically clear the previous implicit group
  and mark the selected group. The operation locks the relevant rows so the
  database's single-implicit constraint remains valid under concurrent updates.

The group list should include each group's code so generated values are visible
without opening every detail page.

## Data Migration

Use a staged migration so existing rows can be populated before `export_code`
becomes required and unique:

1. Add nullable metadata fields.
2. Process existing groups in case-insensitive name order using the normal code
   derivation algorithm.
3. Mark the existing group named `AStA Allgemein` as implicit when present.
4. Add the final non-null, format, uniqueness, and single-implicit constraints.

With the current database, the derived codes are `A`, `L`, `S`, `T`, and `U`.
Identifying `AStA Allgemein` is migration-specific; runtime formatting contains
no hard-coded group names or combination table.

## PDF Exports

All PDF modes use the shared formatter:

- Matrix export: each transponder's group band renders one combined label.
- Soll/Ist export: the same group band renders the same label.
- Changes worklist: each changed transponder carries and displays the combined
  label near its heading.

PDF data builders prefetch groups and expose one `group` string per transponder
instead of rebuilding names in Typst. Ungrouped transponders display no label.
The templates only render the supplied string, keeping naming policy in Python.

## Transponder List

Prefetch groups in the transponder list query and compute the label with the
shared formatter, without per-row database queries.

- Desktop: add a `Gruppe` column between `ASTA` and `Türen`, displaying the
  label as a compact chip.
- Narrow screens: display the label beneath the holder name so it remains
  visible when secondary columns are hidden.
- Ungrouped transponders: display a muted dash.
- Search: include the combined label in each row's client-side search data, so
  a query such as `AStA LT` finds that programming combination.

## Dashboard Index

Prefetch groups for the dashboard's transponder cards and compute each card's
label with the same shared formatter used by exports and the full transponder
list. Render a compact group-label chip beside the serial chip. Ungrouped cards
keep only the serial chip and render no fallback marker.

The dashboard keeps its current card ordering, door counts, and summary counts.
Group prefetching adds one fixed query and must not introduce per-transponder
queries.

## Error Handling

Invalid group metadata must not produce a server error or a partially updated
record. Creation and update endpoints return HTTP 400 with a concise JSON error
for malformed codes, duplicate codes, or exhausted automatic generation. UI
actions surface that message and leave the current values unchanged.

Export formatting tolerates an empty group iterable but relies on database
constraints for valid codes and a single implicit group.

## Testing

Add focused tests for:

- automatic code generation, `AStA ` prefix stripping, normalization, and
  shortest-prefix collision fallback;
- exhausted generation and custom code validation;
- case-insensitive code uniqueness;
- stable codes across group renames;
- atomic replacement of the implicit group;
- formatter behavior for no group, implicit-only, implicit combinations,
  combinations without the implicit group, alphabetical ordering, multi-letter
  separators, and ungrouped transponders;
- matrix, Soll/Ist, and changes data containing the same combined label;
- Typst rendering for all PDF modes;
- transponder-list desktop/mobile markup, search metadata, blank state, and
  absence of an N+1 query.
- dashboard group-chip rendering, ungrouped-card behavior, and absence of an
  N+1 query.

Existing Soll tests remain unchanged and act as regression coverage that export
metadata does not alter access inheritance.

## Out Of Scope

- Persisting combined groups such as `AStA LT`.
- Changing group-to-door or group-to-transponder relationships.
- Changing desired-lock propagation or current/planned lock semantics.
- Reordering transponders in exports by group combination.
