# Project Aegis — Phase 3.2 Source Ingestion and Dataset Identity Specification

**Status:** APPROVED ARCHITECTURE — implementation not yet started
**Phase branch:** `phase-3.2-source-ingestion-identity`
**Authoritative base:** `c04f46a4dec50ea6ee5d297c2dfc05ae45afb04b`
**Approved:** 2026-08-12
**Current Alembic head:** `0004`
**Next Alembic revision:** `0005`

---

## 1. Authority and purpose

This document is the authoritative safety and delivery contract for Project
Aegis Phase 3.2. It supersedes informal Phase 3.2 planning notes. If an
implementation choice conflicts with this document, implementation must stop
until the specification is deliberately amended and reviewed.

Phase 3.2 introduces durable identity and lineage around Aegis's existing
trusted-source workflow. It must make repeated observations of a source
dataset traceable without weakening any Phase 2.5 live-publication guarantee
or any Phase 3.1 verified-conversion guarantee.

The phase is additive. Existing approval tickets, manifests, live executions,
dataset codec payloads, stable views, immutable physical tables, target-side
markers, and rollback behavior remain valid.

---

## 2. Baseline at phase entry

Phase 3.2 starts from the verified Phase 3.1 merge on `main`:

```text
c04f46a4dec50ea6ee5d297c2dfc05ae45afb04b
```

At this checkpoint Aegis already provides:

1. A PostgreSQL governance database configured by `DATABASE_URL`.
2. A separate trusted PostgreSQL source configured by
   `SOURCE_DATABASE_URL`.
3. A separate live publication database configured by
   `LIVE_DATABASE_URL`.
4. Complete source-table reads inside one read-only `REPEATABLE READ`
   transaction.
5. Deterministic primary-key ordering.
6. Source primary-key, row-count, schema-fingerprint, and
   dataset-fingerprint provenance.
7. Versioned Gold schemas.
8. Human approval and atomic sandbox-manifest creation.
9. Verified `RENAME_COLUMN` and explicitly allowlisted, verified
   `CAST_COLUMN` live execution.
10. Fresh source revalidation immediately before publication.
11. Corrected-output fingerprint equality between sandbox and live
    recomputation.
12. Immutable physical publication tables behind stable views.
13. Advisory locking, target-side markers, reconciliation, and rollback.

The baseline has no durable identity for the configured source database, the
logical source dataset, a particular source observation, or the configured
publication database. Provenance is copied into workflow records but is not
linked through stable source and observation identities.

---

## 3. Problem statement

The current workflow can prove what one ticket observed, approved, repaired,
and published. It cannot yet prove that two workflows refer to the same
durable source dataset over time.

Specifically:

- `SOURCE_DATABASE_URL` locates and authenticates a database but is not a
  safe persisted identity.
- `source_schema` and `source_table` identify a relation only within whichever
  database is currently configured.
- The complete observed DataFrame is stored inside each approval ticket as
  `target_dataset`, coupling source snapshot identity to approval identity.
- Repeated tickets can duplicate the same complete dataset payload.
- The fresh source read performed during live revalidation is not recorded as
  its own observation.
- `logical_target` is a string supplied at execution time, not a registered
  target within a durable publication-system identity.
- Source and publication URLs are independently checked against governance
  database names, but their external database bindings are not compared.

Phase 3.2 resolves these identity and lineage gaps while retaining the exact
repair and publication safety semantics already verified.

---

## 4. Terminology

### 4.1 Source system

A server-configured, non-secret identity representing one trusted PostgreSQL
database binding. It is not a connection URL, user, credential, or caller
input.

### 4.2 Source dataset

One PostgreSQL relation identified by:

```text
source system + source schema + source table
```

A schema or table rename creates a different source-dataset identity. Phase
3.2 does not infer business continuity across relation renames.

### 4.3 Dataset snapshot

An immutable, replayable capture of one complete source-dataset state. It
contains the existing dataset-codec payload plus the exact provenance used to
identify that state.

### 4.4 Ingestion run

An append-only terminal record of one attempted source observation. Separate
runs exist even when they refer to the same deduplicated dataset snapshot.

### 4.5 Simulation run

An ingestion run initiated by `/simulate-migration-from-source`.

### 4.6 Revalidation run

An ingestion run initiated during `/approvals/{ticket_id}/execute-live` to
prove whether the source still matches the approved simulation.

### 4.7 Publication system

A server-configured, non-secret identity representing the PostgreSQL database
behind `LIVE_DATABASE_URL`.

### 4.8 Publication target

One stable consumer target identified by:

```text
publication system + logical target
```

The existing `logical_target` remains the stable-view name and advisory-lock
key.

---

## 5. Scope

Phase 3.2 includes:

