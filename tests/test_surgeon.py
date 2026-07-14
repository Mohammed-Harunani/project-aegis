import pandas as pd

from inspector import AegisInspector
from consultant.consultant import AegisConsultant
from surgeon.surgeon import AegisSurgeon


def test_surgeon_emits_healing_manifest():
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
    surgeon = AegisSurgeon()

    gold_schema = inspector.generate_observed_schema(df_gold)
    observed_schema = inspector.generate_observed_schema(df_new)

    delta = inspector.detect_delta(observed_schema, gold_schema)

    plans = consultant.propose_repairs(
        schema_delta=delta,
        observed_schema=observed_schema,
        gold_schema=gold_schema,
    )

    assert len(plans) == 1

    result, manifest = surgeon.execute(
        repair_plan=plans[0],
        observed_schema=observed_schema,
        gold_schema=gold_schema,
        target_dataset=df_new,
        operator="test_user",
    )

    # Execution assertions
    assert result.applied is True
    assert result.validation.success is True

    # HealingManifest assertions
    assert manifest.repair_plan.proposed_action == "RENAME_COLUMN new_name -> old_name"
    assert manifest.execution_mode == "sandbox"
    assert manifest.operator == "test_user"
    assert manifest.component_versions["inspector"] == "1.0"
    assert manifest.component_versions["consultant"] == "1.2"
    assert manifest.component_versions["surgeon"] == "1.7"
