from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional, Tuple

import pandas as pd

from src.governance.conversion_safety import (
    InvalidCastActionError,
    ParsedCastAction,
    build_conversion_metadata,
    parse_cast_action,
)
from src.governance.manifest import ConversionOutcomeMetadata, HealingManifest
from src.type_repair import (
    ColumnConversionResult,
    ConversionStatus,
    analyze_and_convert,
)


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
    Applies an explicitly approved repair plan to a controlled working
    dataset.

    Phase 3.1.4 retains the verified CAST_COLUMN path: sandbox casts now delegate to the
    verified type-repair engine, destructive WITH_DROP_INVALID plans are
    refused, and live casts remain blocked. RENAME_COLUMN behavior is kept
    unchanged.
    """

    _SURGEON_VERSION = "1.9"
    _LIVE_CAST_BLOCK_MESSAGE = (
        "Verified CAST_COLUMN execution is sandbox-only; live casting "
        "remains blocked until Phase 3.1.6."
    )
    _DROP_INVALID_BLOCK_MESSAGE = (
        "WITH_DROP_INVALID is forbidden by the verified type-repair "
        "contract; row-dropping is not a type conversion."
    )

    @staticmethod
    def _parse_cast_action(action: str) -> Optional[ParsedCastAction]:
        """Compatibility wrapper around the shared governance parser."""
        try:
            return parse_cast_action(action)
        except InvalidCastActionError:
            return None

    @staticmethod
    def _conversion_metadata(
        column: str,
        result: ColumnConversionResult,
    ) -> ConversionOutcomeMetadata:
        return build_conversion_metadata(column, result)

    @staticmethod
    def _conversion_summary(
        column: str,
        result: ColumnConversionResult,
    ) -> str:
        # Keep the summary deterministic and redacted. No row indexes or raw
        # source values are included. Preserve first-seen reason-code order
        # while removing duplicates, so repeated row failures do not create
        # noisy or input-size-dependent messages.
        reason_codes = tuple(
            dict.fromkeys(
                diagnostic.reason_code.value
                for diagnostic in result.diagnostics
            )
        )
        reason_text = ",".join(reason_codes) if reason_codes else "NONE"
        return (
            f"Verified cast {result.status.value}: "
            f"column={column}; target={result.target_dtype}; "
            f"converted={result.converted_count}; "
            f"failed={result.failed_count}; nulls={result.null_count}; "
            f"reasons={reason_text}; policy={result.policy_version}."
        )

    @staticmethod
    def _safe_conversion_is_applicable(
        source_series: pd.Series,
        result: ColumnConversionResult,
    ) -> bool:
        """Defence-in-depth check before Surgeon assigns a SAFE result."""
        if type(result) is not ColumnConversionResult:
            return False
        if result.status is not ConversionStatus.SAFE:
            return False
        if result.failed_count != 0 or result.diagnostics:
            return False

        converted = result.converted_series
        expected_dtypes = {
            "int64": "int64",
            "float64": "float64",
            "bool": "bool",
            "object": "object",
        }
        expected_dtype = expected_dtypes.get(result.target_dtype)
        if expected_dtype is None:
            return False

        return (
            type(converted) is pd.Series
            and result.total_count == len(source_series)
            and len(converted) == len(source_series)
            and converted.index.equals(source_series.index)
            and converted.name == source_series.name
            and str(converted.dtype) == expected_dtype
        )

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
            if ratio < 0.10:
                return "MEDIUM_RISK"
            if ratio < 0.50:
                return "HIGH_RISK"
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
        working_df = (
            target_dataset.copy(deep=True)
            if execution_mode == "sandbox"
            else target_dataset
        )

        applied = False
        validation_success = False
        validation_message = "No operation performed."
        conversion_outcome = None
        rollback_df = working_df.copy(deep=True)

        try:
            action = repair_plan.proposed_action

            if type(action) is str and action.startswith("RENAME_COLUMN"):
                parts = action.split()
                if len(parts) == 4 and parts[2] == "->":
                    old_col = parts[1]
                    new_col = parts[3]

                    if old_col in working_df.columns:
                        working_df.rename(
                            columns={old_col: new_col},
                            inplace=True,
                        )
                        applied = True
                    else:
                        validation_message = (
                            f"Rename source column not found: {old_col}."
                        )
                else:
                    validation_message = "Invalid RENAME_COLUMN action."

            elif type(action) is str and action.startswith("CAST_COLUMN"):
                cast_action = self._parse_cast_action(action)

                if cast_action is None:
                    validation_message = "Invalid CAST_COLUMN action."
                elif execution_mode != "sandbox":
                    # Explicit defence in depth: the live API already blocks
                    # CAST_COLUMN tickets, but Surgeon itself must enforce the
                    # Phase 3.1.3 boundary too.
                    validation_message = self._LIVE_CAST_BLOCK_MESSAGE
                elif cast_action.drop_invalid_requested:
                    validation_message = self._DROP_INVALID_BLOCK_MESSAGE
                elif cast_action.column not in working_df.columns:
                    validation_message = (
                        f"Cast source column not found: {cast_action.column}."
                    )
                else:
                    conversion_result = analyze_and_convert(
                        working_df[cast_action.column],
                        cast_action.target_dtype,
                    )
                    conversion_outcome = self._conversion_metadata(
                        cast_action.column,
                        conversion_result,
                    )
                    validation_message = self._conversion_summary(
                        cast_action.column,
                        conversion_result,
                    )

                    if conversion_result.status == ConversionStatus.SAFE:
                        source_series = working_df[cast_action.column]
                        if not self._safe_conversion_is_applicable(
                            source_series,
                            conversion_result,
                        ):
                            validation_message = (
                                "Execution error: invalid SAFE conversion result."
                            )
                            applied = False
                        else:
                            # Apply only after the complete series has passed.
                            # Use a second candidate copy so an unexpected pandas
                            # assignment failure cannot partially alter the current
                            # working dataset.
                            candidate_df = working_df.copy(deep=True)
                            candidate_df[cast_action.column] = (
                                conversion_result.converted_series.copy(deep=True)
                            )
                            working_df = candidate_df
                            applied = True
                    else:
                        # Atomic rejection: no converted series exists for a
                        # non-SAFE result, and the working copy remains exactly
                        # as it was before analysis.
                        applied = False

            else:
                validation_message = "Unsupported repair action."

            if applied:
                # Phase 3.1.3 deliberately retains the existing Surgeon
                # post-operation schema check. It validates column names/order;
                # broader complete-schema validation remains at the existing
                # API/live gates.
                validation_success = (
                    list(working_df.columns)
                    == list(gold_schema.column_order)
                )

                if conversion_outcome is not None:
                    if validation_success:
                        validation_message = (
                            f"{validation_message} Schema matches gold."
                        )
                    else:
                        validation_message = (
                            f"{validation_message} "
                            "Operation applied but schema mismatch."
                        )
                elif (
                    validation_success
                    and validation_message == "No operation performed."
                ):
                    validation_message = "Schema matches gold."
                elif not validation_success:
                    validation_message = (
                        "Operation applied but schema mismatch."
                    )
            else:
                validation_success = False

        except Exception as exc:
            # Sandbox execution is all-or-nothing even when an unexpected
            # exception occurs after a candidate transformation was built.
            # Restore the pristine sandbox snapshot before producing the
            # manifest. Live CAST_COLUMN never reaches mutation in this stage;
            # legacy live RENAME_COLUMN behavior remains unchanged.
            if execution_mode == "sandbox":
                working_df = rollback_df
            validation_success = False
            validation_message = (
                f"Execution error: {type(exc).__name__}."
            )
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

        component_versions = {
            "inspector": "1.0",
            "consultant": "1.2",
            "surgeon": self._SURGEON_VERSION,
        }
        if conversion_outcome is not None:
            component_versions["type_repair"] = (
                conversion_outcome.policy_version
            )

        manifest = HealingManifest(
            timestamp=datetime.now(UTC).isoformat(),
            repair_plan=repair_plan,
            execution_result=execution_result,
            execution_mode=execution_mode,
            operator=operator,
            component_versions=component_versions,
            original_row_count=original_row_count,
            final_row_count=final_row_count,
            integrity_status=integrity_status,
            risk_level=risk_level,
            corrected_dataset=working_df.copy(deep=True),
            conversion_outcome=conversion_outcome,
        )

        return execution_result, manifest