1. Stable, non-secret source-system identity.
2. Stable source-dataset identity.
3. Deduplicated immutable dataset snapshots.
4. Append-only simulation and live-revalidation ingestion runs.
5. Ticket, manifest, and live-execution lineage to those runs.
6. Explicit drift-run persistence before live execution is rejected.
7. Stable, non-secret publication-system identity.
8. Registered publication-target identity.
9. Additive lineage fields in existing API responses.
10. Read-only lineage inspection endpoints.
11. A reversible PostgreSQL migration `0005`.
12. PostgreSQL integration and full-regression verification.

---

## 6. Explicit non-goals

Phase 3.2 does not introduce:

- Storage or exposure of credentials, usernames, passwords, or full
  connection URLs.
- Caller-controlled source-system or publication-system identities.
- Automatic rebinding of a known system key to a different database.
- Fabricated identities for historical rows.
- Business-meaning inference.
- Automatic continuity across source table or schema renames.
- Partial-table sampling for a live-eligible workflow.
- Streaming repair, change-data capture, or incremental ingestion.
- Cross-database distributed transactions.
- A change to the existing pandas-based, complete-dataset repair boundary.
- Row dropping, rounding, truncation, clamping, locale guessing, or unsafe
  coercion.
- Expansion of the live CAST allowlist.
- A replacement for `live_execution_id` as the target-side recovery key.
- A replacement for stable views or immutable physical publication tables.
- Deletion of historical snapshots or publication versions.
- An endpoint for callers to register or rebind database systems.

The complete dataset remains read and repaired in memory in Phase 3.2. Large
dataset streaming requires a separate safety design because it must preserve
atomic conversion, deterministic fingerprints, and full-dataset validation.

---

## 7. Mandatory safety invariants

### 7.1 Trust boundary

1. Source and publication system keys come only from server configuration.
2. Requests never accept connection URLs, credentials, source-system keys, or
   publication-system keys.
3. `/simulate-migration-from-source` continues to accept only the registered
   Gold schema reference plus `source_schema` and `source_table`.
4. `/execute-live` continues to take its source exclusively from the approved
   ticket lineage.
5. `logical_target` remains caller supplied, identifier validated, and bound
   to the server-configured publication system.

### 7.2 Observation integrity

1. A successful snapshot always represents a complete source-table read.
2. Table existence, primary key, column metadata, and row data continue to be
   read inside one read-only `REPEATABLE READ` transaction.
3. Primary-key ordering remains mandatory and deterministic.
4. Existing source schema and dataset fingerprint algorithms remain
   unchanged in Phase 3.2.
5. A new provenance fingerprint supplements; it never replaces; the four
   existing provenance values.
6. A snapshot is immutable after insertion.
7. Identical observations of the same source dataset may reuse one snapshot.
8. Every observation still creates a distinct ingestion-run record.
9. Identical content from different source datasets never collapses into one
   snapshot identity.

### 7.3 Approval and sandbox integrity

1. No new live-eligible ticket may exist without a simulation ingestion run.
2. The snapshot used for approval must be the snapshot linked by that run.
3. The copied provenance on the ticket must match the linked snapshot.
4. Existing legacy tickets continue to reconstruct from `target_dataset`.
5. New source-backed tickets reconstruct from their linked snapshot.
6. Approval and sandbox-manifest persistence remain one governance
   transaction.
7. No approved live-eligible ticket may exist without its successful sandbox
   manifest.
8. The manifest must reference the same simulation run as its ticket.
9. Phase 3.1 conversion decision/outcome equality remains mandatory.
10. Corrected datasets remain ephemeral; only the existing corrected-output
    fingerprint and redacted conversion outcome are persisted.

### 7.4 Live revalidation integrity

1. Live execution re-reads the source; it never publishes the older stored
   snapshot directly.
2. The fresh DataFrame used for comparison must be the same fresh DataFrame
   passed to Surgeon.
3. Every completed fresh read creates a revalidation ingestion run.
4. An unchanged source produces `MATCHED` and may continue.
5. A changed source produces `DRIFTED` and publication is blocked with HTTP
   `409`.
6. The changed snapshot and drift run are retained for audit.
7. A source-read failure produces a redacted `FAILED` run when a safe terminal
   result can be persisted.
8. A hard process termination before a terminal run can be written does not
   fabricate a completed run.
9. The live execution links both the approved simulation run and its own
   revalidation run.
10. Existing complete-schema, conversion-outcome, and corrected-output
    fingerprint gates run after source equality is proven and before
    publication.

### 7.5 Publication and rollback integrity

1. `live_execution_id` remains the cross-database publication and recovery
   identifier.
2. `logical_target` remains the advisory-lock key and stable-view name.
3. A new live execution references a registered publication target.
4. Physical table naming remains derived from logical target and live
   execution ID.
5. Publication remains one target-database transaction containing physical
   table creation, data insertion, validation, view replacement, and marker
   insertion.
