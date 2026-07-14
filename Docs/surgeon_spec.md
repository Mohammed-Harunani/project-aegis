# Aegis_Surgeon — V1 Spec

## Responsibility
Execute an approved RepairPlan in a controlled, reversible, and fully auditable manner.

Aegis_Surgeon is the only component allowed to modify schemas or data.

---

## Inputs
- RepairPlan (approved)
- ObservedSchema (current)
- Gold_Schema (reference)
- Target dataset (sandboxed)

---

## Outputs
- Applied transformation (if successful)
- Validation result (pass/fail)
- Updated ObservedSchema (post-repair)

---

## Safety Rules (Mandatory)
- All changes must run in a sandbox first
- Validation must succeed before commit
- All operations must be reversible
- No partial application (all-or-nothing)
- A HealingManifest must be generated before commit

---

## Determinism & Control (V1)
- No autonomous decision-making
- Executes only explicitly approved plans
- No learning or adaptation

---

## Explicit Non-Goals (V1)
Aegis_Surgeon does NOT:
- Propose repair plans
- Modify data without approval
- Perform business logic changes
- Optimize queries or performance
- Operate outside sandboxed context
---

## Sandbox Expectations (V1)

- All execution must occur on a temporary copy of the target dataset
- No operation may touch the original dataset during validation
- Validation must confirm schema correctness before commit
- Commit step is explicit and separate from execution
- If validation fails, no changes are persisted
