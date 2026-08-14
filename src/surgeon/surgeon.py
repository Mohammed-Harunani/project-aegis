from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable, Optional, Tuple

import pandas as pd

from src.column_order import (
    InvalidColumnOrderActionError,
    analyze_reorder_only_eligibility,
    parse_column_order_action,
)
from src.governance.conversion_safety import (
    InvalidCastActionError,
    ParsedCastAction,
    build_conversion_metadata,
    parse_cast_action,
)
from src.governance.manifest import ConversionOutcomeMetadata, HealingManifest
from src.inspector import ColumnStats, ObservedSchema, SchemaDelta
from src.live_execution.cast_allowlist import (
    LiveCastAllowlistError,
    LiveCastPair,
    require_live_cast_pair_allowed,
)
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

    Phase 3.1.6 retains the verified sandbox CAST_COLUMN path and permits a
    live cast only when its trusted logical source-target pair is explicitly
    allowlisted by the caller. Destructive WITH_DROP_INVALID plans remain
    refused. Phase 3.3.5 permits verified REORDER_COLUMNS execution in live
    mode only when the caller grants the narrow one-call capability after
    fresh source revalidation. RENAME_COLUMN behavior is unchanged.
    """

    _SURGEON_VERSION = "2.2"
    _COLUMN_ORDER_POLICY_VERSION = "phase3.3-policy-v1"
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

    @staticmethod
    def _derive_schema_delta(observed_schema, gold_schema) -> SchemaDelta:
        """Build the complete delta used by the independent order proof."""

        try:
            observed_order = observed_schema.column_order
            gold_order = gold_schema.column_order
        except (AttributeError, TypeError):
            return SchemaDelta([], [], {}, True)

        # The policy layer owns validation of malformed and duplicate orders.
        # Supply a neutral delta so those stable reason codes are not hidden by
        # unsafe set/dict operations here.
        if (
            type(observed_order) not in (list, tuple)
            or type(gold_order) not in (list, tuple)
            or any(type(name) is not str for name in observed_order)
            or any(type(name) is not str for name in gold_order)
            or len(set(observed_order)) != len(observed_order)
            or len(set(gold_order)) != len(gold_order)
        ):
            return SchemaDelta([], [], {}, True)

        observed_names = set(observed_order)
        gold_names = set(gold_order)
        missing_columns = sorted(gold_names - observed_names)
        new_columns = sorted(observed_names - gold_names)
        type_mismatches = {}

        try:
            for name in observed_names.intersection(gold_names):
                observed_dtype = observed_schema.columns[name].dtype
                gold_dtype = gold_schema.columns[name].dtype
                if observed_dtype != gold_dtype:
                    type_mismatches[name] = {
                        "observed": observed_dtype,
                        "gold": gold_dtype,
                    }
        except Exception:
            # Leave the summary empty. The policy independently inspects both
            # schema mappings and will fail closed with its metadata reason.
            type_mismatches = {}

        return SchemaDelta(
            missing_columns=missing_columns,
            new_columns=new_columns,
            type_mismatches=type_mismatches,
            reorder_event=tuple(observed_order) != tuple(gold_order),
        )

    @classmethod
    def _order_eligibility(cls, observed_schema, gold_schema):
        return analyze_reorder_only_eligibility(
            cls._derive_schema_delta(observed_schema, gold_schema),
            observed_schema,
            gold_schema,
        )

    @staticmethod
    def _schema_matches_dataset(
        provided_schema,
        actual_schema,
        *,
        trust_logical_dtypes: bool = False,
    ) -> bool:
        """Prove the supplied observed metadata describes this dataset."""

        try:
            if tuple(provided_schema.column_order) != tuple(
                actual_schema.column_order
            ):
                return False
            if set(provided_schema.columns) != set(actual_schema.columns):
                return False
            for name in actual_schema.column_order:
                provided = provided_schema.columns[name]
                actual = actual_schema.columns[name]
                if provided.null_count != actual.null_count:
                    return False
                if (
                    not trust_logical_dtypes
                    and provided.dtype != actual.dtype
                ):
                    return False
        except Exception:
            return False
        return True

    @staticmethod
    def _dataset_schema(dataframe: pd.DataFrame) -> ObservedSchema:
        """Observe only execution-critical metadata without hashing values."""

        columns = {
            name: ColumnStats(
                null_count=int(dataframe[name].isna().sum()),
                # Eligibility never consumes unique_count. Avoid pandas
                # nunique() here because valid nested objects are unhashable.
                unique_count=0,
                dtype=str(dataframe[name].dtype),
            )
            for name in dataframe.columns
        }
        return ObservedSchema(
            columns=columns,
            column_order=list(dataframe.columns),
        )

    @staticmethod
    def _validate_reorder_candidate(
        pristine: pd.DataFrame,
        candidate: pd.DataFrame,
        target_order: Tuple[str, ...],
    ) -> Optional[str]:
        """Return a redacted reason code, or None after the full proof."""

        if tuple(candidate.columns) != target_order:
            return "TARGET_ORDER_MISMATCH"
        if len(candidate.columns) != len(pristine.columns):
            return "COLUMN_COUNT_MISMATCH"
        if set(candidate.columns) != set(pristine.columns):
            return "COLUMN_MEMBERSHIP_MISMATCH"
        if len(candidate) != len(pristine):
            return "ROW_COUNT_MISMATCH"
        if type(candidate.index) is not type(pristine.index):
            return "INDEX_TYPE_MISMATCH"
        if not candidate.index.equals(pristine.index):
            return "INDEX_VALUE_OR_ORDER_MISMATCH"
        if candidate.index.names != pristine.index.names:
            return "INDEX_NAME_MISMATCH"
        if str(candidate.index.dtype) != str(pristine.index.dtype):
            return "INDEX_DTYPE_MISMATCH"

        try:
            for name in pristine.columns:
                if str(candidate[name].dtype) != str(pristine[name].dtype):
                    return "COLUMN_DTYPE_MISMATCH"
                pd.testing.assert_series_equal(
                    candidate[name],
                    pristine[name],
                    check_dtype=True,
                    check_index_type=True,
                    check_series_type=True,
                    check_names=True,
                    check_exact=True,
                    check_categorical=True,
                )

            restored = candidate.loc[:, list(pristine.columns)]
            pd.testing.assert_frame_equal(
                restored,
                pristine,
                check_dtype=True,
                check_index_type=True,
                check_column_type=True,
                check_frame_type=True,
                check_names=True,
                check_exact=True,
                check_categorical=True,
                check_like=False,
            )
        except (AssertionError, TypeError, ValueError):
            return "VALUE_OR_NULL_MISMATCH"

        return None

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
        allowed_live_cast_pairs: Optional[Iterable[LiveCastPair]] = None,
        trusted_observed_schema: bool = False,
        allow_live_column_order: bool = False,
    ) -> Tuple[ExecutionResult, HealingManifest]:

        if allowed_modes is None:
            allowed_modes = ["sandbox"]

        if execution_mode not in allowed_modes:
            raise ValueError("Execution mode not allowed.")
        if type(trusted_observed_schema) is not bool:
            raise ValueError("trusted_observed_schema must be a bool.")
        if type(allow_live_column_order) is not bool:
            raise ValueError("allow_live_column_order must be a bool.")
        if allow_live_column_order and execution_mode != "live":
            raise ValueError(
                "allow_live_column_order is valid only for live execution."
            )

        original_row_count = len(target_dataset)
        source_snapshot = target_dataset.copy(deep=True)
        working_df = (
            target_dataset.copy(deep=True)
            if execution_mode == "sandbox"
            else target_dataset
        )

        applied = False
        validation_success = False
        validation_message = "No operation performed."
        conversion_outcome = None
        order_action_attempted = False
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
                elif cast_action.drop_invalid_requested:
                    validation_message = self._DROP_INVALID_BLOCK_MESSAGE
                elif cast_action.column not in working_df.columns:
                    validation_message = (
                        f"Cast source column not found: {cast_action.column}."
                    )
                else:
                    conversion_result = None
                    live_pair_allowed = True
                    if execution_mode == "live":
                        try:
                            require_live_cast_pair_allowed(
                                repair_plan,
                                observed_schema,
                                allowed_live_cast_pairs,
                            )
                        except LiveCastAllowlistError as exc:
                            validation_message = str(exc)
                            live_pair_allowed = False
                    if live_pair_allowed:
                        conversion_result = analyze_and_convert(
                            working_df[cast_action.column],
                            cast_action.target_dtype,
                        )

                    if conversion_result is None:
                        applied = False
                    else:
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

            elif type(action) is str and action.startswith("REORDER_COLUMNS"):
                order_action_attempted = True
                try:
                    order_action = parse_column_order_action(action)
                except InvalidColumnOrderActionError:
                    validation_message = "Invalid REORDER_COLUMNS action."
                    order_action = None

                if order_action is None:
                    applied = False
                elif execution_mode != "sandbox" and not (
                    execution_mode == "live"
                    and allow_live_column_order
                ):
                    # A generic live call cannot enable order repair. The API
                    # grants this one-call capability only after it has
                    # reread the complete source and independently re-proved
                    # reorder-only eligibility against persisted Gold.
                    validation_message = (
                        "REORDER_COLUMNS is not enabled for live execution."
                    )
                elif type(working_df) is not pd.DataFrame:
                    validation_message = (
                        "Column-order repair requires a pandas DataFrame."
                    )
                else:
                    supplied_eligibility = self._order_eligibility(
                        observed_schema,
                        gold_schema,
                    )
                    if not supplied_eligibility.eligible:
                        validation_message = (
                            "Column-order repair is ineligible: "
                            f"{supplied_eligibility.reason_code.value}."
                        )
                    elif order_action.target_order != supplied_eligibility.gold_order:
                        validation_message = (
                            "Column-order action does not match the Gold schema."
                        )
                    else:
                        raw_columns = tuple(working_df.columns)
                        if (
                            any(type(name) is not str for name in raw_columns)
                            or len(set(raw_columns)) != len(raw_columns)
                        ):
                            validation_message = (
                                "Target dataset has invalid or duplicate columns."
                            )
                        else:
                            actual_schema = self._dataset_schema(working_df)
                            if not self._schema_matches_dataset(
                                observed_schema,
                                actual_schema,
                                trust_logical_dtypes=trusted_observed_schema,
                            ):
                                validation_message = (
                                    "Observed schema does not match the target dataset."
                                )
                            else:
                                eligibility_schema = (
                                    observed_schema
                                    if trusted_observed_schema
                                    else actual_schema
                                )
                                actual_eligibility = self._order_eligibility(
                                    eligibility_schema,
                                    gold_schema,
                                )
                                if not actual_eligibility.eligible:
                                    validation_message = (
                                        "Target dataset is not reorder-only: "
                                        f"{actual_eligibility.reason_code.value}."
                                    )
                                else:
                                    candidate_df = working_df.loc[
                                        :, list(order_action.target_order)
                                    ].copy(deep=True)
                                    preservation_failure = (
                                        self._validate_reorder_candidate(
                                            rollback_df,
                                            candidate_df,
                                            order_action.target_order,
                                        )
                                    )
                                    source_unchanged_failure = (
                                        self._validate_reorder_candidate(
                                            source_snapshot,
                                            target_dataset,
                                            tuple(source_snapshot.columns),
                                        )
                                    )
                                    if preservation_failure is not None:
                                        validation_message = (
                                            "Column-order preservation failed: "
                                            f"{preservation_failure}."
                                        )
                                    elif source_unchanged_failure is not None:
                                        validation_message = (
                                            "Column-order source isolation failed: "
                                            f"{source_unchanged_failure}."
                                        )
                                    else:
                                        working_df = candidate_df
                                        applied = True
                                        validation_message = (
                                            "Verified column-order repair preserved rows, "
                                            "index, dtypes, values, and nulls."
                                        )

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
            # manifest. Live CAST_COLUMN and REORDER_COLUMNS build candidate
            # copies rather than mutating the caller's source; legacy live
            # RENAME_COLUMN behavior remains unchanged.
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
        if order_action_attempted:
            component_versions["column_order"] = (
                self._COLUMN_ORDER_POLICY_VERSION
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