6. Target-side markers for new executions include the publication-target ID.
7. Existing markers without the new ID remain readable.
8. Rollback continues to require both governance-side latest-execution proof
   and target-side latest-marker proof.
9. Rollback never drops immutable physical versions.
10. Ambiguous outcomes remain `RUNNING` or `ROLLING_BACK` until proven; they
    are never guessed into a retryable state.

### 7.6 Historical compatibility

1. Existing rows are not assigned invented source, dataset, snapshot, run, or
   publication identities.
2. New lineage foreign keys on existing tables are nullable for historical
   rows.
3. Existing copied provenance columns remain populated and authoritative for
   their historical workflow.
4. Dataset payload formats `1` and `2` remain readable.
5. Existing API fields retain their names and meanings.
6. Existing migrations `0001` through `0004` are not rewritten.

---

## 8. Server configuration and binding identity

### 8.1 New production variables

```text
AEGIS_SOURCE_SYSTEM_KEY
AEGIS_PUBLICATION_SYSTEM_KEY
```

### 8.2 New test variables

```text
AEGIS_SOURCE_TEST_SYSTEM_KEY
AEGIS_PUBLICATION_TEST_SYSTEM_KEY
```

Test setup maps the verified test keys to the runtime production-key names in
the same way it maps verified test URLs to runtime URL names. Tests continue
to require exactly `aegis_source_test` and `aegis_live_test`.

### 8.3 Key format

System keys are non-secret, operator-chosen identifiers. They must:

- be 3 to 64 characters;
- start with a lowercase ASCII letter;
- contain only lowercase ASCII letters, digits, `_`, and `-`;
- remain stable across credential rotation;
- never be inferred from a username or password.

Validation pattern:

```regex
^[a-z][a-z0-9_-]{2,63}$
```

### 8.4 Endpoint binding fingerprint

The binding fingerprint is SHA-256 over canonical JSON with format version
`1`. The canonical input contains only:

```json
{
  "database": "<database name>",
  "dialect": "postgresql",
  "host": "<lowercase configured host>",
  "port": 5432,
  "version": 1
}
```

Rules:

1. `postgresql+psycopg` and other explicitly supported PostgreSQL driver
   suffixes normalize to dialect `postgresql`.
2. An omitted port normalizes to `5432`.
3. Username, password, query string, SSL material, and application name are
   excluded.
4. Ambiguous multi-host URLs and hostless URLs are rejected in Phase 3.2
   rather than guessed into an identity.
5. Only the SHA-256 result is persisted. The full canonical input and URL are
   not persisted.
6. Reusing a system key with a different binding fingerprint fails closed.
7. Registering the same binding fingerprint under a second key of the same
   role fails closed.
8. Source and publication binding fingerprints must differ.

The binding protects against accidental configuration drift and ordinary
source/target aliasing. It is not a credential and must not depend on optional
elevated PostgreSQL functions. Planned endpoint moves require a separately
specified administrative rebind procedure; Phase 3.2 does not provide one.

### 8.5 Lazy behavior

Source identity validation occurs only when the trusted-source path is used.
Publication identity validation occurs only when live publication is used.
Sandbox-only imports and `/simulate-migration` remain usable without source
or publication configuration.

---

## 9. Governance data model

All new governance tables use PostgreSQL UUID primary keys and timezone-aware
timestamps. JSON payloads use JSONB. SQLite is not a supported substitute.

### 9.1 `source_systems`

| Column | Type | Null | Contract |
|---|---|---:|---|
| `source_system_id` | UUID | no | Primary key |
| `system_key` | Text | no | Unique validated non-secret key |
| `platform` | Text | no | Must equal `POSTGRESQL` |
| `binding_version` | Integer | no | Must equal `1` in Phase 3.2 |
| `endpoint_binding_fingerprint` | Text | no | Unique 64-character lowercase SHA-256 |
| `created_at` | timestamptz | no | UTC creation time |

Rows are immutable. A known key with a different binding is an error, not an
update.

### 9.2 `source_datasets`

| Column | Type | Null | Contract |
|---|---|---:|---|
| `source_dataset_id` | UUID | no | Primary key |
| `source_system_id` | UUID | no | FK to `source_systems` |
| `source_schema` | Text | no | Validated PostgreSQL identifier |
| `source_table` | Text | no | Validated PostgreSQL identifier |
| `created_at` | timestamptz | no | UTC creation time |

Unique constraint:

```text
(source_system_id, source_schema, source_table)
```

Rows are immutable. A schema/table rename creates a new dataset row.

### 9.3 `dataset_snapshots`

