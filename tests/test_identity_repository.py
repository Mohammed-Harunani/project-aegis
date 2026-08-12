"""PostgreSQL integration tests for Phase 3.2 immutable identity resolution."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import get_verified_test_database_url


TEST_DATABASE_URL = get_verified_test_database_url()

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import (
    Base,
    PublicationSystemRecord,
    PublicationTargetRecord,
    SourceDatasetRecord,
    SourceSystemRecord,
)
from src.identity.binding import UnsafeDatabaseTopologyError, build_endpoint_binding
from src.identity.repository import (
    EndpointBindingAlreadyRegisteredError,
    IdentityNotFoundError,
    IdentityRepository,
    SystemBindingConflictError,
)


engine = create_engine(TEST_DATABASE_URL)
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def setup_module(_module):
    Base.metadata.create_all(bind=engine)


def teardown_module(_module):
    Base.metadata.drop_all(bind=engine)


def setup_function(_function):
    with engine.begin() as conn:
        conn.execute(PublicationTargetRecord.__table__.delete())
        conn.execute(SourceDatasetRecord.__table__.delete())
        conn.execute(PublicationSystemRecord.__table__.delete())
        conn.execute(SourceSystemRecord.__table__.delete())


def _binding(database):
    return build_endpoint_binding(
        f"postgresql://identity_user:secret@db.example.com/{database}"
    )


def test_same_source_key_and_binding_resolve_same_identity():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        first = repository.resolve_source_system("erp-source", _binding("erp"))
        second = repository.resolve_source_system("erp-source", _binding("erp"))
    assert first == second


def test_live_source_binding_verification_requires_exact_captured_identity():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        system = repository.resolve_source_system("erp-source", _binding("erp"))
        assert repository.verify_source_system_binding(
            system.source_system_id,
            "erp-source",
            _binding("erp"),
        ) == system
        with pytest.raises(SystemBindingConflictError):
            repository.verify_source_system_binding(
                system.source_system_id,
                "other-source",
                _binding("erp"),
            )
        with pytest.raises(SystemBindingConflictError):
            repository.verify_source_system_binding(
                system.source_system_id,
                "erp-source",
                _binding("other"),
            )


def test_source_key_rebinding_is_rejected_without_overwrite():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        original = repository.resolve_source_system("erp-source", _binding("erp"))
        with pytest.raises(SystemBindingConflictError):
            repository.resolve_source_system("erp-source", _binding("other"))
        persisted = db.get(SourceSystemRecord, original.source_system_id)
        assert persisted.endpoint_binding_fingerprint == original.endpoint_binding_fingerprint


def test_same_source_endpoint_under_different_key_is_rejected():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        repository.resolve_source_system("erp-source", _binding("erp"))
        with pytest.raises(EndpointBindingAlreadyRegisteredError):
            repository.resolve_source_system("second-key", _binding("erp"))


def test_source_and_publication_may_not_share_one_binding():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        repository.resolve_source_system("erp-source", _binding("erp"))
        with pytest.raises(UnsafeDatabaseTopologyError):
            repository.resolve_publication_system("aegis-publish", _binding("erp"))


def test_source_dataset_identity_is_stable_and_relation_scoped():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        system = repository.resolve_source_system("erp-source", _binding("erp"))
        first = repository.resolve_source_dataset(
            system.source_system_id, "raw", "customer_master"
        )
        same = repository.resolve_source_dataset(
            system.source_system_id, "raw", "customer_master"
        )
        other = repository.resolve_source_dataset(
            system.source_system_id, "raw", "supplier_master"
        )
    assert first == same
    assert other.source_dataset_id != first.source_dataset_id
    with TestSessionLocal() as db:
        assert IdentityRepository(db).get_source_dataset(
            first.source_dataset_id
        ) == first


def test_publication_target_identity_is_stable_and_target_scoped():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        system = repository.resolve_publication_system(
            "aegis-publish", _binding("publish")
        )
        first = repository.resolve_publication_target(
            system.publication_system_id, "customer_master"
        )
        same = repository.resolve_publication_target(
            system.publication_system_id, "customer_master"
        )
        other = repository.resolve_publication_target(
            system.publication_system_id, "supplier_master"
        )
    assert first == same
    assert other.publication_target_id != first.publication_target_id


def test_unknown_parent_identity_is_rejected():
    with TestSessionLocal() as db:
        repository = IdentityRepository(db)
        with pytest.raises(IdentityNotFoundError):
            repository.resolve_source_dataset(
                "00000000-0000-0000-0000-000000000000",
                "raw",
                "customer_master",
            )


def test_concurrent_source_dataset_resolution_returns_one_identity():
    with TestSessionLocal() as db:
        system = IdentityRepository(db).resolve_source_system(
            "erp-source", _binding("erp")
        )

    outcomes = []
    outcome_lock = threading.Lock()

    def resolve():
        session = TestSessionLocal()
        try:
            identity = IdentityRepository(session).resolve_source_dataset(
                system.source_system_id, "raw", "customer_master"
            )
            with outcome_lock:
                outcomes.append(identity.source_dataset_id)
        finally:
            session.close()

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 2
    assert len(set(outcomes)) == 1


def test_concurrent_source_system_resolution_returns_one_identity():
    outcomes = []
    outcome_lock = threading.Lock()

    def resolve():
        session = TestSessionLocal()
        try:
            identity = IdentityRepository(session).resolve_source_system(
                "erp-source", _binding("erp")
            )
            with outcome_lock:
                outcomes.append(identity.source_system_id)
        finally:
            session.close()

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 2
    assert len(set(outcomes)) == 1
