# Project Aegis — System Overview (V1)

## Purpose
Project Aegis is a governed, self-healing data system designed to detect, reason about, and safely repair schema-level data issues without breaking downstream consumers.

The system prioritizes determinism, auditability, and human trust over automation speed.

---

## Core Components

### 1. Aegis_Inspector (V1 — LOCKED)
**Role:** Observation  
- Reads datasets
- Produces ObservedSchema
- Detects SchemaDelta
- No side effects

---

### 2. Aegis_Consultant (V1 — LOCKED)
**Role:** Reasoning  
- Analyzes SchemaDelta
- Proposes RepairPlan objects
- Assigns confidence and explanation
- Never executes changes

---

### 3. Aegis_Surgeon (V1 — LOCKED)
**Role:** Execution  
- Executes approved RepairPlans
- Sandbox-first execution
- No silent writes
- Always emits a HealingManifest

---

### 4. HealingManifest (V1 — LOCKED)
**Role:** Governance  
- Immutable audit record
- Generated for every execution attempt
- Captures intent, result, and context
- Append-only by design

---

## End-to-End Data Flow