| Column | Type | Null | Contract |
|---|---|---:|---|
| `dataset_snapshot_id` | UUID | no | Primary key |
| `source_dataset_id` | UUID | no | FK to `source_datasets` |
| `provenance_fingerprint` | Text | no | Snapshot deduplication key within dataset |
| `source_primary_key` | JSONB | no | Non-empty ordered array |
| `source_row_count` | Integer | no | `>= 0` |
| `source_schema_fingerprint` | Text | no | Existing algorithm |
| `source_dataset_fingerprint` | Text | no | Existing algorithm |
| `column_metadata` | JSONB | no | Ordered captured PostgreSQL metadata |
| `payload_format_version` | Integer | no | Dataset codec version |
| `snapshot_payload` | JSONB | no | Complete replayable dataset payload |
| `created_at` | timestamptz | no | First capture time |

Unique constraint:

```text
(source_dataset_id, provenance_fingerprint)
```

The provenance fingerprint is SHA-256 over canonical JSON containing format
version `1`, ordered primary-key columns, row count, source schema
fingerprint, and source dataset fingerprint. It supplements rather than
replaces those individually stored fields.

Snapshot payloads use the existing dataset codec. New Phase 3.2 writes use
the current codec version (`2` at phase entry). The decoder continues to
support formats `1` and `2`.

Rows are append-only and immutable. Snapshot payloads are never returned by a
public API.

### 9.4 `ingestion_runs`

| Column | Type | Null | Contract |
|---|---|---:|---|
| `ingestion_run_id` | UUID | no | Primary key |
| `source_dataset_id` | UUID | no | FK to `source_datasets` |
| `dataset_snapshot_id` | UUID | yes | FK; required for complete reads |
| `purpose` | Text | no | `SIMULATION` or `LIVE_REVALIDATION` |
| `outcome` | Text | no | `CAPTURED`, `MATCHED`, `DRIFTED`, or `FAILED` |
| `baseline_ingestion_run_id` | UUID | yes | Self-FK for live revalidation |
| `requested_by` | Text | yes | Operator for live; null when unavailable for simulation |
| `started_at` | timestamptz | no | Observation start |
| `completed_at` | timestamptz | no | Terminal time, not before start |
| `failure_code` | Text | yes | Stable redacted category |
| `failure_reason` | Text | yes | Redacted diagnostic; never raw data or credentials |

Required combinations:

| Purpose | Outcome | Snapshot | Baseline |
|---|---|---:|---:|
| `SIMULATION` | `CAPTURED` | required | null |
| `SIMULATION` | `FAILED` | optional | null |
| `LIVE_REVALIDATION` | `MATCHED` | required | required |
| `LIVE_REVALIDATION` | `DRIFTED` | required | required |
| `LIVE_REVALIDATION` | `FAILED` | optional | required |

An ingestion run is inserted only as a terminal record and is never updated
or deleted through an Aegis repository.

### 9.5 `publication_systems`

| Column | Type | Null | Contract |
|---|---|---:|---|
| `publication_system_id` | UUID | no | Primary key |
| `system_key` | Text | no | Unique validated non-secret key |
| `platform` | Text | no | Must equal `POSTGRESQL` |
| `binding_version` | Integer | no | Must equal `1` |
| `endpoint_binding_fingerprint` | Text | no | Unique 64-character lowercase SHA-256 |
| `created_at` | timestamptz | no | UTC creation time |

Rows are immutable and follow the same rebinding rules as source systems.

### 9.6 `publication_targets`

| Column | Type | Null | Contract |
|---|---|---:|---|
| `publication_target_id` | UUID | no | Primary key |
| `publication_system_id` | UUID | no | FK to `publication_systems` |
| `logical_target` | Text | no | Existing validated stable-view name |
| `created_at` | timestamptz | no | UTC creation time |

Unique constraint:

```text
(publication_system_id, logical_target)
```

Registration does not create or modify a view. Only the existing controlled
publication transaction may do that.

### 9.7 Existing-table lineage additions

#### `approval_tickets`

Add nullable:

```text
source_ingestion_run_id UUID FK -> ingestion_runs.ingestion_run_id
```

Alter `target_dataset` to nullable. Add a check that at least one replay source
exists:

```text
target_dataset IS NOT NULL OR source_ingestion_run_id IS NOT NULL
```

New source-backed tickets store the snapshot once in `dataset_snapshots` and
set `target_dataset` to SQL `NULL`. Legacy and sample-data tickets retain
their embedded payload.

The application must require every newly inserted `live_eligible=true`
ticket to have `source_ingestion_run_id`. Migration `0005` adds this as a
PostgreSQL `NOT VALID` check so it is enforced for future writes without
inventing lineage for historical live-eligible rows.

#### `healing_manifests`

Add nullable:

```text
source_ingestion_run_id UUID FK -> ingestion_runs.ingestion_run_id
```

New source-backed manifests must reference the same simulation run as their
ticket. Historical and sample-data manifests may remain null.

#### `live_executions`

Add nullable:

