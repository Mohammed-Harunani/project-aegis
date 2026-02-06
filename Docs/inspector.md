# Aegis_Inspector (V1)
> **Status:** V1 LOCKED — behavior changes require a version bump.

## Purpose
Aegis_Inspector is responsible for observing the structure and basic statistical properties of a dataset and detecting schema-level changes relative to a known stable reference (Gold_Schema).

It performs no repairs and makes no decisions beyond detection.

---

## Responsibilities
Aegis_Inspector performs the following actions:
- Observe a dataset and compute a deterministic schema fingerprint
- Capture column order, data types, null counts, and unique counts
- Compare an observed schema against a Gold_Schema
- Detect schema-level deltas only

---

## Inputs
- A Pandas DataFrame representing the current dataset
- A previously recorded Gold_Schema (ObservedSchema)

---

## Outputs
- **ObservedSchema**
  - Mapping of column names to basic column statistics
  - Preserved column order
- **SchemaDelta**
  - List of missing columns
  - List of new columns
  - Data type mismatches
  - Boolean reorder indicator

---

## Determinism & Safety Guarantees
- All computations are deterministic
- No randomness or external state is used
- No data is modified
- No writes are performed
- No side effects occur

---

## Explicit Non-Goals (V1)
Aegis_Inspector does NOT:
- Perform value-level anomaly detection
- Infer business meaning or intent
- Propose or apply repairs
- Compute confidence scores
- Interact with downstream systems
- Modify data or schemas

---

## Contract Compliance
Aegis_Inspector strictly adheres to the Project Aegis System Contract:
- Read-only behavior
- Deterministic outputs
- Clear separation from repair and decision logic
