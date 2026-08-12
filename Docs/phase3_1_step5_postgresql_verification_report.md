# Phase 3.1.5 PostgreSQL Verification Report

## Status

Correction candidate prepared after the first PostgreSQL-enabled integration run exposed a JSONB NULL persistence mismatch. Phase 3.1.5 remains open until owner-run PostgreSQL verification passes and the correction is committed and pushed.

## Observed defect

Migration `0004` correctly permits SQL `NULL` or a JSON object for `approval_tickets.conversion_decision` and `healing_manifests.conversion_outcome`. The ORM mappings used PostgreSQL `JSONB` with its default `none_as_null=False` behavior. Consequently, Python `None` was bound as JSON `null`, not SQL `NULL`, and PostgreSQL correctly rejected the row because `jsonb_typeof('null'::jsonb)` is `null`, not `object`.

This affected non-`CAST_COLUMN` paths where no conversion metadata exists, including `RENAME_COLUMN` approval tickets and manifests. The many test failures were a cascade from this single persistence mismatch.

## Correction

Both nullable conversion metadata columns now use:

```python
JSONB(none_as_null=True)
```

The database check constraints remain unchanged. Therefore:

- Python `None` persists as SQL `NULL`.
- Present conversion metadata must still be a JSON object.
- Raw values and converted series remain excluded from persistence.
- No live `CAST_COLUMN` execution is enabled by this correction.

## Verification gates

The owner verification script performs:

1. Package hash and exact-scope validation.
2. Four focused ORM/constraint contract tests.
3. PostgreSQL integration tests for API, schema registry, and live execution.
4. The complete PostgreSQL-enabled repository suite.
5. Compilation and clean-scope verification.

A passing local pure-Python run is supporting evidence only. Phase 3.1.5 closure requires the owner-run PostgreSQL evidence.
