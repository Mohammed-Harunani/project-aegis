"""PostgreSQL verification for immutable snapshots and ingestion runs."""

import datetime
import decimal
import sys
import threading
import uuid
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import get_verified_test_database_url


TEST_DATABASE_URL = get_verified_test_database_url()

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.consultant.consultant import RepairPlan
from src.db.models import (
    ApprovalTicketRecord,
    Base,
    DatasetSnapshotRecord,
    IngestionRunRecord,
    SourceDatasetRecord,
    SourceSystemRecord,
)
from src.governance.approval_repository import (
    ApprovalReplayError,
    PostgresApprovalRepository,
)
from src.governance.dataset_codec import encode_dataset
from src.identity.binding import build_endpoint_binding
from src.identity.repository import IdentityRepository
from src.ingestion.repository import (
    IngestionLineageError,
    IngestionRepository,
    InvalidIngestionRunError,
    compute_snapshot_provenance_fingerprint,
)
from src.ingestion.service import IngestionService
from src.inspector import ColumnStats, ObservedSchema
from src.live_execution.output_fingerprint import compute_dataframe_fingerprint


engine = create_engine(TEST_DATABASE_URL)
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def setup_module(_module):
    Base.metadata.create_all(bind=engine)


def teardown_module(_module):
    Base.metadata.drop_all(bind=engine)


def setup_function(_function):
    with engine.begin() as connection:
        connection.execute(ApprovalTicketRecord.__table__.delete())
        connection.execute(IngestionRunRecord.__table__.delete())
        connection.execute(DatasetSnapshotRecord.__table__.delete())
        connection.execute(SourceDatasetRecord.__table__.delete())
        connection.execute(SourceSystemRecord.__table__.delete())


def _identity(db, *, table="customers", database="erp"):
    identities = IdentityRepository(db)
    system = identities.resolve_source_system(
        f"{database}-source",
        build_endpoint_binding(
            f"postgresql://identity_user:secret@db.example.com/{database}"
        ),
    )
    return identities.resolve_source_dataset(
        system.source_system_id, "public", table
    )


def _frame():
    return pd.DataFrame(
        {
            "customer_id": [1, 2],
            "external_id": ["A-1", "A-2"],
            "amount": [decimal.Decimal("10.123456789"), decimal.Decimal("20.50")],
            "record_uuid": [
                uuid.UUID("11111111-1111-1111-1111-111111111111"),
                uuid.UUID("22222222-2222-2222-2222-222222222222"),
            ],
            "event_date": [
                datetime.date(2026, 8, 1),
                datetime.date(2026, 8, 2),
            ],
            "event_time": [
                datetime.datetime(2026, 8, 1, 10, tzinfo=datetime.UTC),
                datetime.datetime(2026, 8, 2, 11, tzinfo=datetime.UTC),
            ],
            "metadata": [
                {"__aegis_type__": "decimal", "value": "source-data"},
                [1, {"nested": True}],
            ],
            "ratio": [float("inf"), float("-inf")],
        },
        dtype=object,
        index=pd.Index([100, 200], name="source_row", dtype="int64"),
    )


def _metadata(dataframe):
    return [
        {"column_name": column, "data_type": "synthetic_test_type"}
        for column in dataframe.columns
    ]


def _source_read(dataframe=None, *, primary_key=None, schema_hash=None):
    dataframe = dataframe if dataframe is not None else _frame()
    return {
        "dataframe": dataframe,
        "primary_key": primary_key or ["customer_id"],
        "row_count": len(dataframe),
        "schema_fingerprint": schema_hash or "a" * 64,
        "dataset_fingerprint": compute_dataframe_fingerprint(dataframe),
        "column_metadata": _metadata(dataframe),
    }


def _capture(db, dataset_id, source_read=None, *, started_at=None):
    return IngestionService(IngestionRepository(db)).record_captured_simulation(
        source_dataset_id=dataset_id,
        source_read=source_read or _source_read(),
        started_at=started_at
        or datetime.datetime(2026, 8, 12, 12, tzinfo=datetime.UTC),
    )


