# Aegis Phase 2.4 -- Schema Registry Spec

## Purpose

Adds a versioned, immutable registry of Gold schemas. Every migration
can now reference an exact, named schema version instead of (or in
addition to) supplying a raw schema inline -- and every approval
ticket and healing manifest can record exactly which schema version it
was validated and executed against ("migration lineage").

## Database architecture

Same `aegis` database, no new service. Two new tables, added by
Alembic migration `0002_schema_registry` (migration `0001` is already
applied to a real database and is never edited):

- `gold_schemas` -- one row per named schema family (e.g.
  `customer_master`). `name` is unique.
- `schema_versions` -- one row per immutable version of a schema.
  `UNIQUE(schema_id, version_number)` and `UNIQUE(schema_id,
  fingerprint)` -- the second constraint is what makes "registering an
  identical definition returns a conflict" a database-enforced
  guarantee, not just an application-level check.

Nullable `schema_version_id` foreign keys are added to the existing
`approval_tickets` and `healing_manifests` tables (also via migration
`0002`) -- nullable so existing records and legacy direct-schema
requests are unaffected.

## Schema-definition format

Not a plain `{name: dtype}` dict -- same reasoning as the Phase 2.3
dataset envelope: Postgres JSONB does not guarantee object key order
survives a round trip. Column order is carried explicitly as an array:

```json
{
  "format_version": 1,
  "columns": [
    {"name": "customer_id", "dtype": "int64"},
    {"name": "customer_name", "dtype": "object"}
  ]
}
```

Validated before registration (`src/registry/schema_definition.py`):
at least one column, non-empty and unique column names, dtypes that
`pandas.api.types.pandas_dtype()` accepts, and a supported
`format_version`. Schema *names* (not column names) are required to
match `^[a-z][a-z0-9_]*$` -- lowercase identifier format.

## Fingerprint

SHA-256 over a canonical JSON representation of `format_version` and
`columns` (name + dtype, in order) only -- `created_by`, timestamps,
and `change_summary` never affect it. Verified directly: identical
definitions produce identical fingerprints, reordering columns changes
it, changing a dtype changes it, and injecting an unexpected extra key
into a column dict does not change it (only `name`/`dtype` are pulled
out explicitly).

## Versioning

Version numbers are server-assigned, starting at 1, strictly
sequential per `schema_id`. Assignment locks the parent `gold_schemas`
row (`SELECT ... FOR UPDATE`) for the duration of computing
`MAX(version_number) + 1` and inserting the new row, so two concurrent
registrations against the same schema name cannot both compute the
same next version number -- the same row-locking pattern already used
for approval-ticket concurrency in Phase 2.3.

Registry history is immutable: no update or delete endpoint exists.

## API surface

Four endpoints, exactly as specified:

- `POST /schemas/{schema_name}/versions` -- registers a new version;
  creates the `gold_schemas` row automatically if the name doesn't
  exist yet. Returns 409 if the submitted definition's fingerprint
  already exists for this schema (not a new version, a genuine
  conflict). Returns 422 if the definition itself is invalid.
- `GET /schemas/{schema_name}/versions/{version_number}` -- exact
  version, full definition + metadata. 404 for unknown schema or
  version.
- `GET /schemas/{schema_name}/versions` -- history, newest first.
  Metadata and fingerprints only, not the full definition per row --
  avoids duplicating potentially large schema definitions across every
  history entry.
- `GET /schemas/{schema_name}/latest` -- highest version_number.

## Migration API integration

`/simulate-migration`'s request model now accepts either:

- `schema_name` (+ optional `schema_version`) -- registry-backed, or
- `gold_schema` -- the existing direct/legacy payload,

but never both and never neither -- enforced by a Pydantic model
validator returning 422. When `schema_name` is given, the referenced
schema version is resolved (specific version, or latest if
`schema_version` is omitted) *before* Inspector/Consultant run, and
its `columns` are converted to the same flat `{name: dtype}` shape
`_build_gold_schema()` already expects -- no duplicate Gold-schema
construction logic.

## Migration lineage

`schema_version_id` on both `approval_tickets` and `healing_manifests`
records exactly which immutable schema version a migration was
inspected, approved, and executed against. Both tables need it
independently: a pending ticket needs lineage before execution
happens at all, and an auto-approved manifest may not have a ticket to
inherit lineage from at all.

The existing Gold-schema snapshot on the ticket (from Phase 2.2/2.3) is
kept even when `schema_version_id` is populated -- the foreign key
establishes lineage, the snapshot preserves the exact execution input
for deterministic replay and audit, independent of whether the
registry entry itself is ever mutated (it can't be, but the snapshot
means audit records don't depend on that guarantee holding forever).

Legacy direct-`gold_schema` requests always have `schema_version_id =
null` -- this is expected, not a bug, and is covered by a dedicated
test.

## What's verified vs. not

Fingerprint computation, schema-definition validation, and dtype
checking are pure Python (plus pandas, which is available in the
sandbox this was built in) and were run directly, not just written --
see the assertions in `tests/test_schema_registry_logic.py`, which
executes cleanly in this environment. Everything touching PostgreSQL
-- the new tables, the row-locked version assignment, the four API
endpoints, lineage persistence -- is written and reasoned through
carefully but **not executed**, for the same reason as every other
Postgres-dependent piece of this project: no Docker, Postgres, or
sqlalchemy in this sandbox. Treat that part as a draft until it's run
for real.
