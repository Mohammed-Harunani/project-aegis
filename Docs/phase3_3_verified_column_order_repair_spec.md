# Project Aegis — Phase 3.3 Verified Column Order Repair Specification

**Status:** APPROVED ARCHITECTURE — implementation not yet started
**Phase branch:** `phase-3.3-verified-column-order-repair`
**Authoritative base:** `fe15b90595bd81e724f4912367c2b17b1d6eae05`
**Prepared:** 2026-08-13
**Approved:** 2026-08-13
**Current Alembic head:** `0005`

---

## 1. Authority and purpose

This document is the proposed authoritative architecture for Project Aegis
Phase 3.3. It defines the only column-order repair that Phase 3.3 may plan,
approve, execute, publish, reconcile, and roll back.

Phase 3.3 closes a specific capability gap that is already visible in the
verified codebase:

- `AegisInspector` detects `reorder_event`;
- `AegisConsultant` has no reorder proposal;
- `AegisSurgeon` supports only `RENAME_COLUMN` and verified `CAST_COLUMN`;
- consequently, an otherwise identical dataset whose columns are presented in
  the wrong order cannot be repaired.

The phase must make that repair deterministic and provable without expanding
into missing-column synthesis, destructive column removal, combined repairs,
or business-meaning inference.

This specification supersedes any earlier informal Phase 3.3 suggestion. It
does not supersede the closed contracts for Phases 2.1–2.5, 3.1, or 3.2.
Those contracts remain authoritative except where this document explicitly
adds a compatible reorder-only capability.

---

## 2. Verified phase-entry baseline

Phase 3.3 begins from merge commit:

```text
fe15b90595bd81e724f4912367c2b17b1d6eae05
```

That commit is the verified merge of Phase 3.2 into `main`.

At phase entry:

- local `main` and `origin/main` are identical;
- the repository is clean;
- Phase 3.2 is closed with 20/20 closure criteria passed;
- the full regression suite has 483 passing tests;
- Alembic has one linear head, `0005`;
- source systems, source datasets, snapshots, ingestion runs, publication
  systems, and publication targets have durable identity;
- source-backed approval and live execution carry immutable lineage;
- live execution revalidates the complete trusted source;
- publication uses immutable physical tables behind stable views;
- target-side markers, reconciliation, and rollback are verified;
- `RENAME_COLUMN` remains supported;
- verified `CAST_COLUMN` remains supported under the Phase 3.1 safety contract;
- `WITH_DROP_INVALID` remains forbidden.

The phase-entry behaviour must remain backward compatible.

---

## 3. Problem statement

Column order is part of Aegis's registered Gold schema and part of the
deterministic DataFrame and publication contract. It affects:

- positional downstream consumers;
- stable-view output shape;
- schema comparison;
- healing-manifest evidence;
- sandbox/live output fingerprints;
- reproducibility of approved repairs.

The Inspector already identifies when:

```text
observed column order != Gold column order
```

However, detection currently terminates without a repair plan when the drift
is reorder-only. This is not a case that requires inference: if both schemas
contain the exact same unique column names and exact same logical dtypes, the
Gold order is already registered and authoritative.

The safe operation is therefore a permutation, not a data transformation.

---

## 4. Terminology

### 4.1 Observed order

The ordered list in `ObservedSchema.column_order` produced from the complete
dataset used for the current simulation or revalidation.

### 4.2 Gold order

The ordered list in the selected immutable Schema Registry version.

### 4.3 Reorder-only drift

A schema delta for which all of the following are true:

- `reorder_event` is `True`;
- `missing_columns` is empty;
- `new_columns` is empty;
- `type_mismatches` is empty;
- observed and Gold column counts are equal;
- both orders contain unique string names;
- both orders contain exactly the same names.

### 4.4 Compound drift

Any reorder event accompanied by a missing column, new column, type mismatch,
duplicate label, invalid label, or unequal column membership.

Compound drift is not reorder-only drift.

### 4.5 Order repair

A permutation of complete DataFrame columns from the observed order to the
exact Gold order with no other mutation.

---

## 5. Scope

Phase 3.3 includes:

1. A typed, deterministic reorder action contract.
2. Strict parsing and canonical serialization of that action.
3. Consultant proposal of one reorder plan for reorder-only drift.
4. Fail-closed rejection of compound drift as an order repair.
5. Sandbox execution on a working copy.
6. Exact preservation validation for all non-order dataset properties.
7. Approval, manifest, schema-version, snapshot, and ingestion-run lineage.
8. Source-backed revalidation before live execution.
9. Live publication through the registered Phase 3.2 publication target.
10. Existing target-lock, marker, reconciliation, immutable-version, and
    rollback semantics.
11. Additive API response evidence where required.
12. Pure, repository, API, PostgreSQL, live-publication, reconciliation,
    rollback, migration-head, and full-regression verification.
13. Phase reports and formal closure.

---

## 6. Explicit non-goals

Phase 3.3 does not introduce:

- `ADD_COLUMN`;
- `DROP_COLUMN`;
- missing-column value synthesis;
- default-value inference;
- null-filling;
- row deletion;
- row reordering or sorting;
- duplicate-column repair;
- rename inference beyond existing Phase 2 behaviour;
- combined reorder-and-cast execution;
- combined reorder-and-rename execution;
- multi-plan orchestration;
- automatic retry with a different repair;
- changes to Gold schemas;
- automatic continuity across source column renames;
- caller-controlled source or publication identity;
- streaming or change-data-capture ingestion;
- query or storage optimization;
- business-meaning inference;
- credentials or connection URLs in governance records;
- weakening of Phase 3.1 conversion safety;
- weakening of Phase 3.2 identity, drift, publication, or rollback safety.

Missing and unexpected columns remain visible drift. They must not be silently
treated as reorder candidates.

---

## 7. Mandatory safety invariants

### 7.1 Exact eligibility

An order plan may be proposed only for reorder-only drift as defined in
Section 4.3.

The implementation must independently prove eligibility. It must not rely on
`reorder_event` alone.

### 7.2 Gold authority

Only the selected immutable Schema Registry version defines the target order.
The caller cannot provide, override, or edit the target order in simulation,
approval, or live-execution requests.

### 7.3 Explicit approved order

The persisted repair action must contain the complete ordered Gold column list.
Execution must compare the approved list with the persisted Gold schema again.
An action whose list differs from the selected schema version must fail closed.

### 7.4 No compound execution

An order action may not conceal or accompany any other schema change. If fresh
source revalidation finds missing, new, renamed, duplicated, or type-mismatched
columns, live execution must stop before publication.

### 7.5 No data mutation

An order repair may change only the position of complete columns.

It must preserve exactly:

- row count;
- row order;
- index values;
- index order;
- index name;
- index dtype;
- column names;
- every column dtype;
- every scalar value;
- null positions and null kinds as supported by the existing codec;
- Decimal, UUID, date, datetime, Timestamp, infinity, and nested object values;
- source DataFrame contents;
- Gold schema contents.

### 7.6 Sandbox isolation

The supplied source DataFrame must never be mutated. The Surgeon must build and
validate a separate candidate. Failure must return or retain the pristine
sandbox state according to the existing all-or-nothing contract.

### 7.7 Determinism

Given the same observed schema, Gold schema, approved action, and dataset, the
plan, corrected DataFrame, manifest evidence, and output fingerprint must be
identical.

### 7.8 Approval integrity

Source-backed reorder tickets remain subject to meaningful human approval.
Confidence is not authorization. The existing source-backed rule that always
creates a pending approval ticket remains in force.

### 7.9 Lineage integrity

The approved ticket, healing manifest, and live execution must remain bound to
the same:

- schema version;
- source system;
- source dataset;
- approved simulation snapshot;
- simulation ingestion run;
- revalidation ingestion run where applicable;
- publication system;
- publication target.

### 7.10 Live revalidation

Live execution must reread the complete trusted source and prove that its
current identity, schema, dataset fingerprint, primary key, and row count meet
the Phase 3.2 contract.

It must then re-establish reorder-only eligibility. Approval-time eligibility
must never be assumed to remain true.

### 7.11 Output identity

The freshly recomputed live corrected DataFrame must match the approved sandbox
output fingerprint before any target-side publication transaction begins.

### 7.12 Publication integrity

Publication must use the registered publication target only. It must retain:

- fixed publication schemas;
- immutable physical version tables;
- stable consumer-facing views;
- per-target advisory locking;
- operation markers;
- ambiguous-outcome reconciliation;
- stale rollback protection;
- immutable historical versions.

### 7.13 Fail closed

