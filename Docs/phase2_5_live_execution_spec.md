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
The governance record is written in stages instead: `PENDING` before
the target transaction starts, `RUNNING` once it does, then
`COMPLETED` or `FAILED` based on the outcome. The target table itself
can never be left half-mutated; the governance record's job is to
accurately reflect what happened, not to be part of the same commit.

**2. "The target is not the original source table" (safety gate #11)
has no concrete data to check against yet.** Nothing in Aegis today
tracks a live source-table reference -- data arrives as JSON
`sample_data`, not a query against a live table via the read-only
connector. `execute-live` accepts an *optional* `source_schema` /
`source_table` pair purely for this comparison; if supplied, target
must differ; if omitted, the check is skipped rather than invented.
Flagging this rather than silently deciding it either way.

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
in sandbox at approval time (not persisting a second copy of the
corrected dataset anywhere) -- consistent with "every repair must be
deterministic."

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
