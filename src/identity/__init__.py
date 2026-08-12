"""Phase 3.2 stable source, dataset, and publication identities."""

from src.identity.binding import (
    EndpointBinding,
    IdentityBindingError,
    IdentityConfigurationError,
    UnsafeDatabaseTopologyError,
    build_endpoint_binding,
    configured_binding,
    ensure_distinct_bindings,
    validate_endpoint_binding,
    validate_system_key,
)
from src.identity.models import (
    PublicationSystemIdentity,
    PublicationTargetIdentity,
    SourceDatasetIdentity,
    SourceSystemIdentity,
)
from src.identity.repository import (
    EndpointBindingAlreadyRegisteredError,
    IdentityNotFoundError,
    IdentityRepository,
    SystemBindingConflictError,
)

__all__ = [
    "EndpointBinding",
    "EndpointBindingAlreadyRegisteredError",
    "IdentityBindingError",
    "IdentityConfigurationError",
    "IdentityNotFoundError",
    "IdentityRepository",
    "PublicationSystemIdentity",
    "PublicationTargetIdentity",
    "SourceDatasetIdentity",
    "SourceSystemIdentity",
    "SystemBindingConflictError",
    "UnsafeDatabaseTopologyError",
    "build_endpoint_binding",
    "configured_binding",
    "ensure_distinct_bindings",
    "validate_endpoint_binding",
    "validate_system_key",
]
