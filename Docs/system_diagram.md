# Project Aegis — System Diagram (V1)

```mermaid
flowchart TD
    A[Dataset] --> B[Aegis_Inspector]

    B --> C[ObservedSchema]
    B --> D[SchemaDelta]

    D --> E[Aegis_Consultant]

    E --> F[RepairPlan]

    F --> G[Aegis_Surgeon]

    G --> H[ExecutionResult]
    G --> I[HealingManifest]

    style B fill:#e3f2fd
    style E fill:#e8f5e9
    style G fill:#fff3e0
    style I fill:#fce4ec
