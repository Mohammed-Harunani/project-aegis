from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone

import pandas as pd

from consultant.consultant import RepairPlan
from inspector import ObservedSchema
from governance.manifest import HealingManifest


@dataclass(frozen=True)
class ValidationResult:
    success: bool
    message: str


@dataclass(frozen=True)
class ExecutionResult:
    applied: bool
    validation: ValidationResult


class AegisSurgeon:
    """
    Aegis_Surgeon (V1 - LOCKED)
    Executes approved repair plans in a controlled and auditable manner.
    """

    def execute(
        self,
        repair_plan: RepairPlan,
        observed_schema: ObservedSchema,
        gold_schema: ObservedSchema,
        target_dataset: pd.DataFrame,
        operator: Optional[str] = None,
    ) -> tuple[ExecutionResult, HealingManifest]:
        """
        Execute an approved repair plan in a sandboxed environment.
        """

        # Safety: work on a copy only
        sandbox_df = target_dataset.copy()
        action = repair_plan.proposed_action

        if action.startswith("RENAME_COLUMN"):
            try:
                _, mapping = action.split("RENAME_COLUMN", 1)
                source, target = mapping.strip().split("->")
                source = source.strip()
                target = target.strip()

                if source not in sandbox_df.columns:
                    result = ExecutionResult(
                        applied=False,
                        validation=ValidationResult(
                            success=False,
                            message=f"Source column '{source}' not found.",
                        ),
                    )
                else:
                    sandbox_df = sandbox_df.rename(columns={source: target})

                    if target not in sandbox_df.columns:
                        result = ExecutionResult(
                            applied=False,
                            validation=ValidationResult(
                                success=False,
                                message="Rename validation failed.",
                            ),
                        )
                    else:
                        result = ExecutionResult(
                            applied=True,
                            validation=ValidationResult(
                                success=True,
                                message="Rename executed successfully in sandbox.",
                            ),
                        )

            except Exception as e:
                result = ExecutionResult(
                    applied=False,
                    validation=ValidationResult(
                        success=False,
                        message=str(e),
                    ),
                )
        else:
            result = ExecutionResult(
                applied=False,
                validation=ValidationResult(
                    success=False,
                    message="Unsupported repair action.",
                ),
            )

        manifest = HealingManifest(
            timestamp=datetime.now(timezone.utc).isoformat(),
            repair_plan=repair_plan.proposed_action,
            execution_result="applied" if result.applied else "not_applied",
            validation_result=result.validation.message,
            inspector_version="V1",
            consultant_version="V1",
            surgeon_version="V1",
            execution_mode="sandbox",
            operator=operator,
        )

        return result, manifest
