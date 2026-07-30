# Phase 3.1.3 — Sandbox Surgeon Integration Report

**Status:** implementation candidate; owner verification and commit required.
**Parent commit:** `b2badc7` — Phase 3.1.2 verified type-conversion engine.
**Branch:** `phase-3.1-verified-type-repair`.
**Live `CAST_COLUMN`:** still blocked.

## 1. Scope

Phase 3.1.3 integrates the verified type-repair engine into
`AegisSurgeon` for sandbox `CAST_COLUMN` execution only.

This stage does not:

- enable live casting;
- change Inspector or Consultant proposal behavior;
- persist conversion metadata to PostgreSQL;
- change approval rules;
- add a live conversion allowlist;
- modify publication, reconciliation, or rollback behavior.

Those concerns remain assigned to Phases 3.1.4–3.1.6.

## 2. Runtime changes

### 2.1 Verified CAST_COLUMN delegation

`AegisSurgeon` no longer performs direct pandas `.astype()` calls for
`CAST_COLUMN`.

For a sandbox cast, Surgeon now:

1. parses the approved V1 action;
2. rejects destructive `WITH_DROP_INVALID` plans;
3. delegates complete-series analysis to
   `type_repair.analyze_and_convert()`;
4. records redacted outcome metadata;
5. independently validates the structure of a `SAFE` result;
6. applies the converted series only when the result is `SAFE`;
7. replaces only the named column in a candidate sandbox copy;
8. runs the existing post-operation schema-order validation;
9. emits a `HealingManifest` containing the resulting sandbox dataset.

### 2.2 Atomic rejection

For `UNSAFE`, `UNSUPPORTED`, or `ERROR` conversion results:

- `ExecutionResult.applied` is `False`;
- validation fails;
- no converted series is assigned;
- row count, order, index, and source values remain unchanged;
- the manifest contains the unchanged sandbox copy.

An unexpected sandbox exception restores a pristine pre-operation copy
before the manifest is created.

### 2.3 Destructive plan rejection

`CAST_COLUMN ... WITH_DROP_INVALID` is rejected before the conversion
engine is called.

No rows are dropped, and the integrity status remains
`NO_VOLUME_CHANGE`.

### 2.4 Live boundary

Surgeon itself now blocks `CAST_COLUMN` whenever
`execution_mode != "sandbox"`, even if a caller includes `live` in
`allowed_modes`.

This is defence in depth in addition to the existing live-capable API
rejection. `RENAME_COLUMN` live behavior remains available and
unchanged.

## 3. Manifest behavior

A new immutable `ConversionOutcomeMetadata` model records a redacted
summary for casts that actually reach the verified engine:

- column name;
- conversion status;
- source and target dtype;
- policy version;
- total, null, converted, failed, and diagnostic counts;
- unique reason codes in deterministic first-seen order.

It contains no raw source values and no row indexes.

`HealingManifest.conversion_outcome` is optional:

- verified cast analysis: populated;
- rename, blocked live cast, destructive plan rejection, missing column,
  or malformed action: `None`.

This metadata is deliberately in-memory only in Phase 3.1.3.
`manifest_repository.py`, database models, migrations, and API response
contracts are not extended here. Formal persistence belongs to Phase
3.1.4.

## 4. Compatibility

- `RENAME_COLUMN` behavior remains intact in sandbox and live modes.
- Existing `ExecutionResult` and `ValidationResult` fields are unchanged.
- Existing manifest constructor calls remain valid because the new field
  has a default.
- Manifest persistence continues to ignore ephemeral DataFrame and
  conversion-outcome fields.
- Surgeon component version is now `1.8`.
- The type-repair policy version is added to `component_versions` only
  when the conversion engine actually ran.

## 5. Deterministic validation messages

Cast summaries include only:

- status;
- column name;
- target dtype;
- converted, failed, and null counts;
- unique reason codes;
- policy version.

Raw values and row indexes are excluded.

## 6. Tests added

`tests/test_surgeon.py` now covers:

- unchanged rename behavior;
- safe object-string to int64 conversion;
- strict boolean token conversion;
- fractional-loss rejection;
- ambiguous-boolean rejection;
- integer-to-float precision-loss rejection;
- unsupported target rejection;
- destructive drop-invalid rejection;
- live cast blocking at Surgeon boundary;
- live rename preservation;
- row/index/order/name/null preservation;
- replacement of only the named column;
- existing post-operation schema validation;
- missing and malformed action handling;
- engine `ERROR` handling;
- deterministic redacted summaries;
- immutable conversion metadata;
- rollback after an unexpected sandbox exception;
- direct source assertion that Surgeon contains no `.astype()` call;
- safe `object` widening with identity/null preservation;
- null-incompatible int64 rejection;
- rejection of malformed `SAFE` engine results before assignment.

## 7. Verification evidence

Executed in the developer environment:

```text
python -m pytest -q tests/test_surgeon.py
28 passed

python -m pytest -q tests/test_surgeon.py tests/test_type_repair_converter.py
177 passed

python -m pytest -q \
  tests/test_inspector.py \
  tests/test_consultant.py \
  tests/test_surgeon.py \
  tests/test_selector.py \
  tests/test_approval.py \
  tests/test_schema_registry_logic.py \
  tests/test_live_execution_logic.py \
  tests/test_dataset_codec.py \
  tests/test_type_repair_converter.py
245 passed

python -m pytest -q
245 passed, 3 skipped

python -m compileall -q src tests
exit code 0
```

The three skipped modules require the dedicated PostgreSQL test
databases. No PostgreSQL result is claimed in Phase 3.1.3.

## 8. Changed project files

```text
Docs/phase3_1_step3_sandbox_surgeon_report.md
src/governance/__init__.py
src/governance/manifest.py
src/surgeon/surgeon.py
tests/test_surgeon.py
```

## 9. Remaining gates

Phase 3.1.3 is not closed until the owner:

1. applies these five files to clean commit `b2badc7`;
2. runs targeted and complete pure verification locally;
3. confirms a clean Git diff limited to the five files;
4. creates one Phase 3.1.3 commit;
5. pushes the feature branch.

Live `CAST_COLUMN` remains blocked until Phase 3.1.6.