Malformed action text, unknown action versions, duplicate names, non-string
names, unequal membership, stale Gold order, source drift, fingerprint drift,
target incompatibility, or unverifiable preservation must produce no applied
repair and no publication.

### 7.14 Historical compatibility

Existing rename and cast tickets, manifests, executions, and migrations must
continue to decode and behave unchanged.

---

## 8. Action contract

### 8.1 Canonical action

The canonical action is:

```text
REORDER_COLUMNS TO ["customer_id","email","created_at"]
```

The suffix is a compact JSON array containing the complete Gold order.

Canonical serialization must use:

- JSON array syntax;
- string elements only;
- no duplicate elements;
- exact element order;
- compact separators;
- deterministic Unicode handling;
- no caller-provided formatting retained as canonical evidence.

### 8.2 Typed representation

Parsing must produce an immutable typed representation, conceptually:

```text
ParsedColumnOrderAction(
    target_order=("customer_id", "email", "created_at")
)
```

The implementation may choose the final class and module names, but the
representation must be frozen or otherwise immutable.

### 8.3 Parser rules

The parser must reject:

- non-string action values;
- wrong prefixes;
- missing `TO`;
- empty suffixes;
- invalid JSON;
- non-array JSON;
- empty arrays for non-empty schemas;
- non-string array elements;
- duplicate names;
- trailing tokens outside the JSON value;
- target lists that differ from the persisted Gold order;
- target lists whose membership differs from the observed order.

### 8.4 Legacy isolation

The reorder parser must not reinterpret `RENAME_COLUMN`, `CAST_COLUMN`, or any
unknown action. Existing parsers retain responsibility for their own actions.

---

## 9. Inspector contract

The Phase 3.3 implementation does not need to change the public `SchemaDelta`
shape merely to expose reorder eligibility. Existing fields are sufficient.

However, the reorder eligibility layer must validate:

```text
reorder_event == True
missing_columns == []
new_columns == []
type_mismatches == {}
set(observed_order) == set(gold_order)
len(observed_order) == len(gold_order)
all names unique and strings
```

If implementation uncovers an inability of the Inspector to represent a
required proof, work must pause for an architecture amendment rather than
silently altering `SchemaDelta`.

---

## 10. Consultant contract

### 10.1 Proposal conditions

The Consultant may append exactly one order RepairPlan only when strict
reorder-only eligibility succeeds.

### 10.2 Plan contents

The plan must contain:

- the canonical action from Section 8;
- a fixed confidence selected and tested by implementation;
- an explanation that states the dataset has identical columns and dtypes but
  differs from the registered Gold order;
- no data values;
- no credentials or connection details.

The fixed confidence must not be presented as proof and must not bypass the
source-backed approval rule.

### 10.3 Compound drift

The Consultant must not append an order plan when any other drift exists. A
later simulation may propose ordering after another independently governed
repair has completed and the dataset is observed again.

### 10.4 Selection tier

`REORDER_COLUMNS` is non-destructive. Governance selection may place it in the
same non-destructive tier as rename and verified strict cast, but selection
must not combine plans.

---

## 11. Surgeon contract

### 11.1 Dispatch

The Surgeon may dispatch `REORDER_COLUMNS` only through the strict parser and
eligibility validator. Prefix matching without complete parsing is
insufficient.

### 11.2 Candidate construction

The candidate must be constructed from a controlled working copy using the
exact approved order. The operation must be a column permutation only.

### 11.3 Preservation proof

Before success, the Surgeon must prove:

1. candidate columns equal the exact Gold order;
2. candidate and input have identical column membership;
3. candidate and input have identical row counts;
4. candidate and input have identical index values, order, name, and dtype;
5. each named column has the identical dtype before and after;
6. each named column has identical ordered values and null positions before
   and after;
7. returning the candidate to the original column order yields content equal
   to the pristine input under an exact deterministic comparison;
8. the original DataFrame remains unchanged.

An inability to prove any item is a failed repair.

### 11.4 Manifest

Every attempt must emit the existing HealingManifest shape with accurate:

- action;
- applied state;
- validation state and message;
- original and final row counts;
- risk and integrity status;
- schema version and source lineage;
- sandbox output fingerprint when successful.

Because this is not a type conversion:

- approval `conversion_decision` must remain SQL `NULL`;
- manifest `conversion_outcome` must remain SQL `NULL`;
- public conversion metadata must remain `null`.

