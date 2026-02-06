# Aegis_Consultant — V1 Spec

## Responsibility
Analyze a SchemaDelta and propose one or more possible repair plans with an associated confidence score and explanation.

---

## Inputs
- SchemaDelta
- ObservedSchema (current)
- Gold_Schema (reference)

---

## Outputs
- List of RepairPlan objects containing:
  - proposed_action
  - confidence
  - explanation

---

## Determinism & Safety (V1)
- No automatic application of changes
- No data modification
- Proposals only

---

## Explicit Non-Goals (V1)
Aegis_Consultant does NOT:
- Execute any repair
- Modify schemas or data
- Learn or update itself
- Use external systems or APIs
- Perform value-level analysis
