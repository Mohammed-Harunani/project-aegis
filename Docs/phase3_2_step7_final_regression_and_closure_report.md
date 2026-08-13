# Project Aegis — Phase 3.2.7 Final Regression and Closure Report

## Status

Phase 3.2.7 is the final verification and closure stage for Phase 3.2 —
Source Ingestion Identity.

This report consolidates the implementation milestones, dedicated PostgreSQL
acceptance evidence, final full-regression evidence, compatibility review,
repository-integrity checks, and the resulting Phase 3.2 closure decision.

**Final verification result: PASSED**

**Phase 3.2 closure status: READY TO CLOSE after this documentation commit is
committed, pushed, and locally/remotely verified**

**Controlled merge status: READY FOR SEPARATE MERGE PROCEDURE; no merge is
performed by this documentation step**

## 1. Verified branch and baseline

- Branch: `phase-3.2-source-ingestion-identity`
- Authoritative base and unchanged `origin/main`:
  `c04f46a4dec50ea6ee5d297c2dfc05ae45afb04b`
- Implementation and PostgreSQL-report tip verified by final regression:
  `ff847b7ae000f4009c7e4a95f25b6f8ba1b6ad2e`
- Local and remote phase tips matched exactly before final regression.
- Local and remote phase tips still matched after final regression.
- `origin/main` remained at the authoritative Phase 3.2 base.
- `origin/main` was verified as an ancestor of the phase tip.
- The phase history contained six linear commits before this closure report.
- The repository was clean before and after final regression.

## 2. Phase 3.2 milestone chain

| Stage | Result | Commit |
|---|---|---|
| Phase 3.2.1 — Architecture and safety contract | Closed | `06f873f` |
| Phase 3.2.2 — Source and dataset identity model | Closed | `a8fc814` |
| Phase 3.2.3 — Immutable snapshots and ingestion runs | Closed | `e292142` |
| Phase 3.2.4 — Drift and provenance enforcement | Closed | `1264374` |
| Phase 3.2.5 — Publication identity integration | Closed | `8d4d19b` |
| Phase 3.2.6 — Dedicated PostgreSQL verification | Closed | `ff847b7` |
| Phase 3.2.7 — Final regression and closure | Final verification passed | Documentation commit pending at report creation |

The milestone chain is linear from `c04f46a` through `ff847b7`. No Phase 3.2
implementation commit is based on an unmerged side branch.

## 3. Consolidated implementation result

Phase 3.2 established durable identity and observation lineage without
changing the trusted-source or stable-view publication architecture.

### 3.1 Identity authority

- Source and publication system keys are server-controlled configuration.
- Database endpoint identity is represented by a credential-free binding
  fingerprint.
- Stable keys cannot silently rebind to a different endpoint.
- A single endpoint cannot be registered under competing stable keys.
- Source and publication bindings cannot resolve to the same database.
- Source datasets are identified by source system, schema, and table.
- Publication targets are identified by publication system and logical
  target.
- No public create, update, delete, or rebind API was introduced for these
  identities.

### 3.2 Immutable observations

- Complete source reads produce immutable dataset snapshots.
- Snapshot identity is scoped to the registered source dataset.
- Repeated identical observations create distinct runs while safely reusing
  the same snapshot.
- Identical content from different sources remains identity-separated.
- Snapshot serialization retains the verified dataset codec behavior for
  Decimal, UUID, JSONB, date, datetime, infinity, indexes, order, and object
  dtype.
- Terminal ingestion runs record simulation, live revalidation, drift, and
  redacted failure outcomes.

### 3.3 Approval and manifest lineage

- New source-backed tickets link to their exact simulation ingestion run.
- New ticket replay resolves through the linked immutable snapshot.
- Legacy embedded ticket payloads remain replayable.
- Approval and sandbox manifest persistence remain atomic.
- Manifests reference the same simulation lineage as their tickets.
- Existing conversion-governance and corrected-output fingerprint evidence
  remains authoritative.

### 3.4 Live drift enforcement

- Live execution verifies the configured source still matches the approved
  registered source system.
- The source is reread completely before publication.
- The fresh DataFrame used for equality evidence is the same DataFrame passed
  to Surgeon.