### 11.5 Failure behaviour

Malformed, stale, ineligible, or preservation-failing order actions must:

- set `applied=False`;
- set validation failure;
- avoid partial mutation;
- avoid exposing row values in messages;
- remain deterministic.

---

## 12. Governance and approval contract

### 12.1 Submission

Approval submission continues to persist the complete target dataset replay
payload and Phase 3.2 lineage. The canonical reorder action is stored in the
existing proposed-action field.

### 12.2 Approval-time validation

Approval must reject a reorder ticket if:

- the action cannot be parsed;
- target order differs from the ticket's persisted Gold schema;
- the ticket's observed and Gold schemas do not prove reorder-only drift;
- the replay dataset does not contain the required unique column membership;
- non-cast conversion metadata is present.

### 12.3 Decision audit

Existing operator validation, row locking, state transitions, notes, and
timestamps remain unchanged.

### 12.4 No new authorization shortcut

Phase 3.3 must not introduce an endpoint or configuration flag that bypasses
normal governance.

---

## 13. API contract

### 13.1 Existing endpoints

Phase 3.3 uses the existing endpoints:

```text
POST /simulate-migration
POST /simulate-migration-from-source
GET  /approvals
GET  /approvals/{ticket_id}
POST /approvals/{ticket_id}/approve
POST /approvals/{ticket_id}/reject
POST /approvals/{ticket_id}/execute-live
GET  /live-executions/{live_execution_id}
POST /live-executions/{live_execution_id}/rollback
```

No new write endpoint is authorized by this specification.

### 13.2 Request models

No request may accept a caller-provided target order. Existing simulation
schema selection remains the sole route to Gold order.

### 13.3 Responses

Existing `proposed_repair` and ticket representations may expose the canonical
action. Additive order-specific metadata is permitted only if it is:

- derived from persisted evidence;
- redacted of data values;
- backward compatible;
- covered by API tests.

### 13.4 Errors

The API must preserve the existing distinction between:

- request or eligibility failure;
- missing resources;
- state conflict;
- disabled live execution;
- target-lock conflict;
- ambiguous external outcome.

Order-specific failure must not be reported as a successful no-op.

---

## 14. Live-execution contract

### 14.1 Gates

All existing live gates remain mandatory, including:

- global live-execution switch;
- approved ticket;
- explicit confirmation;
- valid operator;
- live-eligible source-backed ticket;
- Schema Registry lineage;
- successful sandbox manifest;
- approved output fingerprint;
- registered source and publication identity;
- no earlier execution of the same ticket;
- no unresolved execution against the target;
- target advisory lock;
- source revalidation;
- target compatibility.

### 14.2 Fresh reorder proof

After rereading the complete source, live execution must regenerate the
observed schema and prove reorder-only eligibility against the persisted Gold
schema. It must not trust the approval-time delta alone.

### 14.3 Fresh deterministic execution

The live path must recompute through the Surgeon. It must not publish the
approval replay payload as a substitute for fresh source execution.

### 14.4 Fingerprint equality

The fresh reordered output fingerprint must equal the approved sandbox output
fingerprint before publication begins.

### 14.5 Publication order

The immutable physical table and stable view must expose the Gold column order.
Writer compatibility checks must remain fail closed. Phase 3.3 does not
authorize silently changing an already published consumer contract whose
ordered view definition is incompatible.

### 14.6 Outcome handling

RUNNING, COMPLETED, FAILED, outcome-unknown, reconciliation, ROLLING_BACK, and
ROLLED_BACK behaviour remains governed by the existing marker and lock proofs.

---

## 15. Rollback contract

Rollback remains a view repoint to the previous immutable physical version, or
view removal for a first publication.

It must:

- operate only on the latest completed execution for the target;
- verify the target-side marker and expected current version;
- retain both physical versions;
- preserve registered publication identity;
- reconcile ambiguous outcomes rather than guessing;
- never mutate source data or governance history.

An order repair does not receive a special destructive rollback path.

---

## 16. Persistence and migration decision

### 16.1 Presumed migration impact

No new governance table or column is presently required. Existing records
already persist:

- proposed action text;
- observed and Gold schemas;
- replay dataset;
- schema version;
- manifest;
- output fingerprint;
- source and snapshot lineage;
- publication target identity;
- live execution state.

### 16.2 Alembic head