```text
simulation_ingestion_run_id UUID FK -> ingestion_runs.ingestion_run_id
revalidation_ingestion_run_id UUID FK -> ingestion_runs.ingestion_run_id
publication_target_id UUID FK -> publication_targets.publication_target_id
```

All three are required by the new application path. They remain nullable in
the physical schema only for historical compatibility and for the short
`RUNNING` interval before a terminal revalidation run is persisted.

The six existing copied source-provenance fields remain unchanged and
continue to make each live execution independently auditable.

### 9.8 Indexing

Create indexes for every new foreign key and for:

- `ingestion_runs(source_dataset_id, completed_at)`
- `ingestion_runs(baseline_ingestion_run_id)`
- `dataset_snapshots(source_dataset_id, created_at)`
- `publication_targets(publication_system_id, logical_target)` via its unique
  constraint

Existing live-execution partial unique indexes remain unchanged.

---

## 10. Repository boundaries

Introduce dedicated modules rather than extending API code with direct ORM
queries:

```text
src/identity/binding.py
src/identity/models.py
src/identity/repository.py
src/ingestion/models.py
src/ingestion/repository.py
src/ingestion/service.py
```

Responsibilities:

- `identity.binding`: validate keys, parse URLs, canonicalize bindings, and
  compute endpoint fingerprints without retaining secrets.
- `identity.models`: immutable domain representations for systems, datasets,
  and publication targets.
- `identity.repository`: resolve-or-create identity rows and reject rebinding.
- `ingestion.models`: immutable snapshot and terminal-run domain objects.
- `ingestion.repository`: deduplicate snapshots, append runs, and load
  replayable snapshots.
- `ingestion.service`: orchestrate complete source reads and create redacted
  terminal outcomes.

`src/live_execution/source_connector.py` remains responsible for the atomic
PostgreSQL read. It is extended to return the already-captured ordered column
metadata needed by `dataset_snapshots`; it does not persist governance rows.

`PostgresApprovalRepository` resolves a ticket's dataset through exactly one
of two paths:

1. linked snapshot for new source-backed tickets; or
2. embedded `target_dataset` for legacy and sample-data tickets.

Both paths return the same `ApprovalTicket.target_dataset` DataFrame domain
field, preserving Surgeon and conversion-governance interfaces.

---

## 11. Concurrency and idempotency

1. System registration is serialized by a governance-database transaction
   advisory lock derived from role plus system key.
2. Binding-fingerprint conflicts are rejected, never updated.
3. Source-dataset registration uses the database unique constraint as the
   race-condition backstop.
4. Snapshot deduplication uses the unique
   `(source_dataset_id, provenance_fingerprint)` constraint as the backstop.
5. On a concurrent identical insert, the repository rolls back the failed
   savepoint and loads the already-created row.
6. Ingestion runs are never deduplicated.
7. Publication-target registration uses its unique constraint as the
   backstop.
8. Existing ticket row locks, logical-target advisory locks, and live
   execution partial unique indexes remain the authoritative execution
   concurrency controls.

---

## 12. Lifecycle contracts

### 12.1 Source-backed simulation

1. Validate `AEGIS_SOURCE_SYSTEM_KEY`.
2. Resolve and verify the source-system binding.
3. Prove it does not match the configured publication binding when both are
   available.
4. Resolve or create the source-dataset identity.
5. Start observation timing.
6. Read the complete source table through the existing atomic connector.
7. On complete read, serialize the dataset with the existing codec.
8. Resolve or create the immutable dataset snapshot.
9. Append a `SIMULATION/CAPTURED` ingestion run.
10. Continue observed-schema construction, diagnosis, repair selection, and
    conversion preflight.
11. If a ticket is required, create it in the same governance transaction as
    the captured run and snapshot reference.
12. If no repair plan survives, retain the run and snapshot and return their
    IDs with the existing no-repair response.
13. If the source cannot be read, append a redacted `SIMULATION/FAILED` run
    when possible and return the existing error classification.

No ticket is created for a failed or rejected source read.

### 12.2 Approval

1. Row-lock the pending ticket.
2. Resolve its DataFrame from linked snapshot or legacy embedded payload.
3. For linked snapshots, verify run purpose/outcome, source-dataset lineage,
   and copied provenance equality.
4. Apply existing conversion-governance checks.
5. Tentatively mark approved.
6. Run Surgeon in sandbox.
7. Apply existing full-schema, conversion-outcome, and output-fingerprint
   checks.
8. Save the manifest with the ticket's simulation ingestion-run ID.
9. Commit approval and manifest together.
10. On any failure, roll back so the ticket remains `PENDING`.

### 12.3 Live execution

1. Apply the existing global kill switch.
2. Lock and validate the approved ticket and manifest.
3. Resolve the ticket's source-system and source-dataset lineage.
4. Verify the current source configuration still binds to that exact source
   system.
