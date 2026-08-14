# Phase 3.3.6 PostgreSQL Verification Report

**Status:** VERIFIED — Phase 3.3.6 PASSED

**Phase branch:** `phase-3.3-verified-column-order-repair`

**Verified commit:** `d5cb3e11407a5088983f812b8c8845cd295accd0`

**Authoritative Phase 3.3 base:** `fe15b90595bd81e724f4912367c2b17b1d6eae05`

**Verification date:** 2026-08-14

**Current Alembic head:** `0005`

## 1. Purpose

This report records the dedicated PostgreSQL acceptance verification for
Phase 3.3 Verified Column Order Repair. Verification ran against the clean,
published Phase 3.3.5 commit rather than an uncommitted working tree.

The acceptance scope covered:

- canonical and strictly parsed `REORDER_COLUMNS` actions;
- independent reorder-only eligibility proof;
- deterministic Consultant planning and non-destructive selection;
- sandbox and controlled live Surgeon execution;
- row, index, value, null, dtype, and source-isolation preservation;
- approval-time and execution-time column-order governance;
- immutable schema, snapshot, ingestion, source, and publication lineage;
- complete source reread and fresh logical-schema regeneration;
- fresh reorder proof against persisted Gold;
- exact approved-sandbox/live-output fingerprint equality;
- registered-target stable-view publication in exact Gold order;
- immutable physical versions, republish, rollback, and one-shot execution;
- migration-head and historical upgrade/downgrade compatibility.

## 2. Immutable verification preflight

Before PostgreSQL testing:

- local branch was `phase-3.3-verified-column-order-repair`;
- local HEAD was
  `d5cb3e11407a5088983f812b8c8845cd295accd0`;
- remote branch HEAD was
  `d5cb3e11407a5088983f812b8c8845cd295accd0`;
- `origin/main` remained
  `fe15b90595bd81e724f4912367c2b17b1d6eae05`;
- local and remote phase commit identities matched exactly;
- the phase branch contained five linear commits above `origin/main`;
- the repository was clean;
- no verification-time code or configuration edits were present.

Preflight result: **PASSED**.

## 3. Verification environment

Verification used Docker Compose with:

- PostgreSQL 16;
- the repository API image based on Python 3.11 slim;
- the dedicated disposable governance database `aegis_test`;
- the dedicated disposable source database `aegis_source_test`;
- the dedicated disposable publication database `aegis_live_test`;
- server-controlled source and publication test system keys;
- an isolated Compose-network PostgreSQL alias;
- no PostgreSQL host-port publication;
- compilation of `src`, `tests`, and `alembic/versions` before acceptance.

The three database roles remained distinct throughout verification. No test
URL, username, password, or other credential is recorded in this report.

## 4. Reproducible command contract

Each test gate ran in a disposable API container using the same environment
preparation:

```sh
python -m pip install \
  --disable-pip-version-check \
  --root-user-action=ignore \
  -q -r requirements-dev.txt

TEST_DATABASE_URL=$(printf "%s" "$TEST_DATABASE_URL" |
  sed -e "s/@localhost:/@postgres:/" -e "s/@127.0.0.1:/@postgres:/")
LIVE_TEST_DATABASE_URL=$(printf "%s" "$LIVE_TEST_DATABASE_URL" |
  sed -e "s/@localhost:/@postgres:/" -e "s/@127.0.0.1:/@postgres:/")
SOURCE_TEST_DATABASE_URL=$(printf "%s" "$SOURCE_TEST_DATABASE_URL" |
  sed -e "s/@localhost:/@postgres:/" -e "s/@127.0.0.1:/@postgres:/")

export TEST_DATABASE_URL LIVE_TEST_DATABASE_URL SOURCE_TEST_DATABASE_URL
export AEGIS_SOURCE_TEST_SYSTEM_KEY=aegis-source-test
export AEGIS_PUBLICATION_TEST_SYSTEM_KEY=aegis-publication-test
```

The compilation gate executed:

```sh
python -m compileall -q src tests alembic/versions
```

Compilation result: **PASSED**.

## 5. Gate A — focused policy and execution

The first committed-code gate executed:

```sh
python -m pytest -q \
  tests/test_column_order_governance.py \
  tests/test_column_order_policy.py \
  tests/test_column_order_surgeon.py \
  tests/test_surgeon.py \
  tests/test_consultant.py \
  tests/test_selector.py
```

Result:

```text
143 passed in 3.94s
```

Verified behavior included:

1. Canonical serialization and strict JSON parsing remain deterministic.
2. Malformed, duplicate, stale, and non-canonical order actions fail closed.
3. Only reorder-only drift produces a column-order repair plan.
4. Missing, new, renamed, or type-mismatched columns cannot enter the order
   path.
5. Surgeon uses the exact Gold order and preserves source isolation, rows,
   index, values, nulls, and dtypes.
6. Generic live execution remains blocked unless the caller supplies the
   exact Phase 3.3.5 one-call capability.
7. The live capability requires an exact boolean and is invalid outside live
   execution mode.
8. Reorder manifests remain low-risk, volume-preserving, and free of
   conversion metadata.

Gate A result: **PASSED**.

## 6. Gate B — governance, identity, and repository persistence

The second committed-code gate executed:

```sh
python -m pytest -q \
  tests/test_approval.py \
  tests/test_conversion_governance.py \
  tests/test_conversion_governance_api_logic.py \
  tests/test_dataset_codec.py \
  tests/test_identity_repository.py \
  tests/test_ingestion_provenance.py \
  tests/test_ingestion_repository.py \
  tests/test_jsonb_null_persistence_contract.py \
  tests/test_publication_identity_logic.py
```

Result:

```text
108 passed in 9.74s
```

Verified behavior included:

1. Approval state transitions and human decision identity remain enforced.
2. Reorder records cannot carry cast-conversion decisions or outcomes.
3. Dataset replay serialization preserves ordered columns and extended values.
4. Source and publication identities retain stable binding continuity.
5. Snapshot provenance fingerprints remain deterministic and fail closed on
   invalid evidence.
6. Ingestion runs preserve valid purpose, outcome, snapshot, and baseline
   relationships.
7. JSONB absence remains SQL `NULL`, not JSON `null`.
8. Registered publication targets remain bound to their publication systems.

Gate B result: **PASSED**.

## 7. Gate C — source and publication integration

The third committed-code gate executed:

```sh
python -m pytest -q tests/test_live_execution_api.py
```

Result:

```text
60 passed, 3 warnings in 37.53s
```

Verified behavior included:

1. Source-backed reorder submission retains immutable registry, source,
   snapshot, and ingestion lineage.
2. Approval persists matching redacted order evidence and a successful sandbox
   output fingerprint.
3. Live execution rereads the complete source and records a matched
   revalidation ingestion run.
4. The live path rebuilds the observed logical schema from fresh PostgreSQL
   metadata and independently re-proves reorder-only eligibility.
5. Surgeon recomputes the corrected dataset from the fresh read rather than
   publishing approval replay.
6. Fresh corrected output must exactly match the approved sandbox fingerprint
   before publication begins.
7. The stable view and physical version expose exact Gold order and PostgreSQL
   types while preserving values.
8. Source order remains unchanged by publication.
9. Source drift after approval blocks publication and persists `DRIFTED`
   revalidation evidence.
10. Fresh-proof failure and fingerprint mismatch fail before target mutation.
11. Repeated execution of one ticket is rejected.
12. Republish creates a new immutable physical version.
13. Rollback restores the preceding version and its exact order.
14. First-publish rollback removes only the stable view and retains the
    physical version.
15. Registered publication-target identity, marker, reconciliation,
    concurrency, and stale-rollback protections remain effective.
16. Existing rename and controlled cast live behavior remains compatible.

Gate C result: **PASSED**.

## 8. Gate D — migration head and history

The fourth committed-code gate executed:

```sh
python -m pytest -q \
  tests/test_identity_models.py::test_alembic_head_is_0005_with_linear_parent_0004 \
  tests/test_api.py::test_alembic_upgrade_and_downgrade \
  tests/test_identity_migration.py::test_0005_upgrade_preserves_history_and_downgrade_restores_replay_payload \
  tests/test_live_execution_api.py::test_migration_0003_creates_expected_tables_and_columns
```

Result:

```text
4 passed, 9 warnings in 10.83s
```

Verified behavior included:

1. Alembic retains exactly one head: `0005`.
2. `0005` remains linearly based on `0004`.
3. Full upgrade and downgrade history remains valid.
4. `0005` downgrade restores historical replay payloads correctly.
5. Earlier live-execution migration contracts remain readable and valid.
6. Phase 3.3 requires no new database migration.

Gate D result: **PASSED**.

## 9. Consolidated Phase 3.3.6 result

Dedicated committed-code verification executed 315 test invocations:

| Gate | Test executions | Result |
|---|---:|---|
| Focused policy and execution | 143 | PASSED |
| Governance, identity, and repository persistence | 108 | PASSED |
| Source and publication integration | 60 | PASSED |
| Migration head and history | 4 | PASSED |
| **Total** | **315** | **PASSED** |

The migration gate deliberately repeated one migration test from the complete
source/publication module so migration acceptance was visible as its own
explicit gate. The total above therefore records test executions, not a claim
of 315 unique test nodes.

There were no failures, errors, unexpected skips, or xfails.

The immediately preceding Phase 3.3.5 full regression also passed on the same
implementation tree:

```text
609 passed, 9 warnings in 53.55s
```

## 10. Warning assessment

Only previously known non-blocking warnings appeared:

- Starlette warned that the current TestClient/httpx compatibility path is
  deprecated in favor of `httpx2`.
- Alembic warned that `path_separator` is absent and legacy path splitting is
  being used.

Neither warning changed test behavior, persistence, source observation,
fingerprints, target mutation, migration results, or acceptance evidence.
They remain dependency-maintenance items rather than Phase 3.3 blockers.

## 11. Evidence correction record

The first attempted matrix wrapper used a multiline native-command argument.
It produced no pytest output, so its fixed PowerShell summary was rejected and
was not used as acceptance evidence.

The corrected wrapper executed each Docker command separately and emitted the
complete pytest output recorded above. Its initial display label predicted 92
source/publication tests; pytest authoritatively collected and passed 60. This
report records the actual `60 passed` result. The label mismatch did not alter
test selection, execution, or exit-code enforcement.

## 12. Cleanup and repository integrity

After all gates:

- the temporary `aegis-phase33-step6-postgres` container was stopped and
  removed;
- the retained PostgreSQL volume was not deleted;
- local HEAD remained
  `d5cb3e11407a5088983f812b8c8845cd295accd0`;
- the working tree remained clean;
- local and remote phase branch state remained synchronized;
- `origin/main` remained unchanged.

Cleanup result: **PASSED**.

## 13. Acceptance decision

Phase 3.3.6 PostgreSQL verification is accepted.

The published Phase 3.3 implementation has demonstrated its canonical action,
reorder-only planning, sandbox preservation, approval governance, immutable
lineage, fresh source revalidation, controlled live Surgeon execution, exact
fingerprint equality, registered-target publication, stable-view ordering,
immutable versioning, rollback, reconciliation, concurrency, and migration
compatibility contracts against the required dedicated PostgreSQL databases.

Phase 3.3 may advance to Phase 3.3.7 final regression, documentation closure,
and controlled merge preparation.