The expected Alembic head remains `0005`.

### 16.3 Amendment gate

If implementation establishes that durable order-specific evidence cannot be
represented safely in the current model, work must stop. A proposed migration
`0006` and an amended architecture must be reviewed before schema changes are
made.

Existing migration `0005` must not be amended after its verified integration
into `main`.

---

## 17. Concurrency and idempotency

### 17.1 Simulation determinism

Repeated simulation over the same schema version and dataset must generate the
same canonical order action and corrected fingerprint.

### 17.2 Already ordered input

If observed order already equals Gold order, no order repair is proposed.

### 17.3 Approval concurrency

Existing ticket row locks and one-way PENDING transitions remain authoritative.

### 17.4 Live concurrency

Existing ticket locking, one-execution-per-ticket rule, unresolved-target gate,
and per-target advisory lock remain authoritative.

### 17.5 Retry behaviour

An ambiguous publish or rollback response is resolved through marker-based
reconciliation. It is not treated as permission to publish the same ticket
again.

---

## 18. Security and privacy

Phase 3.3 must not persist or return:

- source row values as diagnostic samples;
- credentials;
- usernames or passwords;
- connection URLs;
- unredacted conversion data;
- caller-invented identity.

The canonical action contains schema column names because they are necessary
governance evidence. Error messages must describe structural failure without
including source values.

Malformed action input must never be evaluated as code. Parsing is strict JSON
parsing after exact action-prefix validation.

---

## 19. Required tests

### 19.1 Action-model and parser tests

Tests must cover:

- canonical serialization;
- deterministic Unicode serialization;
- immutable parsed target order;
- malformed prefixes;
- missing `TO`;
- invalid JSON;
- non-array JSON;
- non-string elements;
- duplicate names;
- empty or trailing payloads;
- stale Gold order;
- membership mismatch;
- isolation from rename and cast parsers.

### 19.2 Consultant tests

Tests must prove:

- one plan for pure reorder drift;
- no plan when already ordered;
- no order plan with missing columns;
- no order plan with new columns;
- no order plan with type mismatch;
- no order plan for rename-shaped drift;
- deterministic action, confidence, and explanation;
- non-destructive selection classification.

### 19.3 Surgeon tests

Tests must prove:

- exact Gold ordering;
- source DataFrame unchanged;
- value and null preservation;
- dtype preservation;
- row and index preservation;
- Decimal, UUID, temporal, infinity, and nested-object preservation;
- empty DataFrame handling;
- single-column already-ordered behaviour;
- malformed action rejection;
- stale target order rejection;
- duplicate-label rejection;
- compound-drift rejection;
- atomic failure on unexpected exceptions;
- accurate manifest and output fingerprint;
- SQL-null conversion metadata contract.

### 19.4 Governance and repository tests

Tests must cover:

- ticket round trip;
- approval-time reorder validation;
- non-cast conversion metadata rejection;
- manifest persistence;
- source and publication lineage preservation;
- historical rename and cast round trips;
- JSONB SQL `NULL` behaviour.

### 19.5 Sandbox API tests

Tests must cover:

- sample-backed reorder simulation remains non-live-eligible;
- source-backed reorder simulation creates a pending live-eligible ticket;
- canonical action is returned;
- no-plan response for already ordered input;
- compound drift does not masquerade as reorder-only;
- approval and rejection state behaviour.

### 19.6 Live PostgreSQL tests

Tests must cover:

- complete source read in non-Gold order;
- approval followed by successful live reorder publication;
- stable view exposes Gold order;
- physical version is immutable;
- values and PostgreSQL types survive publication;
- source drift after approval blocks execution;
- reordered approval fingerprint mismatch blocks publication;
- registered source identity and binding drift gates;
- registered publication target identity;
- repeated execution conflict;
- republish creates a new version;
- rollback restores the preceding version and its order;
- first-publish rollback removes only the view;
- stale rollback rejection;
- target-lock concurrency;
- publish and rollback marker recovery;
- RUNNING and ROLLING_BACK reconciliation.

### 19.7 Migration and compatibility tests

Tests must prove:

- Alembic still has one head, `0005`, unless an approved amendment changes it;
- upgrade/downgrade history remains valid;
- all prior Phase 2 and Phase 3.1/3.2 behaviour remains green;
- legacy records remain readable.

### 19.8 Full regression

