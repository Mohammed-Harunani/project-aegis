# Phase 3.1 — Verified Type Mismatch Repair Engine

**Status:** authoritative implementation contract; Phase 3.1 is active and not yet verified.
**Baseline:** `4a5ead5` — Phase 2.5 closed and PostgreSQL-verified.
**Development branch:** `phase-3.1-verified-type-repair`

## 1. Purpose

Phase 3.1 replaces blind pandas casting with a deterministic,
diagnostic, value-preserving type-repair engine.

The existing Surgeon checks whether this operation raises an exception:

```python
series.astype(target_type)
```

That is not a sufficient safety test. Some casts succeed while changing
the meaning or precision of the data. Examples include:

- `1.9` cast to `int64`, which discards the fractional component;
- `"false"` cast to `bool`, which pandas treats as `True` because it is
  a non-empty string;
- a large integer cast to `float64`, which can lose integer precision;
- a timezone-aware timestamp cast to a timezone-naive target;
- values that parse successfully but do not survive a semantic
  round-trip.

Phase 3.1 must detect these cases before mutation, produce row-level
diagnostics, and refuse unsafe or unsupported conversions.

## 2. Scope

Phase 3.1 covers the controlled execution of `CAST_COLUMN` repair plans.

It does not change Inspector detection or Consultant proposal behavior
unless an integration stage explicitly requires additional metadata.

The phase includes:

1. a conversion-policy vocabulary;
2. a pure, deterministic conversion analyser;
3. row-level diagnostics;
4. sandbox Surgeon integration;
5. governance and approval integration;
6. PostgreSQL-backed validation;
7. controlled live enablement for an explicit allowlist only.

## 3. Non-goals

Phase 3.1 does not:

- infer business meaning from column names;
- guess locale-specific number or date formats;
- repair invalid values automatically;
- drop rows to make a cast succeed;
- round, truncate, clamp, coerce, or replace failed values;
- permit approval to override an unsafe conversion result;
- enable every possible dtype conversion;
- change the Phase 2.5 immutable-publication or rollback architecture.

## 4. Core safety invariants

Every Phase 3.1 conversion must satisfy all applicable invariants.

### 4.1 Preflight before mutation

The complete source series is analysed before any converted value is
written to the working dataset.

A single unsafe or unsupported non-null value rejects the complete
column conversion.

### 4.2 Atomicity

A rejected conversion leaves the working dataset unchanged.

No partially converted column may be returned or persisted.

### 4.3 Row preservation

A type repair must preserve:

- row count;
- row order;
- index values;
- index order;
- null positions.

`WITH_DROP_INVALID` is not allowed by the verified type-repair engine.
Dropping records is a destructive data-quality operation, not a type
conversion.

### 4.4 Semantic value preservation

The target value must represent the same business value as the source
value under the conversion policy.

A conversion that merely executes without raising is insufficient.

### 4.5 Precision preservation

Conversions must reject:

- fractional loss;
- integer overflow;
- significant-digit loss;
- non-finite values where the target does not safely represent them;
- timezone loss;
- silent boolean truthiness conversion.

### 4.6 Determinism

The same ordered input values, source dtype, target dtype, and policy
version must always produce the same:

- status;
- converted values;
- diagnostics;
- counts;
- reason codes.

The engine must not depend on locale, operating-system settings, current
time, random values, or database row order.

### 4.7 Explicit support

Unknown source-value categories and unknown target dtypes are rejected
as `UNSUPPORTED`.

There is no fallback to `object`, `TEXT`, stringification, or pandas
coercion.

## 5. Conversion outcomes

A complete column analysis returns exactly one outcome:

- `SAFE` — every non-null value converts under the policy and all
  invariants pass;
- `UNSAFE` — the conversion is understood but one or more values would
  lose meaning, precision, range, timezone, or integrity;
- `UNSUPPORTED` — the engine has no approved converter for the source
  category and target dtype;
- `ERROR` — an internal execution failure occurred and the conversion
  must not be applied.

Only `SAFE` may produce an applicable converted series.

## 6. Required result model

The implementation must expose immutable result structures equivalent
to the following conceptual model:

