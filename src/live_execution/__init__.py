from .identifiers import (
    InvalidIdentifierError,
    validate_identifier,
    shadow_table_name,
    backup_table_name,
)
from .dtype_mapping import UnsupportedDtypeError, pandas_dtype_to_postgres_type
from .safety import (
    LiveExecutionNotAllowedError,
    LiveExecutionConflictError,
    live_execution_globally_enabled,
    get_target_schema_allowlist,
    evaluate_safety_gates,
)

__all__ = [
    "InvalidIdentifierError",
    "validate_identifier",
    "shadow_table_name",
    "backup_table_name",
    "UnsupportedDtypeError",
    "pandas_dtype_to_postgres_type",
    "LiveExecutionNotAllowedError",
    "LiveExecutionConflictError",
    "live_execution_globally_enabled",
    "get_target_schema_allowlist",
    "evaluate_safety_gates",
]

# PostgresLiveWriter and LiveExecutionRepository are intentionally NOT
# re-exported here -- both require sqlalchemy, and doing so here would
# make it a transitive dependency of every plain `from live_execution.x
# import y`, including the pure-logic pieces above. Import them
# directly where needed:
#   from src.live_execution.writer import PostgresLiveWriter
#   from src.live_execution.repository import LiveExecutionRepository