- Matching observations persist `MATCHED` and may continue.
- Changed observations persist `DRIFTED`, mark the attempt failed, and block
  target mutation.
- Source-read failures persist only stable, redacted diagnostics when a safe
  terminal run can be written.

### 3.5 Publication and recovery identity

- New live executions reference a registered publication target.
- Publication-system resolution participates in the governance transaction
  that creates the running live execution.
- `live_execution_id` remains the cross-database recovery key.
- `logical_target` remains the advisory-lock key and stable-view name.
- Target-side PUBLISH and ROLLBACK markers include the publication-target ID.
- Existing marker tables are upgraded additively with
  `ADD COLUMN IF NOT EXISTS`.
- Historical marker rows with null publication identity remain readable.
- Marker identity disagreement cannot prove completion during ambiguous
  outcome handling or reconciliation.
- Rollback requires both governance-side latest-execution proof and
  target-side marker proof, including publication-target equality for new
  lineage.
- Stable views and immutable physical publication tables retain their prior
  semantics.

## 4. Migration and historical compatibility

Alembic migration `0005_source_ingestion_identity.py` remains the single
linear head over `0004`.

Verified migration behavior:

1. Upgrade creates the Phase 3.2 identity, snapshot, run, and publication
   structures.
2. Existing tables receive nullable lineage foreign keys so historical rows
   are not assigned invented identity.
3. New live-eligible ticket writes are constrained to carry valid simulation
   lineage.
4. Existing target dataset storage becomes nullable for linked-snapshot
   replay.
5. Downgrade materializes the linked snapshot payload before removing Phase
   3.2 lineage.
6. Representative legacy and Phase 3.2-created data survives the verified
   upgrade/downgrade contract.
7. ORM metadata and migration schema expectations remain aligned.

Historical compatibility result: **PASSED**.

## 5. Phase 3.2.5 owner verification evidence

Before the publication-identity implementation was committed, the owner ran:

- deterministic and database-independent gate: **90 passed, 1 warning**;
- targeted identity and live-publication PostgreSQL gate:
  **61 passed, 3 warnings**;
- complete PostgreSQL-enabled repository suite:
  **483 passed, 9 warnings**;
- source, test, and migration compilation: **PASSED**;
- whitespace verification: **PASSED**.

The verified implementation was committed as:

```text
8d4d19b81d54984b892b3805595548beda647e00
feat: bind live publication to registered targets
```

The commit was pushed and the local and remote branch tips were verified
identical.

## 6. Phase 3.2.6 committed-code PostgreSQL evidence

Dedicated verification ran against committed and remotely published
`8d4d19b81d54984b892b3805595548beda647e00`.

| Gate | Scope | Result |
|---|---|---:|
| Governance database | Migration, identity, snapshot, and ingestion repositories | 30 passed |
| Three-database workflow | Source, governance, publication, recovery, and rollback | 49 passed |
| **Total** | **Dedicated PostgreSQL verification** | **79 passed** |

The dedicated environment used the exact disposable databases:

- `aegis_test` for governance;
- `aegis_source_test` for trusted source reads;
- `aegis_live_test` for publication.

The PostgreSQL container was isolated inside the Compose network without a
host port. It was stopped and removed after verification; the persistent test
volume was retained.

Full evidence is recorded in
`Docs/phase3_2_step6_postgresql_verification_report.md`.

Phase 3.2.6 result: **PASSED**.

## 7. Phase 3.2.7 final regression evidence

The final regression ran on 2026-08-13 against committed and remotely
published `ff847b7ae000f4009c7e4a95f25b6f8ba1b6ad2e`.

The API image was freshly rebuilt from the clean phase branch. The test
process received only the verified disposable database URLs and test system
keys. Compilation ran before the complete suite.

Results:

- source, test, and Alembic compilation: **PASSED**;
- complete PostgreSQL-enabled repository suite:
  **483 passed, 9 warnings in 41.30s**;
- suite exit code: **0**;
- failures: **0**;
- errors: **0**;
- unexpected skips or xfails: **0**;
- repository after regression: **CLEAN**;
- local and remote phase tips: **IDENTICAL**;
- remote main: **UNCHANGED**;
- ancestry: **LINEAR**.

