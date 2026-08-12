# Phase 3.1.4 — Governance and Approval Integration Report

**Status:** implementation candidate; owner verification and commit required.
**Parent commit:** `07a9632` — Phase 3.1.3 sandbox Surgeon integration.
**Branch:** `phase-3.1-verified-type-repair`.
**Authoritative contract:** `Docs/phase3_1_verified_type_repair_spec.md`.

## 1. Objective

Phase 3.1.4 connects the verified type-repair engine to Aegis governance,
approval, persistence, and API boundaries without enabling live casting.

The stage implements two required controls:

1. Persist and expose conversion decisions safely.
2. Enforce human approval together with conversion safety.

A human decision can authorize a verified `SAFE` sandbox conversion, but it
cannot convert `UNSAFE`, `UNSUPPORTED`, `ERROR`, destructive, malformed, or
stale conversion evidence into executable work.

## 2. Safety behavior

### 2.1 CAST_COLUMN preflight

`/simulate-migration` performs a complete verified conversion analysis before
creating an approval ticket.

For `CAST_COLUMN`:

- `SAFE` produces a pending human-approval ticket.
- `UNSAFE`, `UNSUPPORTED`, and `ERROR` produce
  `BLOCKED_BY_CONVERSION_SAFETY` and no ticket.
- `WITH_DROP_INVALID` is refused before analysis.
- malformed actions and missing source columns are refused.
- CAST_COLUMN is never auto-approved.

`RENAME_COLUMN` retains its existing governance behavior.

### 2.2 Persisted decision

A safe CAST_COLUMN ticket stores one immutable, redacted
`ConversionOutcomeMetadata` object in
`approval_tickets.conversion_decision`.

The stored object contains only:

- column name;
- outcome status;
- source and target dtype labels;
- policy version;
- aggregate counts;
- unique stable reason codes.

It excludes:

- raw source values;
- converted values;
- row indexes or labels;
- diagnostic messages;
- the converted pandas Series.

### 2.3 Approval enforcement

Conversion safety is enforced in three layers:

1. the in-memory `ApprovalQueue`;
2. `PostgresApprovalRepository.approve()`;
3. the API approval orchestrator.

Before a CAST_COLUMN ticket can become approved:

- a persisted decision must exist;
- its status must be `SAFE`;
- the persisted dataset snapshot is analysed again;
- the fresh redacted decision must exactly equal the persisted decision;
- the sandbox Surgeon execution must apply successfully;
- the execution outcome must exactly equal the approved decision.

Any mismatch rolls back the approval transaction and leaves the ticket
`PENDING`.

### 2.4 Manifest persistence

The sandbox execution outcome is stored in
`healing_manifests.conversion_outcome` using the same reviewed redacted JSON
shape as the approval decision.

The corrected DataFrame remains ephemeral and is not stored in this field.

### 2.5 Live execution boundary

Live CAST_COLUMN remains blocked.

Defence-in-depth now rejects a live request when either:

- the repair plan is CAST_COLUMN; or
- the ticket contains a conversion decision.

A sandbox manifest containing a conversion outcome is also rejected by the
live path. Controlled live enablement remains Phase 3.1.6 work and must use an
explicit allowlist.

## 3. Database schema

Alembic revision `0004_conversion_decisions.py` adds:

- `approval_tickets.conversion_decision JSONB NULL`;
- `healing_manifests.conversion_outcome JSONB NULL`.

Both fields are nullable for RENAME_COLUMN and historical records.

Database check constraints require each populated field to be a JSON object.
The immutable Python model validates the exact allowed field set and value
invariants.

No migration is applied or PostgreSQL result claimed in this stage. Real
disposable-database migration and integration verification belongs to Phase
3.1.5.

## 4. API exposure

Redacted conversion metadata is exposed through:

- `/simulate-migration` safe pending responses;
- `/simulate-migration` blocked conversion responses;
- `GET /approvals`;
- `GET /approvals/{ticket_id}`;
- `POST /approvals/{ticket_id}/approve`.

No endpoint exposes raw values, row labels, converted series, or diagnostic
messages through the conversion-decision object.

## 5. Compatibility

The stage preserves:

- existing RENAME_COLUMN execution behavior;
- sandbox-only automatic approval for eligible non-cast plans;
- approval transaction rollback behavior;
- source provenance and schema lineage;
- immutable publication and rollback architecture;
- the Phase 3.1 authoritative specification file unchanged;
- the Phase 3.1.3 verified conversion engine and Surgeon behavior.

Surgeon component version advances from `1.8` to `1.9` because it now consumes
the shared governance CAST parser and redacted metadata builder.

## 6. Files in scope

1. `alembic/versions/0004_conversion_decisions.py`
2. `Docs/phase3_1_step4_governance_approval_report.md`
3. `src/api/app.py`
4. `src/db/models.py`
5. `src/governance/__init__.py`
6. `src/governance/approval.py`
7. `src/governance/approval_repository.py`
8. `src/governance/conversion_safety.py`
9. `src/governance/manifest.py`
10. `src/governance/manifest_repository.py`
11. `src/surgeon/surgeon.py`
12. `tests/test_api.py`
13. `tests/test_conversion_governance.py`
14. `tests/test_conversion_governance_api_logic.py`
15. `tests/test_surgeon.py`

No other project file is part of Phase 3.1.4.

## 7. Local verification evidence

The delivery environment produced:

- Phase 3.1.4 governance tests: `40 passed`;
- Phase 3.1.2–3.1.4 targeted tests: `217 passed`;
- complete available pure suite: `285 passed, 3 skipped`;
- compilation: exit code `0`;
- authoritative Phase 3.1 specification SHA-256 unchanged:
  `FA8B4037D4C0B8FD5395E8CE5C6543FE57441AC7D05F7ECEC050F2CB558120EC`.

The three skipped modules require the disposable PostgreSQL databases and are
not claimed as passing in this stage.

## 8. Phase boundary

Phase 3.1.4 is ready for owner apply and local verification after the parent
commit and clean-repository gates pass.

Phase 3.1.5 remains responsible for:

- applying Alembic revision `0004` to disposable PostgreSQL;
- exercising real ticket and manifest JSONB persistence;
- running API, schema registry, live-target, and source-database suites;
- correcting any stateful migration or database behavior discovered there.

Live CAST_COLUMN remains blocked until Phase 3.1.6.
