# Phase 2.5 -- Historical Appendix

**This is the historical correction-pass record, not the active
specification.** The authoritative, current specification is
`Docs/phase2_5_live_execution_spec.md`. Everything below this line is
preserved for audit/history only: the original design documents (now
retired), and the full text of every correction pass that has been
made since, in the order they happened. Nothing below should be
treated as describing the system's current behavior without checking
the authoritative spec first.

---

# Aegis Phase 2.5 -- Live Execution Mode Spec

**Read this first: this document is written in the order it was
built, not in order of what's currently true.** The sections below
titled "Purpose" through the early correction passes describe the
ORIGINAL rename-to-backup, caller-supplied-sample_data design, which
is retired. Jump to **"FINAL LOCKED ARCHITECTURE"** for what the
system actually does today (trusted-source ingestion + stable-view
publication), and to the correction-pass sections after it for what's
been fixed since. A full reorganization putting the current
architecture first was requested in review and is a reasonable ask --
it hasn't been done yet, since restructuring a document this size
under time pressure risks losing or garbling content by transcription
error more than it risks a reader scrolling past some history. This
pointer is the interim fix.

## Purpose

Adds controlled, PostgreSQL-only publication of a Surgeon-corrected
dataset into an explicitly configured external target table, with
rollback. This is the first phase where Aegis writes anywhere other
than its own governance database or an in-memory sandbox copy.

## Two design decisions not fully pinned down by the locked
## architecture -- made explicit here rather than assumed silently

**1. Cross-database "atomicity" is staged, not a single literal
transaction.** The locked design describes one sequence ending in a
single commit, but `live_executions` (the audit record) lives in the
governance database (`DATABASE_URL`), while the shadow/backup/promote
DDL runs against a *different* connection (`LIVE_DATABASE_URL`) --
even when they happen to be the same physical Postgres server, they
are different SQLAlchemy engines/sessions. Plain SQLAlchemy cannot
commit across two connections as one atomic unit without distributed
transaction machinery (two-phase commit), which nothing in the locked
architecture asks for. So: the target-database sequence (create
shadow, write, validate, backup existing, promote) genuinely is one
atomic transaction -- if anything in it fails, Postgres rolls the
*target* database back to exactly its pre-execution state, guaranteed.
The governance record's job is to accurately reflect what happened,
not to be part of the same commit as the target transaction. It is
created already `RUNNING`, in one committed insert (`create_running()`
-- see "Second correction pass" below for why a separate `PENDING`
stage was tried first and then removed), then updated to `COMPLETED`
or `FAILED` based on the outcome. The target table itself can never be
left half-mutated by this design, regardless of how the governance
record's own update turns out.

