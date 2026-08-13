# Phase 3.2.6 PostgreSQL Verification Report

**Status:** VERIFIED — Phase 3.2.6 PASSED

**Phase branch:** `phase-3.2-source-ingestion-identity`

**Verified commit:** `8d4d19b81d54984b892b3805595548beda647e00`

**Authoritative Phase 3.2 base:** `c04f46a4dec50ea6ee5d297c2dfc05ae45afb04b`

**Verification date:** 2026-08-12

**Current Alembic head:** `0005`

## 1. Purpose

This report records the dedicated PostgreSQL acceptance verification for
Phase 3.2 Source Ingestion Identity. Verification ran against the immutable,
published Phase 3.2.5 commit rather than an uncommitted working tree.

The acceptance scope covered:

- Alembic `0005` upgrade, downgrade, and historical replay preservation;
- immutable source-system, source-dataset, publication-system, and
  publication-target identity;
- snapshot deduplication, exact replay, and terminal ingestion runs;
- source binding continuity and complete live revalidation;
- drift persistence and publication blocking;
- live execution linkage to simulation, revalidation, and publication target;
- additive target-side marker evolution;
- target-bound PUBLISH and ROLLBACK evidence;
- ambiguous-outcome recovery and reconciliation;
- stable-view publication, immutable physical versions, and rollback safety;
- concurrency, supersession, and stale-rollback protection.

## 2. Immutable verification preflight

Before PostgreSQL testing:

- local branch was `phase-3.2-source-ingestion-identity`;
- local HEAD was
  `8d4d19b81d54984b892b3805595548beda647e00`;
- remote branch HEAD was
  `8d4d19b81d54984b892b3805595548beda647e00`;
- local and remote commit identities matched exactly;
- the repository was clean;
- no verification-time code or configuration edits were present.

Preflight result: **PASSED**.

## 3. Verification environment

Verification used Docker Compose with:

- PostgreSQL 16;
- the repository's API image based on Python 3.11 slim;
- the dedicated disposable governance database `aegis_test`;
- the dedicated disposable source database `aegis_source_test`;
- the dedicated disposable publication database `aegis_live_test`;
- server-controlled test system keys mapped to the runtime key names;
- an isolated Compose-network PostgreSQL alias;
- no PostgreSQL host-port publication.

The three database roles remained distinct throughout verification. No test
URL, username, password, or other credential was written into this report.

## 4. Gate A — governance persistence and migration

The first committed-code gate executed:

```text
tests/test_identity_migration.py
tests/test_identity_repository.py
tests/test_ingestion_repository.py
```

Result:

```text
30 passed, 3 warnings in 9.47s
```

Verified behavior included:

1. `0005` upgrades cleanly from the previous linear Alembic history.
2. `0005` downgrade restores replay payloads before removing Phase 3.2
   lineage.
3. Representative historical and Phase 3.2 rows survive the migration
   round-trip contract.
4. Stable keys and endpoint-binding fingerprints resolve durable identities.
5. Key rebinding, duplicate endpoint registration, and unsafe topology fail
   closed.
6. Publication identity resolution can participate in the live execution's
   governance transaction.
7. Dataset snapshots remain immutable and deduplicate only within the correct
   source-dataset scope.
8. Ingestion runs preserve valid purpose, outcome, snapshot, and baseline
   relationships.

Gate A result: **PASSED**.

## 5. Gate B — complete three-database live workflow

The second committed-code gate executed:

```text
tests/test_live_execution_api.py
```

Result:

```text
49 passed, 3 warnings in 49.25s
```

Verified behavior included:

1. The configured source binding must match the approved simulation lineage.
2. Equal source and publication bindings are rejected before live mutation.
3. Publication systems and logical targets resolve stable registered target
   identities.
4. New live execution rows persist a non-null `publication_target_id`.
5. Fresh source revalidation persists `MATCHED`, `DRIFTED`, or redacted
   `FAILED` evidence as appropriate.
6. Drift blocks publication while retaining the changed provenance evidence.
7. The target-side marker table gains `publication_target_id UUID` using an
   idempotent additive alteration.
8. Historical marker rows without publication identity remain readable.
9. New PUBLISH and ROLLBACK markers carry the exact live execution's
   publication-target UUID.
10. Marker identity mismatch cannot prove completion during ambiguous-outcome
    handling or reconciliation.
11. Rollback refuses a latest marker whose publication identity disagrees
    with the governance live execution.
12. Stable views, immutable physical tables, precision preservation,
    concurrent locking, supersession checks, and crash recovery retain their
    existing semantics.

Gate B result: **PASSED**.

## 6. Consolidated Phase 3.2.6 result

Dedicated committed-code PostgreSQL verification executed **79 tests**:

| Gate | Tests | Result |
|---|---:|---|
| Governance migration, identity, and ingestion | 30 | PASSED |
| Three-database live workflow | 49 | PASSED |
| **Total** | **79** | **PASSED** |

There were no failures, errors, unexpected skips, or xfails.

## 7. Warning assessment

Only previously known non-blocking warnings appeared:

- Alembic warned that `path_separator` is absent and legacy path splitting is
  being used.
- Starlette warned that its current TestClient/httpx compatibility path is
  deprecated in favor of `httpx2`.

Neither warning changed test behavior, persistence results, target mutation,
or acceptance evidence. They are dependency-maintenance items and are not
Phase 3.2 safety blockers.

## 8. Cleanup and repository integrity

After both gates:

- the temporary verification PostgreSQL container was stopped and removed;
- the retained PostgreSQL volume was not deleted;
- local HEAD remained
  `8d4d19b81d54984b892b3805595548beda647e00`;
- the working tree remained clean;
- local and remote branch state remained synchronized.

Cleanup result: **PASSED**.

## 9. Acceptance decision

Phase 3.2.6 PostgreSQL verification is accepted.

The committed Phase 3.2 implementation has demonstrated its migration,
identity, ingestion, drift, publication, marker, reconciliation, and rollback
contracts against the required dedicated PostgreSQL databases. Phase 3.2 may
advance to Phase 3.2.7 final regression, documentation closure, and controlled
merge preparation.
