import pandas as pd
from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class ColumnStats:
    null_count: int
    unique_count: int
    dtype: str


@dataclass(frozen=True)
class ObservedSchema:
    columns: Dict[str, ColumnStats]
    column_order: List[str]


@dataclass(frozen=True)
class SchemaDelta:
    missing_columns: List[str]
    new_columns: List[str]
    type_mismatches: Dict[str, Dict[str, str]]
    reorder_event: bool


class AegisInspector:
    """
    Aegis_Inspector (V1 - LOCKED)
    Responsible for observing schema and detecting schema-level deltas.
    """

    def generate_observed_schema(self, df: pd.DataFrame) -> ObservedSchema:
        columns: Dict[str, ColumnStats] = {}
        column_order = list(df.columns)

        for col in df.columns:
            series = df[col]
            stats = ColumnStats(
                null_count=int(series.isna().sum()),
                unique_count=int(series.nunique(dropna=True)),
                dtype=str(series.dtype),
            )
            columns[col] = stats

        return ObservedSchema(
            columns=columns,
            column_order=column_order,
        )

    def detect_delta(
        self,
        observed_schema: ObservedSchema,
        gold_schema: ObservedSchema,
    ) -> SchemaDelta:

        observed_cols = set(observed_schema.columns.keys())
        gold_cols = set(gold_schema.columns.keys())

        missing_columns = sorted(gold_cols - observed_cols)
        new_columns = sorted(observed_cols - gold_cols)

        type_mismatches: Dict[str, Dict[str, str]] = {}
        for col in observed_cols.intersection(gold_cols):
            observed_type = observed_schema.columns[col].dtype
            gold_type = gold_schema.columns[col].dtype
            if observed_type != gold_type:
                type_mismatches[col] = {
                    "observed": observed_type,
                    "gold": gold_type,
                }

        reorder_event = observed_schema.column_order != gold_schema.column_order

        return SchemaDelta(
            missing_columns=missing_columns,
            new_columns=new_columns,
            type_mismatches=type_mismatches,
            reorder_event=reorder_event,
        )
