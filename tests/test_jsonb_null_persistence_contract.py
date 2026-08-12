"""Phase 3.1.5 PostgreSQL JSONB NULL persistence contract."""

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from src.db.models import ApprovalTicketRecord, HealingManifestRecord


def _column(model, name):
    return model.__table__.columns[name]


def test_approval_conversion_decision_uses_sql_null_for_python_none():
    column_type = _column(ApprovalTicketRecord, "conversion_decision").type
    assert isinstance(column_type, JSONB)
    assert column_type.none_as_null is True
    processor = column_type.bind_processor(postgresql.dialect())
    assert processor is not None
    assert processor(None) is None


def test_manifest_conversion_outcome_uses_sql_null_for_python_none():
    column_type = _column(HealingManifestRecord, "conversion_outcome").type
    assert isinstance(column_type, JSONB)
    assert column_type.none_as_null is True
    processor = column_type.bind_processor(postgresql.dialect())
    assert processor is not None
    assert processor(None) is None


def test_approval_json_object_constraint_remains_enabled():
    names = {constraint.name for constraint in ApprovalTicketRecord.__table__.constraints}
    assert "ck_approval_tickets_conversion_decision_object" in names


def test_manifest_json_object_constraint_remains_enabled():
    names = {constraint.name for constraint in HealingManifestRecord.__table__.constraints}
    assert "ck_healing_manifests_conversion_outcome_object" in names
