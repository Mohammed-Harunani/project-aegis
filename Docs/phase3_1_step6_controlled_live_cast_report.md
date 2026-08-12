# Phase 3.1.6 — Controlled Live CAST_COLUMN Enablement

## Scope

Phase 3.1.6 enables live `CAST_COLUMN` execution only for explicitly configured
logical source-target pairs. It does not broaden the verified conversion matrix,
weaken human approval, permit `WITH_DROP_INVALID`, or change Phase 2.5's
immutable publication, stable-view, reconciliation, and rollback architecture.

## Fail-closed configuration

`AEGIS_LIVE_CAST_ALLOWLIST` is empty by default. Its strict format is a
comma-separated set of `source->target` tokens, for example:

```text
AEGIS_LIVE_CAST_ALLOWLIST=object->int64,int64->float64
```

Malformed, duplicate, unsupported, or non-allowlisted pairs are rejected. Pair
resolution uses the source table's trusted logical schema metadata rather than
pandas' inferred dtype label. PostgreSQL text/varchar metadata maps explicitly
to the Aegis logical dtype `object`; a pandas 3.x native `str` label is not
silently aliased into the live allowlist. Synthetic tests therefore pin
`dtype=object` when modelling a trusted PostgreSQL text source.

## Mandatory execution gates

An allowlisted live cast still requires all of the following:

1. complete trusted-source read and persisted provenance;
2. a deterministic `SAFE` conversion decision with zero failures;
3. meaningful human approval and a matching sandbox conversion outcome;
4. the pair remaining allowlisted at execution time;
5. unchanged source fingerprints on the fresh full-table re-read;
6. a fresh live Surgeon outcome identical to both the approved decision and
   sandbox manifest;
7. unchanged row count, order, index, and null positions;
8. complete post-repair Gold-schema validation;
9. exact corrected-output fingerprint parity with the sandbox result;
10. Phase 2.5 immutable physical publication and stable-view publication.

Removing a pair after approval revokes live eligibility without changing the
approved audit record. `RENAME_COLUMN` behavior remains unchanged.

## Changed files

- `.env.example`
- `Docs/phase3_1_step6_controlled_live_cast_report.md`
- `src/api/app.py`
- `src/live_execution/__init__.py`
- `src/live_execution/cast_allowlist.py`
- `src/surgeon/surgeon.py`
- `tests/test_conversion_governance_api_logic.py`
- `tests/test_live_cast_allowlist.py`
- `tests/test_live_execution_api.py`
- `tests/test_surgeon.py`

## Verification status

Package-author verification covers deterministic allowlist, governance, Surgeon,
API-logic, existing pure regression, and compilation tests. PostgreSQL-backed
publication, reconciliation, rollback, and complete-suite evidence must be
established by the repository owner against the verified disposable databases
before Phase 3.1.6 can close.