5. Resolve and verify the publication-system binding.
6. Reject equal source/publication endpoint bindings.
7. Resolve or create the publication target for `logical_target`.
8. Apply all existing database-only safety gates.
9. Acquire the existing logical-target advisory lock.
10. Create the `RUNNING` live record linked to simulation run and publication
    target.
11. Perform a fresh complete source read.
12. Resolve or create the fresh snapshot.
13. Append a revalidation run linked to the simulation baseline.
14. Link the live execution to that revalidation run.
15. If any of primary key, row count, schema fingerprint, or dataset
    fingerprint differs, record `DRIFTED`, mark the live execution `FAILED`,
    and return HTTP `409` without touching the target.
16. If all four match, record `MATCHED` and continue with the fresh DataFrame.
17. Run existing live Surgeon, conversion, complete-schema, and corrected-
    output fingerprint gates.
18. Publish through the existing writer.
19. Record publication-target ID in the target-side marker.
20. Apply the existing completion or ambiguous-outcome logic unchanged.

### 12.4 Inspection and reconciliation

`GET /live-executions/{id}` continues to reconcile `RUNNING` and
`ROLLING_BACK` records using staleness, target locking, and markers. New
lineage IDs are returned additively.

Reconciliation never creates a missing ingestion run after the fact unless
the complete source observation and its provenance were durably captured.

### 12.5 Rollback

Rollback does not re-read the source and does not create an ingestion run. It
uses the live execution's registered publication target plus the existing
governance and target-marker staleness checks.

For new markers, publication-target ID must agree with the live execution.
For historical markers where it is null, existing logical-target and
live-execution-ID checks remain authoritative.

---

## 13. Target-side marker evolution

The target-side `_aegis_execution_log` lives under
`LIVE_DATABASE_URL`, not the governance database. Alembic must not create or
alter it.

`PostgresPublicationWriter._ensure_publish_schemas_and_log()` must
idempotently execute the equivalent of:

```sql
ALTER TABLE aegis_publish_data._aegis_execution_log
ADD COLUMN IF NOT EXISTS publication_target_id UUID;
```

New PUBLISH and ROLLBACK markers populate the field. Historical rows remain
null. The column has no cross-database foreign key.

Marker creation remains inside the same transaction as the corresponding
view change.

---

## 14. API contract

### 14.1 Existing request models

No breaking request change is allowed for:

- `SimulateFromSourceRequest`
- `ApprovalDecisionRequest`
- `ExecuteLiveRequest`
- `LiveRollbackRequest`

System keys remain server configuration and are not accepted in these
models.

### 14.2 Additive response lineage

Source-backed simulation responses add, where available:

```text
source_system_id
source_dataset_id
dataset_snapshot_id
ingestion_run_id
```

Approval inspection adds `source_ingestion_run_id`.

Live execution responses add:

```text
simulation_ingestion_run_id
revalidation_ingestion_run_id
publication_target_id
```

No existing response field is removed or redefined.

### 14.3 New read-only endpoints

The minimum inspection surface is:

```text
GET /source-datasets/{source_dataset_id}
GET /source-datasets/{source_dataset_id}/ingestion-runs
GET /ingestion-runs/{ingestion_run_id}
GET /publication-targets/{publication_target_id}
```

These endpoints return identity, timestamps, outcome, row count, and
fingerprints as appropriate. They never return:

- snapshot row data;
- connection URLs;
- usernames or credentials;
- raw failing values;
- unredacted conversion diagnostics.

There are no public create, update, delete, or rebind endpoints for these
entities in Phase 3.2.

---

## 15. Error semantics

Existing status meanings remain unchanged.

New classifications:

| Condition | HTTP | Meaning |
|---|---:|---|
| Missing/invalid server system key | 500 | Server configuration error |
| Known key bound to different endpoint | 409 | Configuration identity conflict |
| Source and publication bindings equal | 422 | Unsafe topology for live workflow |
| Unknown lineage ID in inspection | 404 | Resource not found |
| Ticket/snapshot lineage inconsistency | 422 | Persisted safety evidence invalid |
| Source differs during revalidation | 409 | Approved world state changed |
| Ingestion persistence fails before publish | 500 | No publication attempted |

Failure messages must be redacted. They may include system key, schema,
table, stable IDs, and fingerprint mismatch categories. They must not include
connection URLs, passwords, raw row values, or unredacted conversion
diagnostics.

---

## 16. Migration `0005`

### 16.1 Upgrade

Migration `0005_source_ingestion_identity.py` revises `0004` and must:

1. Create `source_systems`.
2. Create `source_datasets`.
3. Create `dataset_snapshots`.
4. Create `ingestion_runs`.
5. Create `publication_systems`.
6. Create `publication_targets`.
7. Add the nullable lineage foreign keys to existing tables.
8. Alter `approval_tickets.target_dataset` to nullable.
9. Add the replay-source check constraint.
10. Add future-write lineage checks using PostgreSQL `NOT VALID` where
    historical live rows cannot satisfy the new condition.
