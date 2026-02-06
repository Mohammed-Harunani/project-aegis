# Aegis_Inspector — V1 Spec

## Responsibility
Compute schema + statistical fingerprints and detect schema-level deltas.

## Inputs
- Current table schema
- Previous Gold_Schema

## Outputs
- Fingerprint (JSON)
- Delta description (JSON)

## Out-of-Scope (V1)
- Value-level anomaly detection
- Any repair logic
