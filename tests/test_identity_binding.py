"""Pure tests for Phase 3.2 server-controlled database identity bindings."""

import os
from dataclasses import replace

import pytest

from src.identity.binding import (
    IdentityBindingError,
    IdentityConfigurationError,
    UnsafeDatabaseTopologyError,
    build_endpoint_binding,
    configured_binding,
    ensure_distinct_bindings,
    validate_endpoint_binding,
    validate_system_key,
)


def test_valid_system_keys_are_returned_unchanged():
    for key in ("erp-source", "aegis_source_1", "abc", "a" + "1" * 63):
        assert validate_system_key(key) == key


def test_invalid_system_keys_fail_closed():
    for key in (
        None,
        "",
        "ab",
        "Uppercase",
        "1-leading",
        "contains space",
        "contains.dot",
        "a" * 65,
    ):
        with pytest.raises(IdentityConfigurationError):
            validate_system_key(key)


def test_binding_is_stable_across_credentials_driver_and_nonrouting_query():
    first = build_endpoint_binding(
        "postgresql+psycopg://user_one:secret_one@DB.EXAMPLE.COM/erp"
        "?sslmode=require&application_name=aegis"
    )
    second = build_endpoint_binding(
        "postgresql+psycopg2://user_two:secret_two@db.example.com:5432/erp"
        "?sslmode=verify-full"
    )
    assert first.fingerprint == second.fingerprint
    assert first.host == "db.example.com"
    assert first.port == 5432
    assert first.database == "erp"
    assert first.dialect == "postgresql"


@pytest.mark.parametrize(
    "changed_url",
    [
        "postgresql://user:pw@other.example.com:5432/erp",
        "postgresql://user:pw@db.example.com:5433/erp",
        "postgresql://user:pw@db.example.com:5432/other_db",
    ],
)
def test_host_port_or_database_change_changes_binding(changed_url):
    baseline = build_endpoint_binding(
        "postgresql://user:pw@db.example.com:5432/erp"
    )
    changed = build_endpoint_binding(changed_url)
    assert changed.fingerprint != baseline.fingerprint


def test_binding_fingerprint_is_deterministic_sha256():
    binding = build_endpoint_binding(
        "postgresql+psycopg://user:pw@db.example.com:5432/erp"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "forged.example.com"),
        ("host", "DB.EXAMPLE.COM"),
        ("port", 5433),
        ("port", 0),
        ("database", "forged"),
    ],
)
def test_forged_or_noncanonical_binding_objects_are_rejected(field, value):
    binding = build_endpoint_binding(
        "postgresql://user:pw@db.example.com:5432/erp"
    )
    with pytest.raises(IdentityBindingError):
        validate_endpoint_binding(replace(binding, **{field: value}))
    assert binding.fingerprint == (
        "463b215553998f6e2c872c8145264a718229915fb255b3e19fe2f6aaf8e40224"
    )


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "sqlite:///local.db",
        "postgresql+unknown://user:pw@db.example.com/erp",
        "postgresql://user:pw@/erp",
        "postgresql://user:pw@one.example.com,two.example.com/erp",
        "postgresql://user:pw@db.example.com/erp?host=other.example.com",
        "postgresql://user:pw@db.example.com/erp?dbname=other_database",
        "postgresql://user:pw@db.example.com/erp?service=production",
    ],
)
def test_unsupported_or_ambiguous_endpoint_shapes_are_rejected(unsafe_url):
    with pytest.raises(IdentityBindingError):
        build_endpoint_binding(unsafe_url)


def test_binding_errors_never_echo_credentials():
    secret = "do-not-leak-this-password"
    with pytest.raises(IdentityBindingError) as exc_info:
        build_endpoint_binding(f"postgresql://user:{secret}@/erp")
    assert secret not in str(exc_info.value)


def test_configured_binding_requires_both_server_settings(monkeypatch):
    monkeypatch.delenv("AEGIS_SOURCE_SYSTEM_KEY", raising=False)
    monkeypatch.delenv("SOURCE_DATABASE_URL", raising=False)
    with pytest.raises(IdentityConfigurationError, match="AEGIS_SOURCE_SYSTEM_KEY"):
        configured_binding(
            key_env="AEGIS_SOURCE_SYSTEM_KEY",
            url_env="SOURCE_DATABASE_URL",
        )

    monkeypatch.setenv("AEGIS_SOURCE_SYSTEM_KEY", "source-prod")
    with pytest.raises(IdentityConfigurationError, match="SOURCE_DATABASE_URL"):
        configured_binding(
            key_env="AEGIS_SOURCE_SYSTEM_KEY",
            url_env="SOURCE_DATABASE_URL",
        )


def test_configured_binding_never_returns_the_url(monkeypatch):
    monkeypatch.setenv("AEGIS_SOURCE_SYSTEM_KEY", "source-prod")
    monkeypatch.setenv(
        "SOURCE_DATABASE_URL",
        "postgresql://user:secret@db.example.com/erp",
    )
    key, binding = configured_binding(
        key_env="AEGIS_SOURCE_SYSTEM_KEY",
        url_env="SOURCE_DATABASE_URL",
    )
    assert key == "source-prod"
    assert "secret" not in repr(binding)
    assert "user" not in repr(binding)


def test_equal_source_and_publication_bindings_are_rejected():
    source = build_endpoint_binding(
        "postgresql://source_user:one@db.example.com/erp"
    )
    publication = build_endpoint_binding(
        "postgresql://publish_user:two@db.example.com/erp"
    )
    with pytest.raises(UnsafeDatabaseTopologyError):
        ensure_distinct_bindings(source, publication)


def test_distinct_source_and_publication_bindings_pass():
    source = build_endpoint_binding(
        "postgresql://source_user:one@db.example.com/source_db"
    )
    publication = build_endpoint_binding(
        "postgresql://publish_user:two@db.example.com/publish_db"
    )
    ensure_distinct_bindings(source, publication)


def test_module_does_not_mutate_environment():
    before = dict(os.environ)
    build_endpoint_binding("postgresql://user:pw@db.example.com/erp")
    assert dict(os.environ) == before