11. Create required indexes.

The upgrade performs no historical identity backfill.

### 16.2 Downgrade

Before restoring `approval_tickets.target_dataset` to non-null, downgrade must
materialize the linked snapshot payload into every Phase 3.2 ticket whose
embedded payload is SQL `NULL`.

It must then:

1. Drop new check constraints and indexes.
2. Drop existing-table lineage foreign keys and columns.
3. Restore `target_dataset` to non-null.
4. Drop new tables in reverse dependency order.

Downgrade must preserve the replayability of Phase 3.2-created tickets. It
must fail rather than drop lineage if a required snapshot cannot be
materialized.

### 16.3 Model parity

`src/db/models.py` and Alembic `0005` must describe equivalent columns,
constraints, and indexes. Tests using `Base.metadata.create_all()` must see
the same new-schema contract, except for deliberately historical
`NOT VALID` migration behavior.

---

## 17. Security and privacy

1. System keys are non-secret but still validated and bounded.
2. Only endpoint-binding hashes are persisted.
3. URL objects and credentials must not appear in ORM models, Pydantic
   responses, logs, exception messages, manifests, or test snapshots.
4. Dataset snapshot payloads remain internal governance data and are never
   returned through Phase 3.2 APIs.
5. Conversion evidence remains redacted exactly as in Phase 3.1.
6. Ingestion failure reasons use stable categories plus sanitized messages.
7. Test fixtures use only dedicated disposable databases.
8. No test may fall back from a test URL or test system key to production
   configuration.

---

## 18. Required tests

### 18.1 Pure identity tests

- Valid and invalid system-key formats.
- Credential rotation does not change the binding fingerprint.
- Username, password, query string, and driver suffix do not affect binding.
- Host, port, or database changes do affect binding.
- Hostless and ambiguous multi-host URLs fail closed.
- Known key plus changed binding is rejected.
- Duplicate binding under another key is rejected.
- Equal source/publication bindings are rejected.
- No secret appears in serialized objects or errors.

### 18.2 Source-dataset tests

- Same system/schema/table resolves the same dataset ID.
- Different source systems never share dataset identity.
- Different schema or table creates a different dataset ID.
- Unsafe identifiers remain rejected.
- Concurrent get-or-create resolves one durable identity.

### 18.3 Snapshot tests

- Exact codec-v2 replay of Decimal, UUID, JSONB, date, datetime, infinity,
  index, column order, and object dtype.
- Existing codec-v1 payloads remain readable.
- Same dataset and provenance reuse a snapshot ID.
- Same content in different source datasets produces different snapshot IDs.
- Primary-key-only changes alter provenance identity.
- Schema-only changes alter provenance identity.
- Row-value changes alter provenance identity.
- Snapshot repository exposes no update/delete path.

### 18.4 Ingestion-run tests

- Repeated identical simulation creates two runs referencing one snapshot.
- Initial successful run is `SIMULATION/CAPTURED`.
- Matching live revalidation is `LIVE_REVALIDATION/MATCHED`.
- Changed live revalidation is `LIVE_REVALIDATION/DRIFTED` and blocks live
  publication.
- Failed source read records only redacted diagnostics.
- Invalid purpose/outcome/snapshot/baseline combinations are rejected.
- Runs are listed deterministically.

### 18.5 Approval and manifest tests

- New source ticket stores SQL `NULL` in `target_dataset` and reconstructs
  from linked snapshot.
- Legacy embedded tickets still reconstruct identically.
- Missing or mismatched run/snapshot lineage blocks approval.
- Approval and manifest remain atomic.
- Manifest references the ticket's exact simulation run.
- Existing conversion decision/outcome tests remain unchanged.

### 18.6 Live execution tests

- Current source binding must match the ticket's registered source system.
- Source and publication endpoint equality blocks execution before target
  mutation.
- Live record links simulation run, revalidation run, and publication target.
- Matching revalidation uses the fresh DataFrame for Surgeon.
- Drifted revalidation persists evidence and marks the live attempt failed.
- Publisher marker includes publication-target ID.
- Historical null marker remains readable.
- Existing ambiguous-commit and reconciliation tests continue to pass.
- Existing concurrency and rollback tests continue to pass.

### 18.7 Migration tests

- `0005` upgrades cleanly from `0004`.
- Expected tables, columns, FKs, constraints, and indexes exist.
- Historical rows remain unchanged with null lineage.
- A new live-eligible ticket without run lineage is rejected.
- Downgrade materializes snapshot payloads into tickets before dropping
  lineage.
- `0005 -> 0004 -> 0005` succeeds on representative legacy and new rows.

