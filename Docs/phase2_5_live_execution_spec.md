# Aegis Phase 2.5 -- Live Execution Mode Specification

**Status: verified, authoritative.** This document describes what the
system actually does today. The full history of how it got here --
every correction pass, every bug found and fixed, in order -- lives in
`Docs/phase2_5_live_execution_history_appendix.md`. Nothing in this
document should be read as aspirational; if something described here
doesn't match the code, the code is the source of truth and this spec
needs fixing, not the other way around.

---

## 1. Scope

Phase 2.5 adds **live execution**: Aegis can now read a real,
complete table from a trusted PostgreSQL source, propose and validate
a correction against a registered Gold schema, and -- after explicit
human approval -- publish the corrected dataset to a real PostgreSQL
target, safely and reversibly.

Everything before Phase 2.5 (Inspector, Consultant, Governance,
Surgeon, the Healing Manifest, the Schema Registry) operated only on
caller-supplied `sample_data` and never touched a real database beyond
Aegis's own governance store. Phase 2.5 is what makes any of that
correction logic actually consequential outside of Aegis itself.

**What Phase 2.5 does NOT do, deliberately, right now:**

- It does not support `CAST_COLUMN` repairs on the live-capable path.
  Only `RENAME_COLUMN` may execute live. See Section 3.
- It does not preserve exact source-database type fidelity on
  publication (precision/scale/length are not recreated on the
  published table) -- see Section 3's canonicalization policy.
**Verification status:** Phase 2.5 has now been executed against real,
disposable PostgreSQL instances for all three database roles. On
2026-07-22, the targeted live-execution suite passed 32/32 tests and
the complete project suite passed 145/145 tests. The five full-suite
warnings were deprecation notices only; there were no failures or
collection errors. This closes the former real-database verification
gap.

---

## 2. Architecture

### Three databases, never conflated

- **Governance** (`DATABASE_URL` / `TEST_DATABASE_URL`) -- Aegis's own
  storage: `approval_tickets`, `healing_manifests`, `live_executions`,
  `gold_schemas`, `schema_versions`. Managed by Alembic.
- **Live target** (`LIVE_DATABASE_URL` / `LIVE_TEST_DATABASE_URL`) --
  where corrected data is actually published. Must never be the
  governance database (enforced in code). Contains two
  Aegis-controlled schemas, created idempotently at runtime by the
  writer, never by an Alembic migration (see Section 8):
  `aegis_publish` (consumer-facing stable views) and
  `aegis_publish_data` (immutable versioned physical tables plus the
  execution-log marker table).
- **Trusted source** (`SOURCE_DATABASE_URL` / `SOURCE_TEST_DATABASE_URL`)
  -- a real external table Aegis reads a complete, trusted snapshot
  from. Must never be the governance or live-target database.

### Components added or changed in Phase 2.5

- `src/live_execution/source_connector.py` -- reads a complete source
  table under `REPEATABLE READ`, one transaction, ordered by primary
  key. Requires a primary key. Captures rich column metadata (see
  Section 8) and computes schema + dataset fingerprints.
- `src/live_execution/logical_dtype.py` -- the shared logical dtype
  vocabulary (see Section 3), kept dependency-free (no sqlalchemy) so
  `safety.py`'s pure-Python-testable gate logic doesn't transitively
  require a database.
- `src/aegis_dtypes.py` -- the even-more-basic shared vocabulary check
  used by the Schema Registry (Phase 2.4) and `logical_dtype.py`, kept
  at the top level specifically so the earlier phase doesn't have to
  import from the later one.
- `src/live_execution/writer.py` -- `PostgresPublicationWriter`:
  session-level advisory locking, stable-view publication, rollback.
- `src/live_execution/safety.py` -- the mandatory gates a repair must
  pass before live execution (`evaluate_safety_gates`,
  `verify_complete_schema_match`, `verify_output_fingerprint_match`).
- `src/api/app.py` -- new endpoints: `POST /simulate-migration-from-source`,
  `POST /approvals/{ticket_id}/execute-live`, `GET /live-executions/{id}`,
  `POST /live-executions/{id}/rollback`.
- Unchanged from earlier phases: `AegisInspector` (locked V1),
  `AegisConsultant`, `AegisSurgeon`, `RepairSelector`, the Schema
  Registry's version-locking model.

---

## 3. Dtype and repair contract

### The logical dtype vocabulary

Every column, at every stage, is described by one of:

- A standard pandas dtype string (`int64`, `float64`, `object`, `bool`,
  etc.) -- anything `pandas.api.types.pandas_dtype()` accepts.
- One of Aegis's own **extended logical dtypes**, which have no native
  pandas equivalent: `decimal`, `date`, `datetime`, `datetime_tz`,
  `uuid`, `json`.

This distinction exists because `read_complete_source_table()` forces
every column to `dtype=object` when reading from the trusted source --
deliberately, to protect `Decimal`/date/UUID/JSONB precision from
pandas' own type inference, which cannot be trusted not to silently
coerce a precise `NUMERIC` value into a lossy `float64`. But once
everything is `object`-dtype, the raw pandas label is meaningless for
schema comparison -- a genuinely `BIGINT` column would show `"object"`
and never match a Gold column declared `"int64"`. `logical_dtype.py`
recovers the correct logical dtype from captured PostgreSQL column
metadata (for the initial source read) or from actual values / the
original schema (for a post-repair working copy), never from the raw
pandas label alone.

### The Schema Registry accepts a wider set than publication supports

The Schema Registry (`src/aegis_dtypes.py`'s `is_valid_aegis_dtype`)
accepts any standard pandas dtype *or* one of the extended logical
dtypes above -- this is intentionally permissive, since a Gold schema
may be used for sandbox-only analysis where publication never
happens. But `/simulate-migration-from-source` (the live-capable path
specifically) additionally requires every Gold column's dtype to be
**publishable** (`is_publishable_dtype`) -- i.e. to have an explicit
mapping to a PostgreSQL type for publication. A Gold schema declaring
`int32`, `Int64`, `float32`, `string`, `datetime64[ns]`, or `category`
would pass Registry validation but fails this check, and is rejected
before a ticket is ever created.

### CAST_COLUMN is sandbox-only

**This is the most important restriction in this section.** Consultant
may propose a `CAST_COLUMN` repair for any type mismatch. Surgeon
executes every cast via a bare `working_df[column].astype(target_type)`,
and this mechanism has two independent, confirmed failure modes:

1. **Every cast targeting an Aegis extended logical dtype fails
   outright.** Pandas has no native understanding of `"decimal"`,
   `"date"`, `"datetime_tz"`, `"uuid"`, or `"json"` as `astype()`
   targets -- every value fails the cast, `applied=False`,
   `risk_level=HIGH_RISK`.
2. **Casts to completely normal, fully-supported pandas dtypes can
   silently corrupt data while reporting success.** Confirmed directly:
   `float64 -> int64` truncates rather than rounds (`1.9 -> 1`);
   `object -> bool` uses Python's string-truthiness rules, not
   semantic parsing (`"false"` and `"0"` both become `True`, since
   they're non-empty strings); `int64 -> float64` silently loses
   precision for integers beyond 2^53. Surgeon's own safety check
   (`_safe_cast_diagnostics`) only verifies that `astype()` doesn't
   *raise* -- it never checks that the result actually preserves the
   original value. All three of these report `applied=True`,
   `validation.success=True`, pass the schema-completeness gate, and
   get marked `LOW_RISK`.

Given both failure modes, **the live-capable path (`/simulate-migration-from-source`)
rejects every `CAST_COLUMN` repair before a ticket is created,
unconditionally** -- not just casts to extended dtypes. Only
`RENAME_COLUMN` may reach live execution, because a rename never
touches values at all, only column metadata, and carries none of this
risk.

`CAST_COLUMN` analysis remains available on the sandbox-only
`/simulate-migration` endpoint (caller-supplied `sample_data`, never
live-eligible) for whatever exploratory value it has. Verified,
pair-specific lossless cast converters (checking that a value actually
round-trips, not just that `astype()` didn't raise) are future,
separate work, not something bolted onto this phase under time
pressure with no way to verify it against a real database.

### Canonicalization, not exact fidelity, on publication

Publication maps each Gold-declared logical dtype to an unconstrained
PostgreSQL type: `decimal` -> unconstrained `NUMERIC` (not
`NUMERIC(p,s)`), `int64` -> `BIGINT` (smaller integer types are not
separately preserved), `float64` -> `DOUBLE PRECISION`, `object` ->
`TEXT` (not `VARCHAR(n)`). This is a **deliberate, stated policy**:
exact VALUE fidelity is fully preserved (an unconstrained `NUMERIC`
still holds the source `Decimal` with all its original digits; nothing
here rounds or truncates), but the source's schema-level *constraints*
(precision, scale, length) are not recreated on the published table.
Exact schema-level fidelity is a larger, separate piece of work.

---

## 4. Trusted-source workflow

`POST /simulate-migration-from-source` (`schema_name`, `source_schema`,
`source_table`, optional `schema_version`):

1. Look up the Gold schema version from the Schema Registry.
2. Validate every Gold column's dtype is publishable (Section 3) --
   reject 422 if not.
3. Read the complete source table (`read_complete_source_table`):
   one connection, one `REPEATABLE READ` transaction covering table
   existence, primary-key lookup, column metadata, and the full
   ordered read. Requires a primary key; rejects 422 if absent.
   `dtype=object` forced throughout to protect precision.
4. Build the observed schema from captured PostgreSQL metadata
   (`build_source_observed_schema`), not from the (precision-protected
   but schema-meaningless) pandas dtype label.
5. Detect the delta against Gold; let Consultant propose repairs.
6. If the selected repair is `CAST_COLUMN`, reject 422 (Section 3).
7. Otherwise, create a ticket with `live_eligible=True` and full source
   provenance persisted: `source_schema`, `source_table`,
   `source_primary_key`, `source_row_count`, `source_schema_fingerprint`,
   `source_dataset_fingerprint`. Always routes to
   `REQUIRES_HUMAN_APPROVAL`, even for a high-confidence repair --
   `AUTO_APPROVE` never creates a ticket, and execute-live needs one to
   reference.

`sample_data`-backed tickets (`POST /simulate-migration`) are
permanently `live_eligible=False`, enforced both at the API layer and
by a database CHECK constraint (Section 8).

---

## 5. Approval and sandbox workflow

`POST /approvals/{ticket_id}/approve`:

1. Run Surgeon in sandbox mode against the ticket's stored
   `target_dataset`.
2. Persist the Healing Manifest (`save_manifest`), inheriting the
   ticket's own schema-version lineage and source provenance.
3. **For live-eligible tickets specifically**: require
   `verify_complete_schema_match()` to pass -- a completely empty
   schema delta against Gold, using `execution_result.applied` to
   determine whether the repair's declared effect should even be
   trusted (a *failed* cast, or any repair Surgeon didn't actually
   apply, must not be credited with its intended outcome). If this
   fails, the approval is rolled back entirely -- the ticket remains
   `PENDING`, no manifest is persisted, and the response explains why.
   Sample-data tickets never had this expectation and are unaffected.

This exists because Surgeon's own validation only checks column
name/order against Gold -- it can pass even when a second column
still has the wrong type (Consultant proposed repairs for multiple
problems, `RepairSelector` chose one), or when the chosen repair
itself failed to apply. Live publication was already blocked in that
case (Section 6), but without this check the *sandbox manifest itself*
would misleadingly claim full success.

---

## 6. Publication workflow

`POST /approvals/{ticket_id}/execute-live` (`operator`, `logical_target`,
`confirm`):

1. Reject 403 if `AEGIS_LIVE_EXECUTION_ENABLED` isn't exactly `"true"`.
2. Row-lock the ticket; require `APPROVED` status and a recorded
   approval identity.
3. Require a matching sandbox manifest (mode, schema version, repair
   plan all consistent with the ticket).
4. Evaluate safety gates: `live_eligible` must be true (this is *the*
   gate closing the sample_data-can-never-go-live guarantee),
   `confirm` must be true, no unresolved execution already exists for
   this `logical_target`, ticket hasn't already executed live.
5. Acquire the session-level advisory lock for `logical_target`
   (`hold_target_lock`), held on one connection for the **entire**
   remaining flow -- not just the final DDL transaction. Yields that
   connection; `publish()`/`rollback_to_previous()` are given it
   directly rather than opening their own, so the lock's liveness and
   the publication transaction's liveness are tied to the exact same
   Postgres backend.
6. Create the `live_executions` row already `RUNNING`, with the
   ticket's own full provenance.
7. Re-verify the source (`verify_source_unchanged`): re-read it and
   require all four persisted values match (primary key, row count,
   schema fingerprint, dataset fingerprint). A mismatch raises
   `SourceChangedError` (409) -- the world changed since approval, and
   execution is refused rather than publishing stale provenance.
8. Recompute the correction (Surgeon, live mode) against this *fresh*
   read (not the ticket's stored snapshot), so "verified unchanged"
   and "what actually gets published" are provably the same data.
9. Re-check schema completeness (Section 5's gate, defensive here
   since a live-eligible ticket can now only ever carry a
   `RENAME_COLUMN`).
10. Verify the live-recomputed output fingerprint matches the sandbox
    manifest's recorded one.
11. `writer.publish()`: create a new immutable physical version table
    under `aegis_publish_data`, typed from Gold's declared logical
    dtype for each column (never inferred from row values -- an
    all-null column keeps its declared type), inserted through the
    real, explicitly-typed SQLAlchemy `Table` object (not
    `DataFrame.to_sql()`, which bypasses JSONB/UUID/Numeric bind
    processors and falls back to `TEXT` for anything object-dtype).
    Requires an **exact** column-signature match against whatever is
    currently published (same names, order, types, count) if this
    logical target has been published before -- appending columns is
    refused, since Postgres won't allow a rollback to repoint a view
    to a narrower physical table later. Repoints (or creates) the
    stable view via `CREATE OR REPLACE VIEW`.
12. Any exception after step 6 is classified by both execution stage
    and target-side evidence:
    - Before `writer.publish()` begins, the target database cannot have
      changed. Unexpected source, Surgeon, fingerprint, or application
      failures therefore mark the execution `FAILED` and return 500.
    - After publication is attempted, a durable `PUBLISH` marker proves
      the target transaction committed; the governance record is healed
      to `COMPLETED`, including when the original response was lost.
    - A `DBAPIError`, or an `unknown` marker-check result, may represent
      a lost commit acknowledgement. The record remains `RUNNING` and
      returns 503 for later lock-gated reconciliation.
    - For an ordinary application exception, when the marker table is
      reachable and contains no matching `PUBLISH` marker, publication
      is treated as not committed; the record is marked `FAILED` and
      returns 500.

---

## 7. Rollback and reconciliation

`POST /live-executions/{id}/rollback` (`operator`): requires
`COMPLETED` status and that this execution is the *latest* completed
one for its `logical_target` (a newer execution having superseded it
is refused, 409). Acquires the same session lock, repoints the stable
view to the execution's own recorded `previous_physical_table` (or
drops the view entirely if this was the first-ever publish). Never
drops or recreates any physical version table.

**Advisory-lock cleanup never masks the real result.** The lock's
`finally` block performs a best-effort unlock: if the connection is
already dead or in a failed-transaction state (e.g. the publish
transaction itself hit a connection error), the unlock attempt is
allowed to fail -- that failure is logged, never re-raised, so it can
never replace whatever the caller's code already decided (a clean 503,
or a confirmed `COMPLETED` result) with an unrelated connection error.
PostgreSQL releases a backend's session-level advisory locks
automatically when its connection terminates, so a suppressed unlock
failure doesn't leave the lock stuck.

`GET /live-executions/{id}` reconciles a stuck `RUNNING` or
`ROLLING_BACK` record: staleness-gated (only acts after
`AEGIS_LIVE_EXECUTION_STALE_SECONDS`), and only concludes anything by
*additionally proving the session lock is free* -- not just that a
marker happens to be absent right now, which alone would never be
sufficient proof that a transaction has finished failing.

---

## 8. Persistence model

### Governance tables (migration `0003`, applied and verified)

- `approval_tickets`: adds `source_schema`, `source_table`,
  `source_primary_key` (JSONB), `source_row_count`,
  `source_schema_fingerprint`, `source_dataset_fingerprint`,
  `live_eligible` (`NOT NULL DEFAULT false`). **CHECK constraint**:
  `NOT live_eligible OR (all six source_* fields non-null AND
  jsonb_array_length(source_primary_key) > 0)` -- a database-level
  guarantee, not just an API-layer one.
- `healing_manifests`: adds `corrected_output_fingerprint` and the
  same six source-provenance fields (nullable; populated only for
  live-eligible tickets' manifests).
- `live_executions` (new table): `logical_target` /
  `physical_table` / `previous_physical_table` (replacing an earlier
  rename-to-backup design's `target_schema`/`target_table`/
  `backup_table` entirely), status enum, and **all six** source
  provenance fields, `NOT NULL` -- this record is meant to stand alone
  as a complete, independent audit trail, not one requiring a join
  back to the ticket to fully account for what was revalidated
  immediately before publication.

This migration does **not** create `aegis_publish`/`aegis_publish_data`
-- those belong in the live-target database, a different database
entirely from the one this migration runs against.
`PostgresPublicationWriter._ensure_publish_schemas_and_log()` creates
them idempotently, in the correct database, the first time anything is
ever published.

### Source schema fingerprint composition

Computed from, per column, in ordinal order: name, `data_type`,
`numeric_precision`, `numeric_scale`, `datetime_precision`,
`is_nullable`, `character_maximum_length`, `udt_schema`, `udt_name`,
`domain_schema`, `domain_name`, `collation_name`. This detects changes
like `VARCHAR(20) -> VARCHAR(200)`, a domain or collation change, or a
UDT identity change, none of which necessarily change any current
row's *values* and would otherwise be invisible to the fingerprint.

### Dataset codec (ticket/manifest `target_dataset` JSONB)

`DATASET_FORMAT_VERSION = 2`. Every scalar is tagged for round-trip
fidelity (`decimal.Decimal`, `pandas.Timestamp` vs. plain
`datetime.datetime` vs. `datetime.date`, `uuid.UUID`,
positive/negative infinity). Every dict/list value is wrapped in an
explicit `{"__aegis_type__": "raw_json", "value": ...}` envelope
*unconditionally*, not just when it happens to collide with the
reserved key -- a legitimate JSONB value containing
`"__aegis_type__"` as its own data would otherwise be indistinguishable
from an internal tag and get silently corrupted on restore.

New data is always written as version 2. `_dataset_from_json` dispatches
on the payload's own `format_version`: version 1 uses a **legacy**
decoder that reproduces the original (pre-UUID, pre-wrapping) behavior
exactly, since that's genuinely what was written under that version.
A version-1 payload's own reserved-key ambiguity (a legitimate dict
containing `"__aegis_type__"`) is a **documented, unresolvable
limitation of that format** -- not something version 2 can
retroactively fix, since version 1 never recorded which interpretation
was correct.

---

## 9. Safety invariants

In roughly the order they're checked:

1. Live execution is globally disabled by default
   (`AEGIS_LIVE_EXECUTION_ENABLED`).
2. A ticket must be `APPROVED`, by a recorded identity, to execute
   live.
3. `live_eligible` must be true -- enforced at the API layer, at
   execute-live time (not just approval time), and by a database CHECK
   constraint.
4. The sandbox manifest must match the ticket's own lineage
   (schema version, repair plan) and must itself have passed the
   schema-completeness check for live-eligible tickets.
5. No unresolved execution may already exist for this `logical_target`,
   and this ticket must not have already executed live.
6. The source must be re-verified unchanged (all four values)
   immediately before publication, not just at approval time.
7. The recomputed schema must have a completely empty delta against
   Gold -- not just matching column order.
8. The live-recomputed output fingerprint must match the sandbox
   manifest's recorded one.
9. Publication requires an exact column-signature match against
   whatever's currently published, if anything is.
10. The session-level advisory lock and the publication transaction
    share one connection, so the lock's later availability is
    meaningful proof about that specific transaction's fate.
11. An ambiguous exception during publish/rollback never concludes
    `FAILED` from a bare marker check alone -- only a marker-check
    combined with proof the lock is free (reconciliation) can
    conclude that.

---

## 10. Acceptance tests

- `tests/test_inspector.py`, `test_consultant.py`, `test_surgeon.py`,
  `test_selector.py`, `test_approval.py`, `test_schema_registry_logic.py`,
  `test_live_execution_logic.py`, `test_dataset_codec.py` -- 69
  pure-Python tests, all passing.
- `tests/test_api.py`, `test_schema_registry_api.py`,
  `test_live_execution_api.py` -- 76 PostgreSQL-dependent tests using
  three disposable databases: governance, live target, and trusted
  source. Coverage includes trusted-source simulation, rejection of
  unpublishable dtypes and every live `CAST_COLUMN`, approval/sandbox
  validation, typed publication, rollback, crash reconciliation,
  concurrent-access handling, and Alembic migration `0003`.
- Targeted verification on 2026-07-22:
  `tests/test_live_execution_api.py` -- **32 passed, 3 deprecation
  warnings, 0 failures**.
- Complete verification on 2026-07-22:
  **145 passed, 5 deprecation warnings, 0 failures, exit code 0** in
  26.45 seconds against PostgreSQL 16.
- The warnings were limited to the Starlette/httpx TestClient
  deprecation and Alembic's legacy `prepend_sys_path` separator
  behavior. They do not affect Phase 2.5 correctness.
