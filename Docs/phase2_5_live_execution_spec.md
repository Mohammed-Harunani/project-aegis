# Aegis Phase 2.5 -- Live Execution Mode Spec

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
