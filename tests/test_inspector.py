import pandas as pd
from src.inspector import AegisInspector


def test_detect_schema_delta():
    df_gold = pd.DataFrame(
        {
            "id": [1, 2, 3],
            "value": [10, 20, 30],
        }
    )

    df_new = pd.DataFrame(
        {
            "value": [10, 20, 30],
            "id": [1, 2, 3],
            "extra": ["a", "b", "c"],
        }
    )

    inspector = AegisInspector()

    gold_schema = inspector.generate_observed_schema(df_gold)
    observed_schema = inspector.generate_observed_schema(df_new)

    delta = inspector.detect_delta(observed_schema, gold_schema)

    assert delta.missing_columns == []
    assert delta.new_columns == ["extra"]
    assert delta.type_mismatches == {}
    assert delta.reorder_event is True