### 18.8 Full regression

Every pre-Phase-3.2 test must continue to pass, including:

- dataset codec;
- approval;
- schema registry;
- conversion governance;
- verified type repair;
- source validation and provenance;
- live CAST allowlist;
- stable-view publication;
- precision preservation;
- source-change blocking;
- ambiguous-outcome reconciliation;
- concurrency; and
- rollback.

---

## 19. Delivery sequence

### 3.2.1 — Discovery and safety contract

- Inspect models, source connection, source connector, fingerprinting,
  approval persistence, live repository, writer, API lifecycle, migrations,
  environment guards, and tests.
- Approve this specification.
- Commit specification only; no runtime behavior changes.

### 3.2.2 — Source and dataset identity model

- Implement binding/key validation.
- Add ORM models and Alembic `0005`.
- Implement identity repositories.
- Verify upgrade/downgrade and identity concurrency.

### 3.2.3 — Immutable snapshots and ingestion runs

- Implement snapshot provenance fingerprint.
- Implement snapshot deduplication and terminal run persistence.
- Move new source-ticket replay to linked snapshots.
- Preserve legacy embedded-ticket replay.

### 3.2.4 — Drift and provenance enforcement

- Integrate simulation lineage.
- Integrate approval/manifest lineage.
- Persist live revalidation runs.
- Record `MATCHED`, `DRIFTED`, and redacted `FAILED` outcomes.
- Enforce source binding continuity.

### 3.2.5 — Publication identity integration

- Implement publication-system and target resolution.
- Enforce source/publication binding separation.
- Link live executions to registered targets.
- Extend target markers additively.
- Preserve reconciliation and rollback.

### 3.2.6 — PostgreSQL verification

- Apply migrations against dedicated PostgreSQL test databases.
- Run targeted identity, ingestion, API, publication, and rollback tests.
- Verify real source drift and recovery paths.
- Verify downgrade with Phase 3.2-created data.

### 3.2.7 — Regression, documentation, and closure

- Run compilation and full tests.
- Review secrets, migrations, API compatibility, and repository cleanliness.
- Produce implementation and PostgreSQL verification reports.
- Close on a clean Phase 3.2 branch before controlled merge.

---

## 20. Closure criteria

Phase 3.2 is not complete until all of the following are proven:

1. This specification is committed as the Phase 3.2 authority.
2. Migration `0005` upgrades and downgrades successfully.
3. No credential or connection URL is persisted or exposed.
4. Source-system rebinding fails closed.
5. Source and publication binding equality fails closed.
6. Source datasets have stable identities.
7. Repeated observations create distinct runs and deduplicated snapshots.
8. Cross-source identical content remains identity-separated.
9. New tickets and manifests link to the exact simulation run.
10. Legacy tickets and codec payloads remain replayable.
11. Live revalidation produces an auditable `MATCHED`, `DRIFTED`, or
    redacted `FAILED` run.
12. Drift blocks publication before any target mutation.
13. Live executions link simulation, revalidation, and publication identity.
14. Target-side markers retain crash-recovery authority.
15. Stable-view publication and rollback remain unchanged in semantics.
16. All targeted PostgreSQL tests pass.
17. The complete pre-existing and Phase 3.2 test suite passes.
18. Compilation passes.
19. Temporary test infrastructure is removed or restored.
20. The repository is clean and the closure commit is locally and remotely
    verified before merge.

---

## 21. Locked decisions summary

| Decision | Locked result |
|---|---|
| Source identity authority | Server configuration only |
| Publication identity authority | Server configuration only |
| Credential persistence | Forbidden |
| Endpoint rebinding | Fail closed; no automatic update |
| Source dataset identity | Source system + schema + table |
| Snapshot identity | Dataset-scoped provenance fingerprint |
| Repeated identical read | New run, reused snapshot |
| Cross-source deduplication | Forbidden |
| Initial and live observations | Separate ingestion runs |
| Historical lineage | Leave null; do not fabricate |
| New source-ticket dataset storage | Linked snapshot; embedded payload null |
| Legacy ticket storage | Existing embedded payload |
| Source read | Complete, deterministic, repeatable-read |
| Live repair input | Fresh revalidated DataFrame |
| Drift | Persist evidence and block publication |
| Publication version key | Existing `live_execution_id` |
| Publication lock key | Existing `logical_target` |
| Physical publication | Existing immutable table + stable view |
| Rollback | Existing marker-verified view repoint |
| Governance migration | Reversible `0005` |
| Streaming/CDC | Out of scope |

---

## 22. Reference

PostgreSQL documents `pg_control_system()` as exposing a cluster-wide system
identifier. Phase 3.2 deliberately does not require that optional server
metadata for its mandatory binding contract; identity behavior must not vary
according to elevated function access:

- https://www.postgresql.org/docs/current/functions-info.html
