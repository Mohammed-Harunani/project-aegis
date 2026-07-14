import json
import logging
from pathlib import Path
import pandas as pd

from src.inspector import AegisInspector
from src.consultant import AegisConsultant
from src.surgeon import AegisSurgeon


def load_config():
    with open("config/governance_config.json", "r") as f:
        return json.load(f)


def setup_logging(log_level):
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def persist_manifest(manifest):
    manifest_dir = Path("manifests")
    manifest_dir.mkdir(exist_ok=True)

    file_path = manifest_dir / f"manifest_{manifest.timestamp.replace(':', '-')}.json"

    with open(file_path, "w") as f:
        json.dump(manifest.__dict__, f, indent=4, default=str)

    logging.info(f"Manifest persisted to: {file_path}")


def decide(plan, risk_level, config):
    auto_threshold = config["auto_apply_threshold"]
    approval_threshold = config["approval_threshold"]

    if risk_level == "CRITICAL_RISK":
        return "HARD_BLOCK"

    if plan.confidence >= auto_threshold and risk_level != "CRITICAL_RISK":
        return "AUTO_APPLY"

    if plan.confidence >= approval_threshold and risk_level in ["LOW_RISK", "MEDIUM_RISK"]:
        return "AUTO_APPLY"

    if risk_level == "HIGH_RISK":
        return "QUARANTINE"

    return "QUARANTINE"


def main():
    print("\n=== Project Aegis Orchestrator (Phase 6 – Non-Interactive Mode) ===\n")

    config = load_config()
    setup_logging(config["log_level"])

    inspector = AegisInspector()
    consultant = AegisConsultant()
    surgeon = AegisSurgeon()

    gold_df = pd.DataFrame({
        "customer_id": [1, 2, 3],
        "amount": [100, 200, 300]
    })

    broken_df = pd.DataFrame({
        "customer_id": ["1", "2", "abc"],
        "amount": [100, 200, 300]
    })

    gold_schema = inspector.generate_observed_schema(gold_df)
    observed_schema = inspector.generate_observed_schema(broken_df)
    delta = inspector.detect_delta(observed_schema, gold_schema)

    logging.info(f"SchemaDelta detected: {delta}")

    plans = consultant.propose_repairs(delta, observed_schema, gold_schema)

    if not plans:
        logging.info("No repair required.")
        return

    plans = sorted(plans, key=lambda p: p.confidence, reverse=True)
    plan = plans[0]

    logging.info(f"Proposed Repair Plan: {plan.proposed_action}")
    logging.info(f"Confidence: {plan.confidence}")

    # Execute in sandbox first to compute risk
    execution_result, healing_manifest = surgeon.execute(
        repair_plan=plan,
        observed_schema=observed_schema,
        gold_schema=gold_schema,
        target_dataset=broken_df,
        operator="system",
        execution_mode="sandbox",
        allowed_modes=config["allowed_execution_modes"],
    )

    risk_level = healing_manifest.risk_level

    decision = decide(plan, risk_level, config)

    logging.info(f"Risk Level: {risk_level}")
    logging.info(f"Decision: {decision}")

    if decision != "AUTO_APPLY":
        logging.warning("Execution halted by governance policy.")
        persist_manifest(healing_manifest)
        return

    logging.info("Auto-applying repair.")

    persist_manifest(healing_manifest)

    print("\n=== Orchestration Complete ===\n")


if __name__ == "__main__":
    main()