def _schema():
    columns = {
        "customer_id": ColumnStats(null_count=0, unique_count=2, dtype="object")
    }
    return ObservedSchema(columns=columns, column_order=["customer_id"])


def _plan():
    return RepairPlan(
        proposed_action="RENAME_COLUMN:customer_id->customer_key",
        confidence=0.9,
        explanation="snapshot-backed approval test",
    )


def test_exact_codec_v2_snapshot_replay_preserves_values_order_index_and_dtypes():
    original = _frame()
    with TestSessionLocal() as db:
        dataset = _identity(db)
        captured = _capture(db, dataset.source_dataset_id, _source_read(original))
        db.commit()
        replayed = IngestionRepository(db).load_snapshot_dataframe(
            captured.snapshot.dataset_snapshot_id
        )

    pd.testing.assert_frame_equal(original, replayed, check_dtype=True)
    assert isinstance(replayed.loc[100, "amount"], decimal.Decimal)
    assert isinstance(replayed.loc[100, "record_uuid"], uuid.UUID)
    assert isinstance(replayed.loc[100, "event_date"], datetime.date)
    assert isinstance(replayed.loc[100, "event_time"], datetime.datetime)
    assert replayed.loc[100, "metadata"] == {
        "__aegis_type__": "decimal",
        "value": "source-data",
    }


def test_repeated_identical_simulations_create_two_runs_reusing_one_snapshot():
    with TestSessionLocal() as db:
        dataset = _identity(db)
        first = _capture(db, dataset.source_dataset_id)
        second = _capture(db, dataset.source_dataset_id)
        db.commit()

        assert first.snapshot.dataset_snapshot_id == second.snapshot.dataset_snapshot_id
        assert first.run.ingestion_run_id != second.run.ingestion_run_id
        assert db.query(DatasetSnapshotRecord).count() == 1
        assert db.query(IngestionRunRecord).count() == 2


def test_identical_content_in_different_datasets_never_shares_snapshot_identity():
    with TestSessionLocal() as db:
        first_dataset = _identity(db, table="customers")
        second_dataset = _identity(db, table="customers_archive")
        first = _capture(db, first_dataset.source_dataset_id)
        second = _capture(db, second_dataset.source_dataset_id)
        db.commit()

    assert first.snapshot.dataset_snapshot_id != second.snapshot.dataset_snapshot_id
    assert first.snapshot.provenance_fingerprint == second.snapshot.provenance_fingerprint


def test_primary_key_schema_and_row_value_changes_create_new_snapshots():
    with TestSessionLocal() as db:
        dataset = _identity(db)
        repository = IngestionRepository(db)
        base_frame = _frame()
        base = repository.resolve_snapshot(
            source_dataset_id=dataset.source_dataset_id,
            dataframe=base_frame,
            source_primary_key=["customer_id"],
            source_row_count=2,
            source_schema_fingerprint="a" * 64,
            source_dataset_fingerprint=compute_dataframe_fingerprint(base_frame),
            column_metadata=_metadata(base_frame),
        )
        primary_key_changed = repository.resolve_snapshot(
            source_dataset_id=dataset.source_dataset_id,
            dataframe=base_frame,
            source_primary_key=["external_id"],
            source_row_count=2,
            source_schema_fingerprint="a" * 64,
            source_dataset_fingerprint=compute_dataframe_fingerprint(base_frame),
            column_metadata=_metadata(base_frame),
        )
        schema_changed = repository.resolve_snapshot(
            source_dataset_id=dataset.source_dataset_id,
            dataframe=base_frame,
            source_primary_key=["customer_id"],
            source_row_count=2,
            source_schema_fingerprint="b" * 64,
            source_dataset_fingerprint=compute_dataframe_fingerprint(base_frame),
            column_metadata=_metadata(base_frame),
        )
        row_changed_frame = base_frame.copy()
        row_changed_frame.loc[100, "amount"] = decimal.Decimal("999.99")
        row_changed = repository.resolve_snapshot(
            source_dataset_id=dataset.source_dataset_id,
            dataframe=row_changed_frame,
            source_primary_key=["customer_id"],
            source_row_count=2,
            source_schema_fingerprint="a" * 64,
            source_dataset_fingerprint=compute_dataframe_fingerprint(row_changed_frame),
            column_metadata=_metadata(row_changed_frame),
        )
        db.commit()

    assert len(
        {
            base.dataset_snapshot_id,
            primary_key_changed.dataset_snapshot_id,
            schema_changed.dataset_snapshot_id,
            row_changed.dataset_snapshot_id,
        }
    ) == 4