Phase 3.2.7 final regression result: **PASSED**.

## 8. Security, privacy, and API compatibility review

The implemented contract and verification suite establish that:

- connection URLs, usernames, passwords, and SSL material are not persisted
  in identity ORM records;
- only stable keys and credential-free endpoint-binding fingerprints are
  retained;
- source-read and ingestion failures use redacted public diagnostics;
- snapshot row payloads remain internal governance data and are not exposed
  through Phase 3.2 inspection endpoints;
- request models do not accept database URLs or system keys;
- no public identity mutation or rebind endpoint exists;
- existing simulation, approval, live execution, reconciliation, and rollback
  request contracts remain compatible;
- response lineage is additive;
- legacy ticket replay and historical null lineage remain supported;
- the existing conversion evidence, safety gates, target locking, immutable
  versions, and stable-view publication model remain enforced.

Security and compatibility review result: **PASSED**.

## 9. Warning disposition

The final suite emitted nine known non-blocking warnings in two categories:

1. Starlette's current TestClient/httpx compatibility path is deprecated in
   favor of `httpx2`.
2. Alembic is using legacy `prepend_sys_path` splitting because
   `path_separator` is not configured.

Neither warning changed schema state, persisted lineage, source validation,
target publication, recovery, rollback, or test results. They remain
dependency-maintenance items and are not Phase 3.2 safety blockers.

## 10. Closure-criteria determination

| # | Closure criterion | Result |
|---:|---|---|
| 1 | Authoritative Phase 3.2 specification committed | PASSED |
| 2 | Migration `0005` upgrades and downgrades successfully | PASSED |
| 3 | Credentials and connection URLs are not persisted or exposed | PASSED |
| 4 | Source-system rebinding fails closed | PASSED |
| 5 | Equal source and publication bindings fail closed | PASSED |
| 6 | Source datasets have stable identities | PASSED |
| 7 | Repeated reads create distinct runs and deduplicated snapshots | PASSED |
| 8 | Cross-source content remains identity-separated | PASSED |
| 9 | New tickets and manifests link to the exact simulation run | PASSED |
| 10 | Legacy tickets and codec payloads remain replayable | PASSED |
| 11 | Live revalidation records `MATCHED`, `DRIFTED`, or redacted `FAILED` | PASSED |
| 12 | Drift blocks publication before target mutation | PASSED |
| 13 | Live executions link simulation, revalidation, and publication identity | PASSED |
| 14 | Target-side markers retain crash-recovery authority | PASSED |
| 15 | Stable-view publication and rollback semantics are preserved | PASSED |
| 16 | Targeted PostgreSQL tests pass | PASSED |
| 17 | Complete existing and Phase 3.2 suite passes | PASSED |
| 18 | Compilation passes | PASSED |
| 19 | Temporary test infrastructure is removed | PASSED |
| 20 | Clean closure commit is locally and remotely verified before merge | PENDING THIS REPORT COMMIT AND PUSH |

Nineteen closure criteria are fully satisfied. The twentieth is the
repository-management verification that follows creation of this report.

## 11. Final closure determination

The implementation and verification gates required for Phase 3.2 have been
completed:

- the safety architecture is authoritative;
- source, dataset, snapshot, run, publication system, and target identity are
  implemented;
- live source drift is audited and enforced;
- approval, manifest, live execution, marker, reconciliation, and rollback
  lineage are connected;
- historical replay remains intact;
- migration round-trips have been verified against PostgreSQL;
- dedicated committed-code PostgreSQL acceptance passed;
- complete final regression passed against a freshly built image;
- warnings were reviewed and classified as non-blocking;
- temporary verification infrastructure was removed;
- `origin/main` remained unchanged;
- the phase branch remained clean, synchronized, and linearly based on main.

On the evidence recorded above, **Phase 3.2.7 final regression is PASSED**.

After this report is committed, pushed, and locally/remotely verified,
**Phase 3.2 — Source Ingestion Identity is CLOSED and ready for a controlled,
explicit merge into `main`**.

Merge to `main` remains a separate repository-management action. It must
create an explicit merge commit, verify both merge parents, prove that the
merged tree equals the verified Phase 3.2 tree, and avoid pushing until the
owner reviews the local merge evidence.