```text
ColumnConversionResult
    status
    source_dtype
    target_dtype
    total_count
    null_count
    converted_count
    failed_count
    diagnostics
    converted_series | None
    policy_version

ConversionDiagnostic
    row_index
    source_value_type
    target_dtype
    reason_code
    message
```

Raw sensitive values must not be written to normal application logs.
A diagnostic may retain the value in controlled in-memory/test output
only when required by the existing sandbox response contract. Future
persistent audit storage must use an explicitly reviewed redaction
policy.

## 7. Mandatory reason codes

The first implementation must define stable reason codes including:

- `PARSE_ERROR`
- `FRACTIONAL_VALUE`
- `OUT_OF_RANGE`
- `PRECISION_LOSS`
- `AMBIGUOUS_BOOLEAN`
- `NON_FINITE_NUMBER`
- `NULL_TARGET_INCOMPATIBLE`
- `TIMEZONE_LOSS`
- `UNSUPPORTED_SOURCE_VALUE`
- `UNSUPPORTED_TARGET_DTYPE`
- `ROUND_TRIP_MISMATCH`
- `INTERNAL_CONVERSION_ERROR`

Tests must assert reason codes rather than relying only on free-form
message text.

## 8. Null policy

The following values are treated as null when detected safely:

- `None`
- `pandas.NA`
- `NaN`
- `NaT`

Null positions must remain unchanged.

A cast to a target representation that cannot preserve existing nulls
is rejected as `NULL_TARGET_INCOMPATIBLE`. Phase 3.1 must not silently
fill, drop, or substitute nulls.

## 9. Initial supported conversion matrix

The first implementation stage is deliberately narrow.

### 9.1 Target `int64`

Allowed source values:

- Python and NumPy integers within signed 64-bit range;
- finite floats whose mathematical value is exactly integral and within
  signed 64-bit range;
- canonical base-10 integer strings with an optional leading `+` or
  `-`.

Rejected examples:

- `1.2`
- `"1.0"`
- `"1e3"`
- empty strings;
- comma-formatted values such as `"1,000"`;
- booleans;
- values outside `-9223372036854775808` through
  `9223372036854775807`;
- non-finite numbers.

### 9.2 Target `float64`

Allowed source values:

- finite integer and floating values that satisfy the policy's
  round-trip check;
- canonical decimal strings accepted by the explicit parser.

Additional rule:

An integer conversion is unsafe when conversion to `float64` and back
does not reproduce the exact integer. This protects integers beyond
the exact binary64 integer range.

Non-finite string tokens such as `NaN`, `Infinity`, and `-Infinity`
are rejected.

### 9.3 Target `bool`

Allowed source values:

- native boolean values;
- integer `0` and `1`;
- case-insensitive strings `true`, `false`, `0`, and `1` after
  surrounding ASCII whitespace is removed.

Rejected examples:

- all other integers;
- non-integral floats;
- `"yes"`, `"no"`, `"on"`, `"off"`;
- arbitrary non-empty strings.

Pandas truthiness casting must never be used as the validator.

### 9.4 Target `object`

A conversion to `object` is allowed only as a non-transforming widening
operation. Values must remain the same Python objects and compare
identically under the engine's value-preservation check.

The engine must not stringify values as part of an `object` cast.

### 9.5 Extended logical targets

The following Aegis logical targets remain explicitly `UNSUPPORTED`
until dedicated converters and tests are implemented:

- `decimal`
- `date`
- `datetime`
- `datetime_tz`
- `uuid`
- `json`

Their presence in the Aegis dtype vocabulary or PostgreSQL publication
mapping does not make blind `astype()` conversion safe.

Other pandas dtypes not named in the initial matrix are also
`UNSUPPORTED` for verified casting.

## 10. Round-trip validation

Where a safe inverse representation exists, the engine must perform a
semantic round-trip check.

Examples:

- integer → float64 → integer must reproduce the exact integer;
- parsed integer string → int64 → canonical integer must represent the
  same signed integer;
- parsed boolean token → bool must map through the strict token table,
  not Python truthiness.

A round-trip mismatch rejects the conversion with
`ROUND_TRIP_MISMATCH` or a more specific reason code.

## 11. Surgeon integration rules

During the sandbox-integration stage:

1. `AegisSurgeon` delegates `CAST_COLUMN` analysis and conversion to the
   new engine.
