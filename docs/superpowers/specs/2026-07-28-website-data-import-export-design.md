# Website Data Import And Export

## Goal

Add a website workflow for downloading and restoring all Schliessmatrix domain
data as one portable backup. The import supports both exact replacement and a
deterministic merge in which imported records win.

The feature is implemented directly in the current `main` worktree and must
preserve the existing uncommitted changes.

## Scope

The backup contains:

- locks and all lock metadata;
- groups, including export codes, implicit-group metadata, and door membership;
- transponders and all transponder metadata;
- active, planned, pending-removal, and desired lock relationships;
- transponder group assignments.

The backup never contains Django users, password hashes, sessions, permissions,
content types, migrations, configuration, or other framework data. Existing PDF
and image import and PDF report export behavior remains unchanged.

Every authenticated user may import and export backups, matching the existing
application-wide authorization model.

## Architecture

Create a focused `access/data_transfer.py` module that owns:

- the current backup format name and schema version;
- deterministic domain-data serialization;
- JSON and schema validation;
- referential and domain-constraint validation;
- transactional replace and merge operations;
- structured result counts for the web layer.

The Django views only enforce HTTP behavior, pass uploaded content and the mode
to the domain module, and translate known validation failures into messages. The
template only presents controls and results. Neither layer duplicates backup
schema or merge policy.

## Backup Format

The export is UTF-8 JSON with a root object containing:

- `format`: a fixed application-specific format identifier;
- `version`: an integer schema version;
- `exported_at`: an ISO 8601 UTC timestamp for user reference;
- `locks`: lock records;
- `groups`: group records;
- `transponders`: transponder records.

Records use stable domain identifiers rather than database row IDs:

- locks use their serials;
- transponders use their serials;
- groups use their stable export codes.

Each lock record contains every scalar `Lock` field. Each group record contains
its name, export code, implicit flag, and a list of door serials. Each
transponder record contains every scalar `Transponder` field, lists of lock
serials for active, planned, pending-removal, and desired access, and a list of
assigned group export codes.

Dates use ISO 8601 date strings. Nullable dates use JSON `null`. Collections and
relationship identifier lists are sorted so repeated exports of unchanged data
differ only in `exported_at`. Unknown root or record fields are rejected rather
than silently discarded, preventing unnoticed data loss when an incompatible
file is imported.

## Export Behavior

An authenticated GET endpoint reads all records and relationships inside one
database transaction to build a consistent snapshot. It returns an attachment
named `schliessmatrix-backup-YYYY-MM-DD.json`. The response uses the JSON content
type, a safe server-generated filename, and private/no-cache headers because the
file contains sensitive access-control data.

Serialization prefetches relationships and must not issue one query per lock,
group, transponder, or relationship.

## Import Validation

Import accepts exactly one JSON file. File-only validation happens before
opening the write transaction. Validation that depends on retained database
records runs again inside the write transaction before its first mutation, so a
concurrent request cannot invalidate merge assumptions. The importer validates:

- valid UTF-8 and JSON syntax;
- root and record shapes, required keys, and permitted keys;
- the exact format identifier and a supported schema version;
- field types, lengths, nullability, date syntax, and model constraints;
- unique lock serials, transponder serials, group export codes, and group names;
- valid group export-code syntax and case-insensitive uniqueness;
- at most one implicit group;
- every relationship reference against either an imported record or, in merge
  mode, an existing retained record;
- merge-time group identity conflicts, including an existing group name paired
  with a different export code.

Empty files, oversized uploads, invalid files, and unsupported future versions
produce specific German error messages. Validation errors do not expose stack
traces or mutate data.

## Replace Mode

`Alle Daten ersetzen` makes the domain state exactly match the backup:

1. Validate the complete file without mutation.
2. Require the dedicated replacement-confirmation checkbox in the POST.
3. Enter one database transaction.
4. Delete existing `access` domain records in relationship-safe order.
5. Create all imported records and then all relationships.
6. Commit only after every record and relationship succeeds.

Auth and framework records remain untouched. Any write error rolls back the
deletions and additions together.

## Merge Mode

`Zusammenführen` retains records absent from the file and applies imported
records as authoritative:

- upsert locks and transponders by serial;
- upsert groups by case-insensitive export code;
- replace every scalar field on a matching imported record;
- replace that imported group's door membership with the backup value;
- replace each imported transponder's four lock sets and group assignments with
  the backup values;
- leave unrelated records and their relationships unchanged.

If the import declares an implicit group, clear the existing implicit marker
before assigning it to the imported group. If no imported group is implicit, an
existing retained implicit group remains implicit. A group-name/export-code
identity conflict rejects the whole import rather than guessing which group is
intended.

Merge validation may resolve relationships to existing retained locks or groups
that are not repeated in the backup. All merge writes run in one transaction,
and any failure rolls back the entire merge.

## Website UI

Add a `Daten` page linked from both desktop and mobile navigation. It contains:

- an export card explaining that the download contains all Schliessmatrix data
  but no login accounts;
- a single download button;
- an import card with a `.json` file input;
- a clear choice between `Zusammenführen` and `Alle Daten ersetzen`;
- a replacement warning and confirmation checkbox shown for replace mode;
- short explanations of imported-record precedence and retained records.

The import form uses multipart POST and normal CSRF protection. The replace
confirmation is enforced server-side, not only hidden or shown in JavaScript.
The page follows the established Tailwind and Alpine patterns and remains usable
on narrow screens.

After success, the page reports created and updated counts by record type and
states which mode ran. Replace mode reports replaced totals. On failure, the
page retains no uploaded file and shows a concise German message instructing the
user to correct or choose the file again.

## Security And Failure Handling

- Existing login middleware protects both endpoints; no additional staff or
  superuser restriction is added.
- CSRF protects import, while export remains a read-only GET.
- User filenames never become filesystem paths or response filenames.
- The importer parses uploaded content without invoking external programs or
  accepting archives.
- Django's configured upload limits provide the hard request cap; the importer
  also rejects content over 10 MiB before JSON parsing.
- Known format and validation failures produce user-safe messages.
- Unexpected database errors are logged through Django's logging configuration,
  display a generic failure message, and leave all domain data unchanged.

## Testing

Add focused tests for:

- deterministic export ordering and response headers;
- all scalar fields and every many-to-many relationship in the schema;
- exclusion of users and framework data;
- export query behavior without per-record queries;
- a populated export, database clear, import, and exact domain-state comparison;
- replace-mode confirmation and exact replacement semantics;
- merge creation, imported-record precedence, relationship replacement, and
  preservation of unrelated records;
- merge relationships that reference retained locks and groups;
- implicit-group replacement and retention behavior;
- duplicate identifiers, invalid field values, malformed dates, invalid UTF-8,
  malformed JSON, empty and oversized files;
- missing references, group name/code conflicts, and multiple implicit groups;
- wrong format identifiers and unsupported schema versions;
- complete rollback after an injected write-phase failure;
- login requirements, CSRF behavior, and safe content-disposition headers;
- German controls, warnings, validation messages, and success summaries;
- desktop and mobile `Daten` navigation links and responsive page structure.

Existing PDF/image import and PDF export tests provide regression coverage that
the new data-transfer endpoints do not change those workflows.

## Out Of Scope

- exporting or restoring login accounts;
- importing arbitrary Django fixtures or SQLite databases;
- CSV or spreadsheet interchange;
- scheduled or off-site backups;
- partial table selection;
- previewing or editing backup contents in the browser;
- backward compatibility with schema versions that have never shipped.