def test_legacy_codec_v1_snapshot_payload_remains_replayable():
    legacy_frame = pd.DataFrame(
        {"customer_id": [1], "name": ["Alice"]}, dtype=object
    )
    payload = encode_dataset(legacy_frame)
    payload["format_version"] = 1
    dataset_fingerprint = compute_dataframe_fingerprint(legacy_frame)
    provenance = compute_snapshot_provenance_fingerprint(
        source_primary_key=["customer_id"],
        source_row_count=1,
        source_schema_fingerprint="a" * 64,
        source_dataset_fingerprint=dataset_fingerprint,
    )

    with TestSessionLocal() as db:
        dataset = _identity(db)
        record = DatasetSnapshotRecord(
            dataset_snapshot_id=uuid.uuid4(),
            source_dataset_id=dataset.source_dataset_id,
            provenance_fingerprint=provenance,
            source_primary_key=["customer_id"],
            source_row_count=1,
            source_schema_fingerprint="a" * 64,
            source_dataset_fingerprint=dataset_fingerprint,
            column_metadata=_metadata(legacy_frame),
            payload_format_version=1,
            snapshot_payload=payload,
            created_at=datetime.datetime.now(datetime.UTC),
        )
        db.add(record)
        db.commit()
        replayed = IngestionRepository(db).load_snapshot_dataframe(
            record.dataset_snapshot_id
        )

    pd.testing.assert_frame_equal(replayed, legacy_frame)


def test_matching_and_drifted_revalidations_link_to_captured_baseline():
    with TestSessionLocal() as db:
        dataset = _identity(db)
        captured = _capture(db, dataset.source_dataset_id)
        repository = IngestionRepository(db)
        service = IngestionService(repository)
        matched = service.record_live_revalidation(
            source_dataset_id=dataset.source_dataset_id,
            baseline_ingestion_run_id=captured.run.ingestion_run_id,
            source_read=_source_read(_frame()),
            requested_by="operator-one",
            started_at=datetime.datetime(2020, 8, 12, 13, tzinfo=datetime.UTC),
        )

        changed_frame = _frame().copy()
        changed_frame.loc[100, "amount"] = decimal.Decimal("999.99")
        drifted = service.record_live_revalidation(
            source_dataset_id=dataset.source_dataset_id,
            baseline_ingestion_run_id=captured.run.ingestion_run_id,
            source_read=_source_read(changed_frame),
            requested_by="operator-one",
            started_at=datetime.datetime(2020, 8, 12, 14, tzinfo=datetime.UTC),
        )
        db.commit()

    assert matched.matched
    assert matched.mismatch_categories == ()
    assert matched.run.baseline_ingestion_run_id == captured.run.ingestion_run_id
    assert not drifted.matched
    assert drifted.mismatch_categories == ("DATASET_FINGERPRINT",)
    assert drifted.run.baseline_ingestion_run_id == captured.run.ingestion_run_id
    assert drifted.run.dataset_snapshot_id != matched.run.dataset_snapshot_id