2. Surgeon does not call `.astype(target_type)` directly for a verified
   cast.
3. A non-`SAFE` result produces:
   - `ExecutionResult.applied = False`;
   - validation failure;
   - a deterministic summary message;
   - no dataset mutation.
4. A `SAFE` result replaces only the named column in the sandbox copy.
5. Existing post-operation schema validation still runs.
6. The HealingManifest records conversion outcome metadata and the
   policy version once the manifest schema is formally extended.

`RENAME_COLUMN` behavior remains unchanged.

## 12. Governance rules

- Human approval remains mandatory.
- Approval never converts `UNSAFE` or `UNSUPPORTED` into executable.
- Auto-approval remains sandbox-only.
- Phase 2.5 live `CAST_COLUMN` rejection remains active until the
  controlled-live-enablement stage.
- Live enablement must use an explicit source-target allowlist.
- The allowlist must start empty and be populated only after unit,
  integration, and PostgreSQL tests pass.

## 13. Live execution requirements

Before any `CAST_COLUMN` pair is live-enabled, tests must demonstrate:

1. full trusted-source re-read;
2. unchanged source provenance and fingerprints;
3. deterministic conversion;
4. zero failed values;
5. unchanged row count, order, index, and null positions;
6. target logical dtype match;
7. successful post-repair schema validation;
8. immutable physical publication;
9. stable-view publication;
10. target-marker reconciliation;
11. rollback to the previous published version;
12. no regression to existing live `RENAME_COLUMN` behavior.

## 14. Module boundary

The conversion engine must be implemented in a database-independent
module so its core tests require only Python and pandas.

Planned package boundary:

```text
src/type_repair/
    __init__.py
    models.py
    policy.py
    converter.py
```

The module must not import FastAPI, SQLAlchemy sessions, repositories,
or PostgreSQL connectors.

Database and API integration belongs in later Phase 3.1 stages.

## 15. Test requirements

The pure test suite must cover at minimum:

- safe integer conversions;
- fractional float to integer rejection;
- integer overflow rejection;
- large integer to float precision-loss rejection;
- strict boolean conversions;
- arbitrary string to boolean rejection;
- null preservation;
- null-target incompatibility;
- unsupported extended targets;
- row/index/order preservation;
- atomic rejection;
- deterministic diagnostics;
- input dataframe immutability;
- reason-code stability.

Property-based testing may be added, but deterministic example tests
remain mandatory.

No existing Phase 2.1–2.5 test may regress.

## 16. Phase 3.1 delivery sequence

### 3.1.1 — Safety contract

- Add this authoritative specification.
- Review and lock the initial conversion matrix.
- No runtime behavior changes.

### 3.1.2 — Pure conversion engine

- Add immutable models, policy and converter.
- Add complete pure-Python unit tests.
- Do not integrate Surgeon yet.

### 3.1.3 — Sandbox Surgeon integration

- Replace direct blind casting for `CAST_COLUMN`.
- Keep live casting blocked.
- Extend sandbox tests and manifest behavior.

### 3.1.4 — Governance and approval integration

- Persist and expose conversion decisions safely.
- Enforce approval plus conversion safety.

### 3.1.5 — PostgreSQL-backed validation

- Run targeted and complete suites against the real disposable
  PostgreSQL databases.
- Correct stateful database and migration issues discovered by testing.

### 3.1.6 — Controlled live enablement

- Enable only explicitly verified conversion pairs.
- Preserve Phase 2.5 publication, reconciliation and rollback
  guarantees.

### 3.1.7 — Documentation and closure

- Update authoritative documentation.
- Record exact test evidence.
- Commit with a clean repository.
- Close Phase 3.1 only after local PostgreSQL verification.

## 17. Phase 3.1 acceptance criteria

Phase 3.1 is complete only when:

- the approved conversion matrix is implemented;
- unsafe and unsupported casts cannot mutate data;
- row-level deterministic diagnostics are available;
- Surgeon uses the verified engine in sandbox mode;
- governance prevents unsafe execution;
- an explicit live allowlist is enforced;
- PostgreSQL-backed targeted tests pass;
- the complete project suite passes;
- documentation reflects the verified implementation;
- the final commit is created from a clean working tree.

Until then, Phase 3.1 remains active and live `CAST_COLUMN` remains
blocked.
