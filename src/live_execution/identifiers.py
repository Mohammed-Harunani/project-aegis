"""
Aegis_LiveExecution_Identifiers
Phase 2.5 -- strict identifier validation for anything that ends up in
dynamically-built DDL (CREATE TABLE, ALTER TABLE ... RENAME TO).

SQL has no bind-parameter mechanism for identifiers the way it does
for values, so the mitigation against injection is allowlist
validation BEFORE a name ever touches a SQL string -- not quoting or
escaping after the fact.
"""

import re

# Matches Postgres's own unquoted-identifier rules closely enough for
# our purposes, and the {0,62} bound respects the 63-byte identifier
# limit (1 required leading char + up to 62 more).
_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


class InvalidIdentifierError(Exception):
    """Raised when a schema/table/column name fails strict validation."""


def validate_identifier(name: str, kind: str) -> str:
    """
    Returns name unchanged if valid; raises InvalidIdentifierError
    otherwise. `kind` is just for a clearer error message (e.g.
    "target table", "column").
    """
    if not isinstance(name, str) or not _SAFE_IDENTIFIER.match(name):
        raise InvalidIdentifierError(
            f"Invalid {kind} identifier: {name!r}. Must match "
            f"{_SAFE_IDENTIFIER.pattern} (letters, digits, underscores, "
            f"starting with a letter or underscore, max 63 characters)."
        )
    return name


def shadow_table_name(target_table: str, execution_id) -> str:
    """
    execution_id is a uuid.UUID; .hex strips dashes (32 hex chars).
    Very long target_table names combined with this suffix can still
    exceed Postgres's 63-byte identifier limit -- a known constraint,
    not solved here with truncation/hashing schemes nobody asked for.
    """
    return f"{target_table}__aegis_shadow_{execution_id.hex}"


def backup_table_name(target_table: str, execution_id) -> str:
    return f"{target_table}__aegis_backup_{execution_id.hex}"