def test_failed_simulation_persists_only_stable_redacted_diagnostics():
    with TestSessionLocal() as db:
        dataset = _identity(db)
        failed = IngestionService(IngestionRepository(db)).record_failed_run(
            source_dataset_id=dataset.source_dataset_id,
            purpose="SIMULATION",
            failure_code="SOURCE_READ_FAILED",
            started_at=datetime.datetime(2020, 8, 12, 15, tzinfo=datetime.UTC),
        )
        db.commit()
        restored = IngestionRepository(db).get_run(failed.ingestion_run_id)

    assert restored.dataset_snapshot_id is None
    assert restored.failure_code == "SOURCE_READ_FAILED"
    assert restored.failure_reason == "Complete source observation could not be read."


@pytest.mark.parametrize(
    "values",
    [
        {"purpose": "SIMULATION", "outcome": "MATCHED"},
        {"purpose": "SIMULATION", "outcome": "CAPTURED", "snapshot": False},
        {"purpose": "LIVE_REVALIDATION", "outcome": "MATCHED"},
        {"purpose": "SIMULATION", "outcome": "FAILED", "failure_code": None},
        {
            "purpose": "SIMULATION",
            "outcome": "CAPTURED",
            "failure_code": "SOURCE_ERROR",
        },
    ],
)
def test_invalid_terminal_run_combinations_fail_closed(values):
    with TestSessionLocal() as db:
        dataset = _identity(db)
        captured = _capture(db, dataset.source_dataset_id)
        arguments = {
            "source_dataset_id": dataset.source_dataset_id,
            "dataset_snapshot_id": captured.snapshot.dataset_snapshot_id,
            "purpose": values["purpose"],
            "outcome": values["outcome"],
            "started_at": datetime.datetime(2026, 8, 12, 15, tzinfo=datetime.UTC),
            "completed_at": datetime.datetime(2026, 8, 12, 15, 1, tzinfo=datetime.UTC),
            "failure_code": values.get("failure_code"),
        }
        if values.get("snapshot") is False:
            arguments["dataset_snapshot_id"] = None
        with pytest.raises(InvalidIngestionRunError):
            IngestionRepository(db).append_run(**arguments)
        db.rollback()


def test_runs_are_listed_deterministically_and_repository_is_append_only():
    with TestSessionLocal() as db:
        dataset = _identity(db)
        first = _capture(
            db,
            dataset.source_dataset_id,
            started_at=datetime.datetime(2026, 8, 12, 10, tzinfo=datetime.UTC),
        )
        second = _capture(
            db,
            dataset.source_dataset_id,
            started_at=datetime.datetime(2026, 8, 12, 11, tzinfo=datetime.UTC),
        )
        db.commit()
        repository = IngestionRepository(db)
        runs = repository.list_runs(dataset.source_dataset_id)
        repeated = repository.list_runs(dataset.source_dataset_id)

    assert runs == repeated
    assert {run.ingestion_run_id for run in runs} == {
        first.run.ingestion_run_id,
        second.run.ingestion_run_id,
    }
    assert runs == sorted(
        runs, key=lambda run: (run.completed_at, run.ingestion_run_id)
    )
    assert not hasattr(IngestionRepository, "update_run")
    assert not hasattr(IngestionRepository, "delete_run")
    assert not hasattr(IngestionRepository, "update_snapshot")
    assert not hasattr(IngestionRepository, "delete_snapshot")


