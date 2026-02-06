from dataclasses import dataclass
from typing import List

from inspector import ObservedSchema, SchemaDelta


@dataclass(frozen=True)
class RepairPlan:
    proposed_action: str
    confidence: float
    explanation: str


class AegisConsultant:
    """
    Aegis_Consultant (V1 - LOCKED)
    Proposes repair plans based on detected schema deltas.
    """

    def propose_repairs(
        self,
        schema_delta: SchemaDelta,
        observed_schema: ObservedSchema,
        gold_schema: ObservedSchema,
    ) -> List[RepairPlan]:
        """
        Analyze schema deltas and propose possible repair plans.

        This method proposes repairs only.
        It does not apply changes or modify data.
        """
        plans: List[RepairPlan] = []

        # Rule 1: Single missing column + single new column -> possible rename
        if (
            len(schema_delta.missing_columns) == 1
            and len(schema_delta.new_columns) == 1
        ):
            missing = schema_delta.missing_columns[0]
            new = schema_delta.new_columns[0]

            observed_type = observed_schema.columns[new].dtype
            gold_type = gold_schema.columns[missing].dtype

            if observed_type == gold_type:
                plans.append(
                    RepairPlan(
                        proposed_action=f"RENAME_COLUMN {new} -> {missing}",
                        confidence=0.85,
                        explanation=(
                            f"Column '{missing}' is missing and column '{new}' "
                            f"appeared with the same data type ({observed_type})."
                        ),
                    )
                )

        return plans
