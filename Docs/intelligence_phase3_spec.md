# Aegis Intelligence Expansion – Phase 3

## Phase 3.1 – Type Mismatch Repair

### Purpose

Enable Aegis to reason about type mismatches between ObservedSchema and GoldSchema.

Example:

Gold:
customer_id → int64

Observed:
customer_id → object

System should propose:

CAST_COLUMN customer_id TO int64

### Rules

- Only propose cast when column exists in both schemas.
- Only act when dtype differs.
- Confidence score: 0.78 (below approval threshold by default).
- Do not auto-apply.
- Governance still controls execution.

### Responsibility Boundary

Inspector → Detect mismatch  
Consultant → Propose CAST_COLUMN  
Surgeon → Executes only if governance allows  
Orchestrator → Remains policy-only