def test_snapshot_backed_ticket_uses_sql_null_and_legacy_ticket_still_replays():
    original = _frame()
    with TestSessionLocal() as db:
        dataset = _identity(db)
        captured = _capture(db, dataset.source_dataset_id, _source_read(original))
        approvals = PostgresApprovalRepository(db)
        linked = approvals.submit(
            repair_plan=_plan(),
            observed_schema=_schema(),
            gold_schema=_schema(),
            target_dataset=None,
            source_schema="public",
            source_table="customers",
            source_primary_key=list(captured.snapshot.source_primary_key),
            source_row_count=captured.snapshot.source_row_count,
            source_schema_fingerprint=captured.snapshot.source_schema_fingerprint,
            source_dataset_fingerprint=captured.snapshot.source_dataset_fingerprint,
            live_eligible=True,
            source_ingestion_run_id=str(captured.run.ingestion_run_id),
        )
        is_sql_null = db.execute(
            text(
                "SELECT target_dataset IS NULL FROM approval_tickets "
                "WHERE ticket_id = :ticket_id"
            ),
            {"ticket_id": linked.ticket_id},
        ).scalar_one()
        fetched_linked = approvals.get(linked.ticket_id)

        legacy = approvals.submit(
            repair_plan=_plan(),
            observed_schema=_schema(),
            gold_schema=_schema(),
            target_dataset=original,
        )
        fetched_legacy = approvals.get(legacy.ticket_id)
        approved_linked = approvals.approve(
            linked.ticket_id, operator="snapshot-test"
        )
        db.rollback()

    assert is_sql_null is True
    assert fetched_linked.source_ingestion_run_id == str(
        captured.run.ingestion_run_id
    )
    pd.testing.assert_frame_equal(fetched_linked.target_dataset, original)
    pd.testing.assert_frame_equal(approved_linked.target_dataset, original)
    assert fetched_legacy.source_ingestion_run_id is None
    pd.testing.assert_frame_equal(fetched_legacy.target_dataset, original)


def test_ticket_provenance_mismatch_and_ambiguous_replay_sources_are_rejected():
    with TestSessionLocal() as db:
        dataset = _identity(db)
        captured = _capture(db, dataset.source_dataset_id)
        approvals = PostgresApprovalRepository(db)
        common = {
            "repair_plan": _plan(),
            "observed_schema": _schema(),
            "gold_schema": _schema(),
        }
        with pytest.raises(ApprovalReplayError, match="source_row_count"):
            approvals.submit(
                **common,
                target_dataset=None,
                source_schema="public",
                source_table="customers",
                source_primary_key=list(captured.snapshot.source_primary_key),
                source_row_count=999,
                source_schema_fingerprint=captured.snapshot.source_schema_fingerprint,
                source_dataset_fingerprint=captured.snapshot.source_dataset_fingerprint,
                live_eligible=True,
                source_ingestion_run_id=str(captured.run.ingestion_run_id),
            )
        with pytest.raises(ApprovalReplayError, match="exactly one"):
            approvals.submit(**common, target_dataset=None)
        with pytest.raises(ApprovalReplayError, match="exactly one"):
            approvals.submit(
                **common,
                target_dataset=_frame(),
                source_ingestion_run_id=str(captured.run.ingestion_run_id),
            )
        db.rollback()


def test_tampered_snapshot_payload_is_refused_during_replay():
    with TestSessionLocal() as db:
        dataset = _identity(db)
        captured = _capture(db, dataset.source_dataset_id)
        db.commit()
        record = db.get(
            DatasetSnapshotRecord, captured.snapshot.dataset_snapshot_id
        )
        record.source_row_count = 999
        db.commit()
        with pytest.raises(IngestionLineageError, match="row count"):
            IngestionRepository(db).load_simulation_replay(
                captured.run.ingestion_run_id
            )


def test_concurrent_identical_snapshot_resolution_returns_one_identity():
    with TestSessionLocal() as db:
        dataset = _identity(db)

    outcomes = []
    failures = []
    outcome_lock = threading.Lock()

    def resolve():
        session = TestSessionLocal()
        try:
            snapshot = IngestionRepository(session).resolve_snapshot(
                source_dataset_id=dataset.source_dataset_id,
                dataframe=_frame(),
                source_primary_key=["customer_id"],
                source_row_count=2,
                source_schema_fingerprint="a" * 64,
                source_dataset_fingerprint=compute_dataframe_fingerprint(_frame()),
                column_metadata=_metadata(_frame()),
            )
            session.commit()
            with outcome_lock:
                outcomes.append(snapshot.dataset_snapshot_id)
        except Exception as exc:  # surfaced by assertions below
            session.rollback()
            with outcome_lock:
                failures.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(outcomes) == 2
    assert len(set(outcomes)) == 1
