"""
Aegis_Phase3_2_IdentityBinding

Pure validation and credential-free fingerprinting for server-controlled
source and publication database identities. Full connection URLs are used
only as transient input and are never retained in the returned fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


BINDING_FORMAT_VERSION = 1
DEFAULT_POSTGRES_PORT = 5432
SYSTEM_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_POSTGRES_DRIVER_NAMES = frozenset(
    {"postgresql", "postgresql+psycopg", "postgresql+psycopg2"}
)

# These query parameters can replace or multiply the actual network endpoint.
# Silently ignoring them would make the persisted binding describe something
# different from what the driver can connect to.
_AMBIGUOUS_ENDPOINT_QUERY_KEYS = frozenset(
    {
        "database",
        "dbname",
        "host",
        "hostaddr",
        "port",
        "service",
        "servicefile",
    }
)


class IdentityConfigurationError(RuntimeError):
    """Required server-controlled identity configuration is absent or invalid."""


class IdentityBindingError(ValueError):
    """A database URL cannot be represented by the locked binding contract."""


class UnsafeDatabaseTopologyError(ValueError):
    """Source and publication resolve to the same configured database binding."""


@dataclass(frozen=True)
class EndpointBinding:
    """In-memory, credential-free canonical PostgreSQL endpoint identity."""

    version: int
    dialect: str
    host: str
    port: int
    database: str
    fingerprint: str


def validate_system_key(value: str, *, setting_name: str = "system key") -> str:
    """Return a valid stable key or fail without exposing unrelated config."""
    if not isinstance(value, str) or not SYSTEM_KEY_PATTERN.fullmatch(value):
        raise IdentityConfigurationError(
            f"{setting_name} must match {SYSTEM_KEY_PATTERN.pattern} "
            "(3-64 lowercase letters, digits, '_' or '-', starting with a letter)."
        )
    return value


def _parse_postgres_url(database_url: str):
    if not isinstance(database_url, str) or not database_url:
        raise IdentityBindingError("Database URL must be a non-empty PostgreSQL URL.")
    try:
        parsed = make_url(database_url)
    except (ArgumentError, TypeError, ValueError) as exc:
        # Deliberately do not interpolate the submitted URL: it can contain a
        # password and must never appear in an exception or log.
        raise IdentityBindingError("Database URL is not a valid SQLAlchemy URL.") from exc

    drivername = parsed.drivername.lower()
    dialect = drivername.split("+", 1)[0]
    if drivername not in SUPPORTED_POSTGRES_DRIVER_NAMES:
        raise IdentityBindingError(
            "Database binding must use an explicitly supported PostgreSQL driver."
        )
    if not parsed.host:
        raise IdentityBindingError(
            "Database binding requires one explicit host; hostless/socket URLs are unsupported."
        )
    if "," in parsed.host:
        raise IdentityBindingError(
            "Database binding requires one explicit host; multi-host URLs are unsupported."
        )
    query_keys = {str(key).lower() for key in parsed.query}
    ambiguous = sorted(query_keys & _AMBIGUOUS_ENDPOINT_QUERY_KEYS)
    if ambiguous:
        raise IdentityBindingError(
            "Database binding query contains endpoint-routing options that Phase 3.2 "
            "cannot identify safely."
        )
    if not parsed.database:
        raise IdentityBindingError("Database binding requires an explicit database name.")
    return parsed, dialect


def _compute_binding_fingerprint(
    *, version: int, dialect: str, host: str, port: int, database: str
) -> str:
    canonical = {
        "database": database,
        "dialect": dialect,
        "host": host,
        "port": port,
        "version": version,
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_endpoint_binding(database_url: str) -> EndpointBinding:
    """
    Produce binding-v1 SHA-256 from host, port, database, and normalized
    PostgreSQL dialect. Credentials, driver suffix, and non-routing query
    options deliberately do not affect identity.
    """
    parsed, dialect = _parse_postgres_url(database_url)
    host = parsed.host.lower()
    try:
        port = parsed.port or DEFAULT_POSTGRES_PORT
    except ValueError as exc:
        raise IdentityBindingError("Database binding has an invalid port.") from exc
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise IdentityBindingError("Database binding port must be between 1 and 65535.")
    database = parsed.database
    fingerprint = _compute_binding_fingerprint(
        version=BINDING_FORMAT_VERSION,
        dialect=dialect,
        host=host,
        port=port,
        database=database,
    )
    return EndpointBinding(
        version=BINDING_FORMAT_VERSION,
        dialect=dialect,
        host=host,
        port=port,
        database=database,
        fingerprint=fingerprint,
    )


def validate_endpoint_binding(binding: EndpointBinding) -> EndpointBinding:
    """Reject forged or unsupported binding objects at repository boundaries."""
    if not isinstance(binding, EndpointBinding):
        raise IdentityBindingError("Endpoint binding has an invalid type.")
    if binding.version != BINDING_FORMAT_VERSION or binding.dialect != "postgresql":
        raise IdentityBindingError("Endpoint binding uses an unsupported format or dialect.")
    if (
        not isinstance(binding.host, str)
        or not binding.host
        or binding.host != binding.host.lower()
        or "," in binding.host
    ):
        raise IdentityBindingError("Endpoint binding has a non-canonical host.")
    if (
        isinstance(binding.port, bool)
        or not isinstance(binding.port, int)
        or not 1 <= binding.port <= 65535
    ):
        raise IdentityBindingError("Endpoint binding has an invalid port.")
    if not isinstance(binding.database, str) or not binding.database:
        raise IdentityBindingError("Endpoint binding has an invalid database name.")
    if not FINGERPRINT_PATTERN.fullmatch(binding.fingerprint or ""):
        raise IdentityBindingError("Endpoint binding fingerprint is not a SHA-256 value.")
    expected = _compute_binding_fingerprint(
        version=binding.version,
        dialect=binding.dialect,
        host=binding.host,
        port=binding.port,
        database=binding.database,
    )
    if binding.fingerprint != expected:
        raise IdentityBindingError(
            "Endpoint binding fingerprint does not match its canonical components."
        )
    return binding


def configured_binding(*, key_env: str, url_env: str) -> tuple[str, EndpointBinding]:
    """Resolve one server-controlled key and URL lazily from the environment."""
    key = os.environ.get(key_env)
    if key is None:
        raise IdentityConfigurationError(f"{key_env} is not set.")
    normalized_key = validate_system_key(key, setting_name=key_env)

    database_url = os.environ.get(url_env)
    if database_url is None:
        raise IdentityConfigurationError(f"{url_env} is not set.")
    try:
        binding = build_endpoint_binding(database_url)
    except IdentityBindingError as exc:
        raise IdentityConfigurationError(
            f"{url_env} has an unsafe database binding: {exc}"
        ) from exc
    return normalized_key, binding


def ensure_distinct_bindings(source: EndpointBinding, publication: EndpointBinding) -> None:
    """Fail closed when trusted source and publication share one binding."""
    validate_endpoint_binding(source)
    validate_endpoint_binding(publication)
    if source.fingerprint == publication.fingerprint:
        raise UnsafeDatabaseTopologyError(
            "Trusted source and publication target resolve to the same database binding."
        )
