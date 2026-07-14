# Aegis Governance – Healing Manifest Persistence (V1.2)

## Purpose

The HealingManifest must be permanently recorded after every approved execution.

This ensures:

- Audit traceability
- Regulatory compliance readiness
- Historical repair tracking
- Forensic analysis capability

## Storage Location

All manifests are stored under:

/manifests/

## File Naming Convention

Format:

manifest_<ISO8601_TIMESTAMP>.json

Example:

manifest_2026-03-23T15-43-40.json

Colons are replaced with hyphens to ensure filesystem compatibility.

## Persistence Rules

- Manifest is written only after successful execution.
- Execution mode must be recorded.
- Operator identity must be recorded.
- Component versions must be recorded.
- Manifest files are immutable once written.
- Overwrite is not allowed.

## Responsibility Boundary

- Surgeon produces HealingManifest object.
- Orchestrator persists it.
- Inspector and Consultant are not aware of persistence.