**2. "The target is not the original source table" (safety gate #11)
originally had no concrete data to check against.** Nothing in Aegis
tracks a live source-table reference -- data arrives as JSON
`sample_data`, not a query against a live table via the read-only
connector. The first correction pass made `source_schema`/
`source_table` **mandatory** request fields specifically because an
optional check that a caller could simply omit isn't a safety gate at
all -- see "Correction pass" below. This section originally proposed
making them optional; that proposal did not survive review.

## Scope boundary (restated from the locked decisions)

PostgreSQL only. `LIVE_DATABASE_URL` is a separate connection from
`DATABASE_URL` and is validated at startup to not be `aegis` or
`aegis_test`. No raw connection strings accepted through any API
request -- only `target_schema`/`target_table` (validated identifiers,
not connection info). A dedicated write component
(`src/live_execution/writer.py`), not an extension of the existing
read-only connector.

## Identifier safety

Schema/table/column names are going into dynamically-built DDL
(`CREATE TABLE`, `ALTER TABLE ... RENAME TO`), and SQL has no bind-
parameter mechanism for identifiers the way it does for values. The
mitigation is strict allowlist validation *before* any string ever
touches a SQL statement: `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$` (also
respects Postgres's 63-byte identifier limit), enforced on
`target_schema`, `target_table`, every column name in the dataset
being written, and the allowlisted-schema check.

## Dtype mapping

Pandas dtype strings are mapped to explicit Postgres column types
(`int64`->`BIGINT`, `float64`->`DOUBLE PRECISION`, `object`->`TEXT`,
`bool`->`BOOLEAN`, `Int64`->`BIGINT`, `datetime64[ns]`->`TIMESTAMP`).
An unmapped dtype is a hard error, not a silent fallback to `TEXT` --
silently widening a numeric column to text on a real external table
is exactly the kind of quiet corruption this project exists to
prevent elsewhere.

## Reusing Surgeon's existing "live" mode

`AegisSurgeon.execute(..., execution_mode="live", allowed_modes=
["sandbox", "live"])` was already there from Phase 1 -- confirmed
directly: with `execution_mode="live"` it mutates the caller's
DataFrame in place (no defensive copy) instead of returning a new
one, and `"live"` is rejected with `ValueError` unless explicitly
listed in `allowed_modes`. `execute-live` calls this exactly once,
deterministically recomputing the same correction already validated
in sandbox at approval time -- consistent with "every repair must be
deterministic." This is separate from how the SANDBOX-time output is
captured for fingerprinting, which the second correction pass changed
significantly -- see below.

## Safety gates (all 15, evaluated before any write)

Implemented in `src/live_execution/safety.py`. Two raise distinct
exception types mapped to different HTTP statuses: state conflicts
(already executed, another execution in flight against the same
target) raise `LiveExecutionConflictError` -> 409; everything else
(missing lineage, wrong risk tier, destructive repair, non-allowlisted
schema, missing confirmation, etc.) raises
`LiveExecutionNotAllowedError` -> 422. The environment kill switch
(`AEGIS_LIVE_EXECUTION_ENABLED`) is checked first, separately, and
maps to 403 -- that's a configuration/authorization question, not a
request-validity one.

## What's verified vs. not

Identifier validation, dtype mapping, and the safety-gate logic
against fake ticket/manifest objects are pure Python and were run
directly. Everything touching PostgreSQL -- the live writer's actual
DDL execution, the migration, the three new endpoints, rollback --
is written and reasoned through carefully but **not executed**, same
constraint as every Postgres-dependent piece of this project before
it. This phase raises the stakes on that gap more than any prior one:
treat every line touching the live writer as a draft until it's run
against a real throwaway Postgres target, not just `aegis_test`.

## Correction pass

A review found nine real issues, all addressed:

1. **Rolled-back tickets could re-execute** -- the partial unique index
   only covered COMPLETED. Now covers PENDING, RUNNING, COMPLETED,
   ROLLING_BACK, and ROLLED_BACK -- everything except FAILED (which
   stays retryable) is one-shot per ticket.
2. **Concurrent requests could publish twice** -- the ticket is now
   row-locked (lock_for_live_execution(), reusing the same
   SELECT ... FOR UPDATE mechanism as ticket approval) before the
   check-then-create sequence, and a second partial unique index on
   (target_schema, target_table) for PENDING/RUNNING is the
   database-enforced backstop if two different tickets race for the
   same target. create_pending() catches the resulting IntegrityError
   and converts it to a clean 409.
3. **A Surgeon failure left the execution stuck RUNNING** -- the
   Surgeon recomputation now happens inside the same try/except as
   the target-database write.
4. **Cross-database crash recovery** -- a durable execution-log marker
   table lives inside the target database itself (same schema,
   written in the same transaction as the shadow/backup/promote
   sequence), independent of whether the governance-database update
   after it succeeds. GET /live-executions/{id} reconciles a stuck
   RUNNING record against this marker on read, rather than a
   background sweep -- a deliberately scoped choice, not a full
   reconciliation service.
5. **Rolling back an older execution could destroy newer data** --
   rollback now checks both the governance database (is this the
   latest COMPLETED execution for this target?) and the target-side
   marker (is this still the most recent PROMOTE for this exact
   table?) before proceeding, and transitions through a new
   ROLLING_BACK state first.
6. **Arbitrary existing tables could be silently replaced** -- an
   existing target with no prior PROMOTE marker (proof Aegis created
   it) is refused outright. Aegis's simplified shadow-table schema
   (column names + basic types only) doesn't preserve primary keys,
   foreign keys, indexes, defaults, triggers, or grants, and Postgres's
   object-identity-based dependency tracking means anything referencing
   the old table by its renamed-backup identity wouldn't follow the
   rename anyway.
7. **Source-vs-target protection was optional** -- source_schema/
   source_table are now required fields, always checked, not skipped
   when omitted.
8. **Live output wasn't proven identical to sandbox output** --
   HealingManifestRecord.corrected_output_fingerprint (a SHA-256 over
   column order, dtypes, index, and every row's values) is computed at
   sandbox time and compared against a fresh fingerprint of the live
   recomputation; a mismatch blocks publication. Getting the corrected
   DataFrame out of Surgeon's "sandbox" mode (which discards its
   internal copy) without modifying Surgeon.py required a second,
   throwaway call using "live" mode's in-place-mutation behavior,
   purely to fingerprint -- verified directly that a matching case
   passes and an intentionally diverged repair plan is caught.
9. **Generated shadow/backup names could overflow Postgres's 63-byte
   identifier limit** -- confirmed directly that a 63-character target
   table name caused the naive version to silently truncate away the
   entire suffix, including the shadow/backup marker itself, making
   the two names identical and colliding across different executions.
   Fixed with a fixed-length suffix and controlled truncation of the
   table-name portion.

Also added: operator validation (strip + reject empty, both request
models), a CHECK constraint on status, explicit Alembic-level
verification that migration 0003 creates the expected tables,
columns, and indexes (the rest of this test file uses Base.metadata
directly for speed, which doesn't exercise the migration itself), and
tests for target-write failure, Surgeon failure, concurrent rollback,
a rolled-back ticket attempting re-execution, and a stale rollback
after a newer execution supersedes the target.

**Local verification note:** if your Postgres data volume predates
this phase, docker/init-test-db.sql won't re-run automatically (it
only fires on first container initialization) -- create
aegis_live_test manually first; see the runbook at the top of
tests/test_live_execution_api.py.

## Second correction pass

A further review found the first correction pass had real gaps of its
own -- including that my fix for the Surgeon-failure issue had been
silently undone by a later reordering I made to support fingerprint
checking, without re-verifying it. Nine issues, all addressed:

1. **Surgeon failure was STILL unprotected** -- moving Surgeon earlier
   (to support the fingerprint gate) put it back outside the
   try/except. Fixed by restructuring the whole endpoint: all non-
   Surgeon gates evaluate first, the execution record is created
   (already RUNNING, see #5), and ONLY THEN does Surgeon run, inside
   the same try/except as fingerprint verification and target
   publication.
2. **ROLLING_BACK wasn't in the target-level in-flight index** -- a
   target actively being rolled back is still busy; a new publication
   could start against it mid-rollback. Fixed in both the model and
   the migration.
3. **Rollback crash recovery didn't exist** -- reconcile_running()
   only handled the promote side. Added reconcile_rolling_back(),
   using the target-side ROLLBACK marker the writer already records.
4. **Reconciliation could fail an operation still legitimately in
   progress** -- the original design reconciled the instant it saw no
   marker, which would wrongly fail a target write that just hadn't
   committed yet. Both reconciliation paths are now staleness-gated
   (AEGIS_LIVE_EXECUTION_STALE_SECONDS, default 300s) and only act
   once a record has sat unchanged longer than that.
5. **PENDING could be permanently stranded** -- create_pending() and
   mark_running() were separate commits with a crash window between
   them and no reconciliation path for PENDING. Replaced with
   create_running(), which creates the record already RUNNING in one
   committed insert.
6. **The stored fingerprint wasn't from the actual sandbox execution**
   -- it came from a second, redundant Surgeon call, which only
   proved two invocations agreed with each other, not that either
   matched the real manifest. Fixed properly: HealingManifest now
   carries corrected_dataset (never persisted -- manifest_repository.py
   doesn't reference the field, so it's simply not written to the
   database), populated by Surgeon itself. Required touching
   Surgeon.py and the manifest dataclass, both previously
   deliberately left alone -- verified first that test_surgeon.py
   only does field-by-field assertions, never whole-object equality,
   so adding an optional field was safe, and separately verified that
   compare=False was necessary (DataFrame equality isn't a plain
   bool and would otherwise break dataclass auto-eq the moment two
   manifests were ever compared).
7. **Manifest-to-ticket consistency wasn't checked** -- added explicit
   verification that the sandbox manifest's execution_mode, lineage,
   and repair plan all match the ticket before proceeding.
8. **A historical name match didn't prove current table identity** --
   if an Aegis-published table were dropped and something unrelated
   recreated with the same name, the old marker would have still
   matched by name. Every PROMOTE marker now records the table's
   Postgres OID (via to_regclass()), and both the managed-target check
   and rollback's staleness check compare current OID against the
   marker's, not just the name.
9. **LIVE_DATABASE_URL didn't verify it was actually PostgreSQL** --
   now checks parsed.drivername and rejects an empty database name.

Also fixed: evaluate_safety_gates() no longer takes fingerprint
parameters (they required Surgeon to have already run, which
conflicts with gate evaluation happening before Surgeon per #1) --
verify_output_fingerprint_match() is now a separate function, called
inside the protected block after Surgeon actually recomputes.

**This documentation is being updated in the same commit as the code
it describes**, specifically to avoid the kind of drift a prior
review caught (spec still describing source fields as optional and
Surgeon being called once, after the implementation had already
changed).

## Third correction pass

A further review found the staleness-timeout approach from the second
pass could still race a legitimately slow operation, plus five more
issues. Fixed:

1. **A timeout alone can't prove death** -- reconciliation now also
   requires acquiring a Postgres transaction-scoped advisory lock
   (pg_try_advisory_xact_lock, keyed by hashtext(schema)/hashtext(table))
   before concluding a markerless record has failed. Confirmed via
   Postgres documentation that this lock type releases automatically on
   commit OR rollback -- including a crash, since the connection drops
   and the transaction never commits -- which is exactly the property
   that turns "looks stale" into "provably nothing is running." If the
   lock can't be acquired, something is genuinely still active and the
   record is left alone regardless of elapsed time. promote()/
   rollback() now acquire this same lock at the start of their own
   transactions, both as another layer of protection against a
   concurrent operation on the same target and to make the lock
   meaningful for reconciliation to check.
2. **An uncertain commit could be wrongly marked FAILED** -- a generic
   exception from the writer no longer means automatic FAILED. The
   target-side marker is checked first: if it shows PROMOTE/ROLLBACK
   actually committed, the record is marked COMPLETED/ROLLED_BACK
   instead (the original request's response was lost, not the
   operation); if the marker table itself can't even be reached, the
   record is left as-is with an outcome-unknown note, not guessed in
   either direction.
3. **A restored table lost its "managed" status** -- the ownership
   check now uses the latest marker of EITHER type (PROMOTE or
   ROLLBACK), not just the latest PROMOTE. A rollback's restored OID
   is just as authoritative as a promotion's; using PROMOTE-only kept
   pointing at whatever the rollback had just superseded.
6. **Approval operator wasn't validated, and status alone didn't prove
   identity** -- `ApprovalDecisionRequest.operator` now uses the same
   strip-and-reject validation as the live-execution request models.
   `execute-live` additionally requires `ticket.decided_by` to be
   non-empty and `ticket.decided_at` to exist before proceeding, as a
   defense against any ticket that predates this validation.
7. **The live test database guard was too permissive** -- it accepted
   any database except `aegis`/`aegis_test`. Now requires exactly
   `aegis_live_test` and a PostgreSQL driver.

Also fixed two stale/contradictory passages this review caught: the
"two design decisions" section still described `source_schema`/
`source_table` as optional after a later pass made them mandatory, the
governance-record lifecycle was still described as PENDING-then-RUNNING
after `create_running()` replaced that with one committed insert, and
`manifest_repository.py`'s comment still claimed Surgeon wasn't touched
after the second correction pass added `corrected_dataset` to it.

## Two issues NOT implemented in this pass -- flagged for a decision,
## not resolved unilaterally

**Blocker 4 (publishing `sample_data`, not a verified complete
dataset)** and **blocker 5 (rename-based table swapping breaking
Postgres object identity for views/FKs/grants)** are not bugs in this
implementation of the locked architecture -- they are challenges to
the architecture itself, and in blocker 5's case, a direct
contradiction of what was explicitly locked in (the rename-to-backup
mechanism was specified by name in the original Phase 2.5 decisions).

Blocker 4 would require Aegis to read a complete, trusted dataset from
a real source system with some proof of completeness -- Aegis has
never done this, in any phase; `sample_data` supplied directly in the
request has been the entire data-ingestion model since Phase 1.
Solving it properly is a data-ingestion redesign, not a Phase 2.5 bug
fix, and deserves the same kind of explicit scope discussion Phase 2.5
itself got before implementation began.

Blocker 5's proposed fix (a dedicated publication schema with either a
stable view over versioned physical tables, or a verified-no-
dependencies restriction) is a different publication mechanism than
the one actually locked in. Replacing it unilaterally would mean
overriding your own prior decision based on a review's suggestion,
which isn't a call I think is mine to make alone.

Both are real, and both are more architecturally significant than
anything fixed in the three correction passes so far. They need your
decision on direction before any code changes, not my guess at one.

## Fourth correction pass -- session-level locking

A further review found the transaction-scoped lock from the third
pass still left a real gap, plus two related ordering issues. Fixed:

1. **The advisory lock began too late.** It only existed inside
   promote()'s own transaction -- meaning a slow Surgeon call (between
   create_running() and promote() being reached) had NO lock
   protection at all. If that took longer than the staleness
   threshold, reconciliation could see "no lock, no marker" and
   incorrectly conclude FAILED while the original request was still
   legitimately working. Locking is now SESSION-level
   (pg_advisory_lock/pg_advisory_unlock, not pg_advisory_xact_lock),
   held by the CALLER via writer.hold_target_lock() for the entire
   flow -- acquired before create_running(), released only after
   mark_completed()/mark_failed(). promote()/rollback() no longer
   acquire their own lock; they require the caller to already hold it.
2. **Ambiguous-commit resolution needed to hold the lock while
   checking the marker**, not test-then-separately-check as two
   steps (which left its own small window). writer now exposes two
   distinct methods for two distinct situations: check_operation_outcome()
   -- a plain marker check, for use when the caller already holds the
   lock itself (execute_live's and rollback's own exception handlers,
   still inside their `with hold_target_lock(...)` block -- trying to
   re-acquire the SAME lock from a different connection there would
   just see the caller's own hold and misreport "active" for the
   wrong reason); and determine_outcome_under_lock() -- acquires the
   lock and checks the marker together, atomically, for reconciliation
   from a SEPARATE later request that does not already hold it.
3. **Rollback's identity check now runs inside the same transaction
   and connection that performs the mutation**, not on a separate
   connection before the transaction even opens. With the caller
   already holding the session lock for the whole operation this
   window was already closed in practice, but checking on the same
   connection removes any doubt.

Added a test that directly holds a target's lock on a separate
connection (simulating a still-in-progress Surgeon call) and confirms
GET does not mark the record FAILED while genuinely active, even past
the staleness threshold -- the exact race this pass closes.

## Two issues still not implemented -- explicitly awaiting the user's
## own confirmation, not inferred from a review document

A subsequent review proposed detailed "locked architecture decisions"
for blockers 4 (trusted source ingestion: a new SOURCE_DATABASE_URL,
a new `/simulate-migration-from-source` endpoint, source fingerprint
verification) and 5 (a publication redesign: dedicated
aegis_publish/aegis_publish_data schemas, stable views over versioned
physical tables, replacing the rename-to-backup mechanism entirely).

Both proposals are thorough and directly responsive to the concerns
raised. Neither has been implemented. The user was explicitly asked,
in the prior turn, to weigh in on exactly this decision -- a review
document proposing a specific design is not the same thing as the
user confirming they want it built, especially at this scope (roughly
a second Phase 2.5's worth of new work) and especially for blocker 5,
which would reverse a mechanism the user explicitly locked in at the
start of Phase 2.5. This is called out here so the gap is never
ambiguous: implementation of 4 and 5 starts only after the user says
so directly.

## FINAL LOCKED ARCHITECTURE (supersedes the rename-based design above)

The user confirmed directly, in chat, that this redesign is required
before live execution -- not an optional extension, not a later
phase. This section is the locked spec; implementation follows it.
Everything above describing the rename-to-backup mechanism and
caller-supplied sample_data as a live-execution input is HISTORICAL,
kept for context on how the design evolved, not a description of the
current system.

### Trusted source

- New `SOURCE_DATABASE_URL`, validated the same way as
  `LIVE_DATABASE_URL` (must be PostgreSQL, must have a database name,
  lazy import so existing tests importing app.py don't break).
- `POST /simulate-migration-from-source` is the live-capable
  simulation path. Given `schema_name`, `schema_version`,
  `source_schema`, `source_table`, it: validates the source
  identifiers, confirms the source table has a primary key, reads the
  COMPLETE table in a read-only REPEATABLE READ transaction ordered
  deterministically by the primary key, computes a source schema
  fingerprint (column names + Postgres types) and a source dataset
  fingerprint (reusing compute_dataframe_fingerprint -- the same
  function that already proves sandbox/live consistency elsewhere),
  runs sandbox Surgeon against that complete dataset, and persists all
  of this provenance on both the ticket and the manifest.
- The existing `POST /simulate-migration` (sample_data) path remains
  for sandbox-only analysis. Tickets from it are permanently
  `live_eligible = false` -- checked at execute-live time, not just at
  approval time, since a caller could otherwise approve a sample_data
  ticket and still attempt to execute it live.
- `execute-live` no longer accepts source_schema/source_table from the
  caller at all -- source identity comes only from the approved
  ticket's own persisted provenance. Before publishing, Aegis re-reads
  the source table and requires the SAME source dataset fingerprint as
  what was approved; any change blocks execution and requires a new
  simulation and approval. This is the same "prove it still matches
  what was approved" pattern already used for the sandbox/live
  Surgeon-output fingerprint -- applied one layer earlier, to the
  input rather than just the correction.

### Stable-view publication

The rename-to-backup mechanism is retired entirely -- not hardened
further. Two fixed schemas, not caller-supplied:

- `aegis_publish_data` -- immutable physical version tables, one per
  execution, named `<logical_target>__<execution_suffix>`, and the
  target-side marker table. Never renamed, never dropped by Aegis
  (including on rollback).
- `aegis_publish` -- stable, consumer-facing views. A live execution
  does `CREATE OR REPLACE VIEW aegis_publish.<logical_target> AS
  SELECT <explicit columns> FROM aegis_publish_data.<new physical
  table>`. Postgres preserves a view's OID across `CREATE OR REPLACE
  VIEW` as long as the output column names, order, and types stay
  compatible (columns may be added at the end) -- which is what
  actually solves the object-identity problem the rename mechanism
  had: anything referencing the view by OID keeps working across
  republication, because the view's identity never changes, only what
  it selects from does.
- Rollback repoints the view to the execution's own recorded
  `previous_physical_table` -- it does not drop the current physical
  version, recreate anything, or touch `aegis_publish_data` beyond
  that one read.
- The API accepts only a logical target name; `target_schema` is gone
  from the request entirely, since there is only ever one valid
  publication schema now.
- Explicitly out of scope, per the locked decision: foreign keys
  targeting the published view, writing through the view, replacing
  arbitrary tables in `public`, preserving arbitrary trigger/
  constraint sets on a "target" (there is no arbitrary target
  anymore), and mutating a source table in place.

### What this replaces

The OID-based "is this the same physical table" tracking from the
third/fourth correction passes existed because the rename mechanism
could hand the same NAME to different physical tables over time.
Immutable, uniquely-suffixed physical version tables that are never
renamed or reused make that whole class of problem structurally
unreachable -- there is no "was this table swapped under me" question
to ask anymore, only "does the marker/view agree on which physical
table is current," which is a much simpler thing to check.

The session-level advisory locking design from the fourth correction
pass (held by the caller for the whole execute-live/rollback flow,
not just the DDL transaction) carries forward unchanged -- locking
around "don't let two operations touch the same logical_target at
once" is orthogonal to how publication itself works underneath it.

## Implementation complete

The locked architecture above is now built, not just specified:

- `src/db/source_session.py` -- lazy SOURCE_DATABASE_URL engine,
  mirrors live_session.py's validation pattern exactly.
- `src/live_execution/source_connector.py` -- reads a complete source
  table under REPEATABLE READ + READ ONLY, ordered by primary key
  (required; a table without one is rejected). Primary key columns
  are looked up via information_schema.key_column_usage rather than
  decoding pg_index's indkey directly -- checked first and switched
  after not being able to fully confirm array_position() behaves
  correctly against int2vector; the ANSI-standard information_schema
  path is unambiguous. Reuses compute_dataframe_fingerprint for the
  dataset fingerprint -- the same function that already proves
  sandbox/live Surgeon-output consistency, now applied one layer
  earlier, to the input.
- `src/live_execution/writer.py` -- rewritten as
  PostgresPublicationWriter. publish() creates an immutable physical
  version table and repoints the stable view via CREATE OR REPLACE
  VIEW in one transaction; structural compatibility (same columns,
  same order, may append at the end) is checked before attempting the
  view swap. rollback_to_previous() repoints to the recorded previous
  version, or drops the view entirely if there wasn't one -- never
  drops or recreates a physical version table. Session-level locking
  (hold_target_lock, held by the caller for the whole flow) carries
  forward unchanged from the prior correction pass.
- `src/live_execution/safety.py` -- the schema-allowlist gate is gone
  (nothing left to allowlist, schemas are fixed); live_eligible is
  the new gate that actually enforces sample_data tickets can never
  execute live.
- `src/api/app.py` -- new POST /simulate-migration-from-source
  (always routes to REQUIRES_HUMAN_APPROVAL, even for a
  high-confidence repair, since AUTO_APPROVE never creates a ticket
  and execute-live needs one to reference). execute-live re-reads the
  source and requires the same dataset fingerprint as what was
  approved before ever running Surgeon; a mismatch blocks execution
  (SourceChangedError, 409) rather than publishing against stale
  provenance. The Surgeon recomputation itself now runs against that
  freshly-verified read, not the ticket's stored snapshot, so
  "verified unchanged" and "what actually gets corrected" are
  provably the same data.
- Migration 0003 (amended, not a new migration -- not yet applied
  live) creates the aegis_publish / aegis_publish_data schemas and
  all the new columns.

Test count: 58 pure-Python (run and passing) + 27 in
test_live_execution_api.py + 17 in test_api.py + 27 in
test_schema_registry_api.py = 129 total, 71 needing three disposable
Postgres databases now (governance, live target, trusted source --
docker/init-test-db.sql updated to create all three automatically on
first container init). None of the Postgres-dependent pieces have run
against a real database in this environment -- everything here is
written, syntax-checked, and reasoned through as carefully as the
rest of this project, but unexecuted until run for real.

## Fifth correction pass

A further review found genuine implementation-level gaps in the final
architecture. Fixed:

1. **Source snapshot wasn't atomic** -- table existence, primary-key
   lookup, column metadata, and the complete ordered read now all
   happen inside one connection and one REPEATABLE READ transaction
   in source_connector.py. Previously these were three separate
   connections; a concurrent schema/constraint change between them
   could have produced a ticket whose primary key, row data, and
   schema fingerprint came from three different states of the table.
2. **Primary-key lookup could mix columns from another table** -- the
   information_schema join only matched on constraint_name/
   constraint_schema, not table_name. Two different tables in the
   same schema sharing an identically-named PRIMARY KEY constraint
   could have had their key_column_usage rows cross-matched. Fixed by
   joining on table_schema/table_name too; added a test with two
   tables sharing a constraint name to prove it.
3. **Ambiguous commit handling could still mark a committed
   publication as FAILED** -- a bare "marker absent" check doesn't
   prove a transaction has finished failing (the commit acknowledgment
   could simply be delayed). Removed FAILED as a possible synchronous
   conclusion for a generic exception entirely -- only "marker found"
   (COMPLETED) is decided synchronously now; everything else is left
   RUNNING/ROLLING_BACK for the lock-gated reconciliation path, which
   additionally proves the session lock is free before it will ever
   conclude failure.
4. **Financial datatype fidelity was unsafe** -- confirmed the writer
   had no explicit support for NUMERIC, DATE, TIMESTAMPTZ, UUID, or
   JSONB at all, mapping everything object-dtype to TEXT regardless of
   actual content. Source reads now build the DataFrame from raw
   fetched rows with dtype=object forced throughout (verified directly
   against real pandas: Decimal, date, timezone-aware datetime, UUID,
   and dict all survive completely untouched -- pd.read_sql's own
   inference was never trusted to guarantee this). The writer now
   infers each column's Postgres type from its actual values rather
   than a meaningless dtype label, with explicit handling for Decimal
   (unconstrained NUMERIC, preserving arbitrary precision exactly),
   date, timezone-aware/naive timestamps, UUID, and JSONB. Order
   matters in that inference (bool before int, since bool is an int
   subclass in Python; datetime.datetime before datetime.date, since
   datetime IS a date subclass) and is commented accordingly. Could
   not verify SQLAlchemy's exact type-class hierarchy in this
   environment (no working install to introspect) -- used exact
   type() equality rather than isinstance() specifically to avoid
   depending on an inheritance assumption that couldn't be confirmed.
5. **Appended columns broke rollback** -- the earlier design allowed
   publishing with extra columns appended at the end (Postgres's
   CREATE OR REPLACE VIEW permits this going forward), but rollback
   repoints to the PREVIOUS, narrower physical table, and Postgres
   will not allow CREATE OR REPLACE VIEW to remove existing output
   columns. That meant publish-then-rollback with an appended column
   would fail specifically on the rollback. Phase 2.5 now requires an
   EXACT publication signature (same names, order, types, count) in
   both directions -- schema evolution belongs in a later, dedicated
   publication-versioning phase.
6. **View compatibility checks ignored types** -- folded into the
   same exact-signature check as #5; comparing names alone let a
   type-changed-but-same-named column reach Postgres and fail with a
   raw database error instead of a controlled 422.
7. **A test could pass without testing anything** -- the incompatible-
   schema test's source/Gold pair matched exactly, so no repair plan
   was ever proposed, and an early `if "ticket_id" not in submit:
   return` let it report passed regardless. Rewrote it with a
   guaranteed repair scenario (the same proven rename pattern used
   throughout this file) and an explicit assertion that fails loudly
   if no ticket is produced. Added a companion test proving appended
   columns are refused, not silently accepted.
9. **Migration 0003 created schemas in the wrong database** -- it runs
   against DATABASE_URL (governance), but was creating aegis_publish/
   aegis_publish_data there via CREATE SCHEMA, even though publication
   happens in LIVE_DATABASE_URL entirely separately. The writer's own
   _ensure_publish_schemas_and_log() already creates both correctly,
   idempotently, in the right database, the first time anything is
   published -- the migration's version was not just wrong but
   entirely redundant. Removed from both upgrade() and downgrade()
   (the downgrade's DROP SCHEMA CASCADE was run against the
   governance database too, which is now correctly gone as well).

Item 8 (docs/env) partially addressed: .env.example now includes
SOURCE_DATABASE_URL/SOURCE_TEST_DATABASE_URL and
AEGIS_LIVE_EXECUTION_STALE_SECONDS, and no longer references the
retired AEGIS_LIVE_TARGET_SCHEMA_ALLOWLIST. A full reorganization of
this document (active architecture first, history in an appendix) was
not done -- restructuring ~30KB of accumulated correction-pass history
under time pressure risked losing or garbling content by transcription
error more than a reader scrolling past some history costs. A "read
this first" pointer was added at the top instead, naming this
explicitly as an interim fix, not a complete one.

**Additional design concern, acknowledged but not resolved:** the
complete source DataFrame is still persisted as JSONB on
approval_tickets.target_dataset. For a genuinely large source table
this duplicates significant data into the governance database and
makes ticket storage/retrieval more expensive than it needs to be.
This does not create an unsafe publication by itself, but it needs
either a documented size boundary or a dedicated immutable-snapshot
storage strategy (separate from the governance database) before this
system takes real production traffic against large source tables.
Flagging this rather than guessing at a boundary number or design
without your input on what "large" means for actual expected usage.

## Sixth correction pass

A further review found a genuine showstopper plus five more real
implementation gaps. Fixed:

1. **SHOWSTOPPER: trusted-source simulation could never detect a
   repair.** Confirmed directly against the actual Consultant/Inspector
   code: forcing dtype=object on every source column (the prior pass's
   fix for Decimal/date/UUID precision) made Consultant.propose_repairs()'s
   rename detection -- which requires observed_type == gold_type exactly
   -- fail for every genuinely-typed source column, since "object" can
   never equal "int64". Verified this returned zero repair plans for
   the exact rename scenario this whole system exists to detect, before
   fixing it. Fixed with a separate logical-dtype vocabulary
   (logical_dtype.py) that recovers the correct dtype from captured
   PostgreSQL column metadata instead of the (now precision-protected
   but schema-meaningless) pandas dtype label. Caught and fixed two
   self-inflicted regressions while implementing this fix itself: the
   first version made safety.py transitively require sqlalchemy,
   breaking the entire pure-Python test suite (fixed by extracting the
   vocabulary into its own dependency-free module, confirmed importable
   without a database again); a subsequent edit accidentally deleted
   the UnsupportedSourceTypeError class definition entirely (caught by
   re-reading the file rather than assuming, restored, reconfirmed).
2. **Provenance revalidation was incomplete** -- verify_source_unchanged()
   now compares all four persisted values (primary key, row count,
   schema fingerprint, dataset fingerprint), not just the dataset
   fingerprint. The schema fingerprint itself now includes ordinal
   position, numeric precision/scale, datetime precision, and
   nullability, not just column name + data_type.
3. **Surgeon's own validation only checks column order, not type** --
   added verify_complete_schema_match(), requiring a completely empty
   schema delta (no missing/new columns, no type mismatches, no
   reorder) before live publication. Catches the case where Consultant
   proposed repairs for two problems but RepairSelector only chose
   one, leaving a second column's type wrong while column order still
   matched. Uses the SAME logical-dtype vocabulary as the source read
   (build_corrected_observed_schema), not raw pandas dtype -- otherwise
   every exotic-typed column (decimal/date/uuid/json) would wrongly
   read as still-mismatched, since their pandas dtype is always
   "object" regardless of correctness.
4/5. **Publication typing was inferred from one row value, and
   inserted through to_sql() rather than the explicitly-typed table**
   -- confirmed both: an all-null NUMERIC/UUID/JSONB column had no
   value to infer from and fell back to TEXT, and to_sql() builds its
   own SQLAlchemy metadata from the DataFrame directly (ignoring the
   physical table's actual declared types), so the JSONB/UUID/Numeric
   bind processors were never actually exercised. Publication typing
   now comes directly from the Gold schema's declared logical dtype
   (guaranteed fully resolved by #3's check, not inferred from values
   at all), and the corrected dataset is inserted through the real,
   explicitly-typed Table object's own insert(), not DataFrame.to_sql().
6. **Advisory-lock liveness was tied to the wrong connection** --
   hold_target_lock() now yields its connection, and publish()/
   rollback_to_previous() require that SAME connection rather than
   opening their own. Reasoned through carefully before implementing:
   the original nested-scope structure (lock acquired before publish
   starts, released after it returns) already prevented the lock from
   releasing while a normal publish was still running, but a narrower
   real gap remained -- the two connections are independently drawn
   from the same pool, so something like an intermediate proxy's idle
   timeout could in principle affect one without affecting the other
   at the same moment, letting the lock release while the publish
   transaction was still genuinely active elsewhere. Sharing one
   connection removes that gap by construction.
7. **No database-level guarantee tied live_eligible to actual
   provenance** -- added a CHECK constraint on approval_tickets
   (NOT live_eligible OR all four provenance fields non-null), and
   made live_executions' own source_schema/source_table/
   source_dataset_fingerprint NOT NULL (every row is guaranteed to
   come from a live_eligible ticket by construction).

Also fixed a broken test: the primary-key constraint-collision test
used two DIFFERENT constraint names (shared_pk_name vs
shared_pk_name_b), never actually exercising the collision it claimed
to test. Confirmed PostgreSQL scopes constraint-name uniqueness
per-table, not per-schema, so two tables can legally share an
identical constraint name -- fixed the test to actually do that.

58/58 pure-Python tests pass throughout every step of this pass,
re-verified after each individual fix, not just at the end.

## Seventh correction pass

A further review found three more severe, crash-level bugs in the
trusted-source path, plus four more real gaps and one invalid test
this project had already gotten wrong once. Every claim was verified
directly (including the test-defect claim, confirmed via a real
PostgreSQL core-developer explanation of index-name namespacing)
before any code changed. Fixed:

1. **SEVERE: the Schema Registry rejected every one of Phase 2.5's own
   logical dtypes.** Confirmed directly: pandas.api.types.pandas_dtype()
   rejects "decimal", "date", "datetime", "datetime_tz", "uuid", and
   "json" outright -- meaning no Gold schema could ever be registered
   declaring a NUMERIC, DATE, TIMESTAMPTZ, UUID, or JSONB column,
   making several prior correction passes' datatype-fidelity work
   unusable in practice for exactly the types a financial system cares
   about most. Fixed with a new, phase-neutral aegis_dtypes.py module
   (deliberately NOT under live_execution/, so Phase 2.4's registry
   doesn't have to depend on a later phase's package) shared by the
   registry and the logical-dtype vocabulary.
2. **SEVERE: JSONB source columns crashed schema observation
   entirely.** Confirmed directly: pandas' own Series.nunique() raises
   TypeError: unhashable type: 'dict' on any dict/list value. Fixed
   with a JSON-safe unique-count helper that canonicalizes dict/list
   values to sorted JSON strings before counting.
3. **SEVERE: UUID source columns crashed ticket submission, and a
   reserved-key collision could silently corrupt legitimate JSONB
   data.** Confirmed both directly: _sanitize_scalar had no UUID
   handling at all (TypeError: Object of type UUID is not JSON
   serializable), and a legitimate JSONB value that happened to
   contain a key literally named "__aegis_type__" was indistinguishable
   from an internal type tag on restore, silently corrupting it into a
   Decimal. Fixed by adding UUID tagging AND wrapping every dict/list
   value in an explicit envelope unconditionally, not just ones that
   happen to collide -- this removes the ambiguity entirely rather
   than trying to detect collisions. This touched previously-verified
   Phase 2.3 serialization code; full regression re-run immediately
   after to confirm nothing else broke.
4. **All-null typed columns were wrongly rejected after repair.**
   Confirmed directly: an all-null column has no value for the
   value-based inference to work from, so it always fell back to
   "object", which then never matched whatever Gold actually declared.
   Fixed by making the post-repair check metadata-backed: an untouched
   or renamed-but-not-cast column's logical dtype is looked up from
   the ticket's own already-correctly-typed original schema instead of
   re-inferred from values that may not exist.
5. **Sandbox manifests could overstate success.** The schema-
   completeness gate was only checked at execute-live time, not at
   approval -- a live-eligible ticket with one of two needed repairs
   applied could still be approved with a manifest claiming full
   success and LOW_RISK. Added the same check to approve_ticket,
   scoped specifically to live-eligible tickets (sample_data tickets
   never carried this expectation, and changing their behavior wasn't
   what was being asked).
6. **Publication normalizes precision/scale rather than preserving
   exact source DDL.** Confirmed and made an explicit, stated policy
   rather than an implicit gap: Phase 2.5 uses canonical Aegis
   normalization (unconstrained NUMERIC, BIGINT for all integers,
   DOUBLE PRECISION for all floats, TEXT for all strings), which fully
   preserves exact VALUE fidelity (nothing is ever rounded or
   truncated) but does not preserve or enforce the source's
   schema-level precision/scale/length constraints on the published
   table. Documented directly in logical_dtype.py as a deliberate
   choice; exact schema-level fidelity would be a separate, larger
   piece of work.
7. **The CHECK constraint didn't cover all six provenance fields.**
   Extended to require source_row_count and source_schema_fingerprint
   too (both part of the four-value revalidation), plus a non-empty
   primary-key array, in both models.py and migration 0003.

Also fixed a test that was invalid for a reason this project had
already gotten wrong once before: the primary-key constraint-collision
test tried to create two PRIMARY KEY constraints sharing one name in
one schema. Confirmed via a real PostgreSQL core-developer explanation
that this is not legally constructible at all -- PRIMARY KEY/UNIQUE
constraints require a backing index, and index names are unique per
SCHEMA, not per table, so the second CREATE TABLE would fail outright
before the lookup logic under test was ever exercised. Redesigned
around a genuinely legal scenario instead: a FOREIGN KEY constraint
(which needs no backing index, and so isn't subject to that
restriction) sharing a name with a PRIMARY KEY on a different table --
this actually reproduces the class of collision the fix addresses.

58/58 pure-Python tests pass, reconfirmed after every individual fix.

## Eighth correction pass

A further review found four more real gaps in the dtype/repair
contract, all confirmed directly against actual Surgeon/pandas
behavior before any code changed. Fixed:

1. **Consultant could propose casts Surgeon cannot perform.**
   Confirmed directly: Surgeon's CAST_COLUMN applies
   working_df[column].astype(target_type) verbatim, and pandas has no
   understanding of Aegis's own extended logical dtypes ("decimal",
   "date", "datetime", "datetime_tz", "uuid", "json") -- every such
   cast fails for every row, reporting applied=False/HIGH_RISK, which
   can never become live-eligible regardless. Rather than implement
   unverified conversion logic for five exotic types with no way to
   test it against a real database, this is fixed conservatively:
   /simulate-migration-from-source now rejects a CAST_COLUMN targeting
   one of these dtypes before a ticket is ever created, with a clear
   explanation. This is a deliberate scope decision, not a permanent
   one -- renames to these types already work fine; only casting a
   mismatched source into them does not yet.
2. **A successful cast to "object" was wrongly rejected post-repair.**
   build_corrected_observed_schema() only ever accounted for
   RENAME_COLUMN; for CAST_COLUMN it fell through to "look up the
   original name's dtype," which is correct for untouched columns but
   wrong for one a cast had just deliberately retyped. Confirmed
   directly: a genuine int64-to-object cast was reconstructed as still
   being int64 and flagged as an unresolved mismatch. Fixed by parsing
   CAST_COLUMN the same way RENAME_COLUMN already was, and using the
   cast's own declared target as authoritative for that column.
3. **The registry accepted dtypes publication can't handle.** Since
   the Schema Registry now accepts anything
   pandas.api.types.pandas_dtype() recognizes, confirmed directly that
   "int32", "Int64", "float32", "string", "datetime64[ns]", and
   "category" all pass registry validation with no publication mapping
   at all -- such a schema could register, simulate, and get approved,
   only to fail at execute-live. Fixed by validating every Gold
   column's dtype is publishable at simulation time for the
   live-capable path specifically, rejecting before a ticket exists.
4. **live_executions didn't retain complete source provenance.** The
   ticket and manifest persist all six provenance fields
   (source_schema, source_table, source_primary_key, source_row_count,
   source_schema_fingerprint, source_dataset_fingerprint), but
   live_executions only copied three -- the other three were only
   recoverable via a join back to the ticket, when the design intent
   was for this record to stand alone as a complete, independent audit
   trail. Added the missing three (NOT NULL, same reasoning as the
   other three) to models.py, migration 0003, create_running(), and
   exposed them in the GET response.

58/58 pure-Python tests pass, reconfirmed after every individual fix.
Two new PostgreSQL-dependent tests added for the new rejection paths
(uncastable-target casts, unpublishable Gold dtypes).