The final suite must run against dedicated PostgreSQL governance, source, and
publication test databases. No test may fall back to production configuration.

The phase-entry floor is:

```text
483 passed
```

Closure requires all existing and new tests to pass.

---

## 20. Delivery sequence

### 3.3.1 — Architecture and safety contract

- approve this specification;
- commit it alone on the phase branch;
- publish and verify the phase branch;
- make no runtime implementation change.

### 3.3.2 — Deterministic order model and planning

- implement immutable action model;
- implement strict parser and canonical serializer;
- implement reorder-only eligibility proof;
- integrate Consultant proposal;
- integrate non-destructive governance classification;
- add pure tests.

### 3.3.3 — Sandbox Surgeon integration

- add strict Surgeon dispatch;
- build candidate on a controlled copy;
- implement preservation proof;
- emit manifest and output fingerprint;
- keep conversion metadata absent;
- add atomicity and adversarial tests.

### 3.3.4 — Governance and API integration

- validate reorder evidence at submission and approval;
- preserve schema, snapshot, ingestion, and identity lineage;
- expose only backward-compatible response evidence;
- verify sample-backed and source-backed flows;
- add repository and API tests.

### 3.3.5 — Controlled live publication

- revalidate complete source and reorder eligibility;
- recompute through Surgeon;
- prove approved output fingerprint;
- publish through registered target;
- verify stable view order, immutable versions, markers, reconciliation, and
  rollback;
- add PostgreSQL live tests.

### 3.3.6 — PostgreSQL verification

- run focused pure tests;
- run governance repository tests;
- run source and publication integration tests;
- verify migration head/history;
- commit a verification report containing exact commands and results.

### 3.3.7 — Regression, documentation, and closure

- run a fresh-image compile and complete regression;
- confirm a clean repository;
- confirm linear ancestry from the verified base;
- confirm local and remote phase heads match;
- evaluate every closure criterion;
- commit and publish the closure report;
- merge only through a separately verified explicit merge commit.

---

## 21. Closure criteria

Phase 3.3 may close only when all of the following pass:

1. The canonical order action is deterministic and strictly parsed.
2. Only reorder-only drift can produce an order plan.
3. Compound drift cannot use the order path.
4. The Gold schema is the sole target-order authority.
5. Sandbox execution changes only column position.
6. Source DataFrames are never mutated.
7. Rows, index, values, nulls, and dtypes are exactly preserved.
8. Malformed and stale actions fail atomically.
9. Successful attempts emit deterministic manifests and fingerprints.
10. Reorder records carry no conversion metadata.
11. Sample-backed tickets remain ineligible for live execution.
12. Source-backed tickets retain meaningful human approval.
13. Live execution revalidates source identity and reorder eligibility.
14. Fresh live output equals the approved sandbox fingerprint.
15. Publication uses only the registered publication target.
16. Stable views expose exact Gold order without mutable physical history.
17. Reconciliation and rollback remain marker- and lock-proven.
18. Alembic history remains linear and verified.
19. All prior tests remain green.
20. All new tests and the complete PostgreSQL regression pass.

---

## 22. Locked decisions summary

| Decision | Phase 3.3 rule |
|---|---|
| Feature | Verified column-order repair |
| Eligible drift | Reorder-only |
| Target authority | Immutable Gold schema version |
| Action | Canonical JSON-backed `REORDER_COLUMNS TO [...]` |
| Compound drift | Refused |
| Missing columns | Detected, not synthesized |
| Unexpected columns | Detected, not dropped |
| Row changes | Forbidden |
| Value changes | Forbidden |
| Dtype changes | Forbidden |
| Sandbox | Mandatory controlled copy |
| Source-backed approval | Human approval mandatory |
| Live source | Complete trusted source reread |
| Live proof | Fresh fingerprint equals approved sandbox fingerprint |
| Publication | Registered target, immutable table, stable view |
| Rollback | Existing marker-verified view repoint |
| Conversion metadata | SQL `NULL` |
| Migration | None presumed; `0005` remains head |
| Streaming/CDC | Out of scope |

---

## 23. Architecture approval record

The user explicitly approved this Phase 3.3 architecture on 2026-08-13.

Before implementation:

1. this exact approved document must be committed alone as Phase 3.3.1;
2. the commit and remote phase branch must be verified;
3. Phase 3.3.2 implementation must begin only from that clean verified commit.
