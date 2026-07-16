from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Tuple
import pandas as pd

from src.governance.manifest import HealingManifest


@dataclass(frozen=True)
class ValidationResult:
    success: bool
    message: str


@dataclass(frozen=True)
class ExecutionResult:
    applied: bool
    validation: ValidationResult


class AegisSurgeon:

    def _safe_cast_diagnostics(self, series: pd.Series, target_type: str):
        failed_rows = []
        failed_values = []

        for idx, value in series.items():
            try:
                pd.Series([value]).astype(target_type)
            except Exception:
                failed_rows.append(idx)
                failed_values.append(value)

        return failed_rows, failed_values

    def _compute_risk(
        self,
        execution_result: ExecutionResult,
        original_row_count: int,
        final_row_count: int,
        integrity_status: str,
    ) -> str:

        if not execution_result.applied:
            return "HIGH_RISK"

        if integrity_status == "DATA_LOSS_EVENT":
            row_delta = original_row_count - final_row_count
            ratio = row_delta / original_row_count if original_row_count > 0 else 0

            if ratio < 0.01:
                return "LOW_RISK"
            elif ratio < 0.10:
                return "MEDIUM_RISK"
            elif ratio < 0.50:
                return "HIGH_RISK"
            else:
                return "CRITICAL_RISK"

        if integrity_status == "DATA_GAIN_EVENT":
            return "MEDIUM_RISK"

        return "LOW_RISK"

    def execute(
        self,
        repair_plan,
        observed_schema,
        gold_schema,
        target_dataset: pd.DataFrame,
        operator: str,
        execution_mode: str = "sandbox",
        allowed_modes=None,
    ) -> Tuple[ExecutionResult, HealingManifest]:

        if allowed_modes is None:
            allowed_modes = ["sandbox"]

        if execution_mode not in allowed_modes:
            raise ValueError("Execution mode not allowed.")

        original_row_count = len(target_dataset)
        working_df = target_dataset.copy() if execution_mode == "sandbox" else target_dataset

        applied = False
        validation_success = False
        validation_message = "No operation performed."

        try:
            action = repair_plan.proposed_action

            if action.startswith("RENAME_COLUMN"):
                parts = action.split()
                old_col = parts[1]
                new_col = parts[3]

                if old_col in working_df.columns:
                    working_df.rename(columns={old_col: new_col}, inplace=True)
                    applied = True

            elif action.startswith("CAST_COLUMN"):
                parts = action.split()
                column = parts[1]
                target_type = parts[3]
                drop_invalid = "WITH_DROP_INVALID" in action

                if column in working_df.columns:
                    failed_rows, failed_values = self._safe_cast_diagnostics(
                        working_df[column], target_type
                    )

                    if failed_rows and not drop_invalid:
                        validation_message = (
                            f"Unsafe cast detected. "
                            f"{len(failed_rows)} invalid values found. "
                            f"Rows: {failed_rows}. "
                            f"Values: {failed_values}"
                        )
                        applied = False
                    else:
                        if failed_rows and drop_invalid:
                            working_df = working_df.drop(index=failed_rows)

                        working_df[column] = working_df[column].astype(target_type)
                        applied = True

                        if failed_rows and drop_invalid:
                            validation_message = (
                                f"Dropped {len(failed_rows)} invalid rows before cast."
                            )

            if applied:
                validation_success = list(working_df.columns) == list(gold_schema.column_order)

                if validation_success and validation_message == "No operation performed.":
                    validation_message = "Schema matches gold."
                elif not validation_success:
                    validation_message = "Operation applied but schema mismatch."

            else:
                validation_success = False

        except Exception as e:
            validation_success = False
            validation_message = f"Execution error: {str(e)}"
            applied = False

        final_row_count = len(working_df)

        if final_row_count < original_row_count:
            integrity_status = "DATA_LOSS_EVENT"
        elif final_row_count > original_row_count:
            integrity_status = "DATA_GAIN_EVENT"
        else:
            integrity_status = "NO_VOLUME_CHANGE"

        validation = ValidationResult(
            success=validation_success,
            message=validation_message,
        )

        execution_result = ExecutionResult(
            applied=applied,
            validation=validation,
        )

        risk_level = self._compute_risk(
            execution_result,
            original_row_count,
            final_row_count,
            integrity_status,
        )

        manifest = HealingManifest(
            timestamp=datetime.now(UTC).isoformat(),
            repair_plan=repair_plan,
            execution_result=execution_result,
            execution_mode=execution_mode,
            operator=operator,
            component_versions={
                "inspector": "1.0",
                "consultant": "1.2",
                "surgeon": "1.7",
            },
            original_row_count=original_row_count,
            final_row_count=final_row_count,
            integrity_status=integrity_status,
            risk_level=risk_level,
            corrected_dataset=working_df.copy(),
        )

        return execution_result, manifest