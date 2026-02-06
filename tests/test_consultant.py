import pandas as pd

from inspector import AegisInspector
from consultant.consultant import AegisConsultant



def test_single_column_rename_proposal():
    df_gold = pd.DataFrame(
        {
            "old_name": [1, 2, 3],
        }
    )

    df_new = pd.DataFrame(
        {
            "new_name": [1, 2, 3],
        }
    )

    inspector = AegisInspector()
    consultant = AegisConsultant()

    gold_schema = inspector.generate_observed_schema(df_gold)
    observed_schema = inspector.generate_observed_schema(df_new)

    delta = inspector.detect_delta(observed_schema, gold_schema)

    plans = consultant.propose_repairs(
        schema_delta=delta,
        observed_schema=observed_schema,
        gold_schema=gold_schema,
    )

    assert len(plans) == 1
    assert plans[0].proposed_action == "RENAME_COLUMN new_name -> old_name"
    assert plans[0].confidence == 0.85
