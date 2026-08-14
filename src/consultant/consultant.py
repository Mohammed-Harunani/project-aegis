from dataclasses import dataclass
from typing import List

from src.column_order import (
    COLUMN_ORDER_CONFIDENCE,
    analyze_reorder_only_eligibility,
    serialize_column_order_action,
)


@dataclass(frozen=True)
class RepairPlan:
    proposed_action: str
    confidence: float
    explanation: str


class AegisConsultant:
    """
    Aegis_Consultant
    Proposes repair strategies based on detected schema delta.
    """

    def propose_repairs(
        self,
        schema_delta,
        observed_schema,
        gold_schema,
    ) -> List[RepairPlan]:

        repair_plans = []

        # ----------------------------
        # Verified Column Order Repair
        # ----------------------------
        # Phase 3.3.2 does not trust reorder_event alone. The pure
        # eligibility proof independently rejects missing/new columns,
        # type mismatches, duplicate/invalid labels, unequal membership,
        # inconsistent schema metadata, and already-correct order.
        order_eligibility = analyze_reorder_only_eligibility(
            schema_delta,
            observed_schema,
            gold_schema,
        )
        if order_eligibility.eligible:
            repair_plans.append(
                RepairPlan(
                    proposed_action=serialize_column_order_action(
                        order_eligibility.gold_order
                    ),
                    confidence=COLUMN_ORDER_CONFIDENCE,
                    explanation=(
                        "Identical columns and dtypes detected in a different "
                        "order. Exact Gold schema order proposed."
                    ),
                )
            )

        # ----------------------------
        # Rename Detection
        # ----------------------------
        if (
            len(schema_delta.missing_columns) == 1
            and len(schema_delta.new_columns) == 1
        ):
            missing_col = schema_delta.missing_columns[0]
            new_col = schema_delta.new_columns[0]

            if new_col in observed_schema.columns and missing_col in gold_schema.columns:
                observed_type = observed_schema.columns[new_col].dtype
                gold_type = gold_schema.columns[missing_col].dtype

                if observed_type == gold_type:
                    repair_plans.append(
                        RepairPlan(
                            proposed_action=f"RENAME_COLUMN {new_col} -> {missing_col}",
                            confidence=0.85,
                            explanation="Column rename detected with matching types.",
                        )
                    )

        # ----------------------------
        # Type Mismatch Handling
        # ----------------------------
        for column, mismatch in schema_delta.type_mismatches.items():
            gold_type = mismatch["gold"]

            # Strict cast
            repair_plans.append(
                RepairPlan(
                    proposed_action=f"CAST_COLUMN {column} TO {gold_type}",
                    confidence=0.85,
                    explanation="Type mismatch detected. Strict cast proposed.",
                )
            )

            # Cast + drop invalid rows
            repair_plans.append(
                RepairPlan(
                    proposed_action=f"CAST_COLUMN {column} TO {gold_type} WITH_DROP_INVALID",
                    confidence=0.70,
                    explanation="Type mismatch with invalid values. Drop invalid rows before cast.",
                )
            )

        return repair_plans