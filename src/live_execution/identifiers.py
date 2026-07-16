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


MAX_IDENTIFIER_LENGTH = 63
_EXECUTION_ID_SUFFIX_CHARS = 16  # 64 bits -- ample collision resistance at this system's realistic volume


def _controlled_suffixed_name(target_table: str, marker: str, execution_id) -> str:
    """
    Builds a name guaranteed to fit within Postgres's 63-byte identifier
    limit, however long target_table is. The naive version (just
    concatenating table + marker + full UUID hex) silently overflows
    for any target_table anywhere close to 63 characters -- confirmed
    directly: a 63-char table name caused Postgres to truncate away
    the ENTIRE suffix, including the shadow/backup marker itself,
    making shadow and backup names identical and colliding across
    different executions. The fix reserves a fixed-length suffix
    first, then truncates the table-name portion to whatever's left.
    """
    suffix = f"__aegis_{marker}_{execution_id.hex[:_EXECUTION_ID_SUFFIX_CHARS]}"
    available_for_table = MAX_IDENTIFIER_LENGTH - len(suffix)
    return f"{target_table[:available_for_table]}{suffix}"


def shadow_table_name(target_table: str, execution_id) -> str:
    return _controlled_suffixed_name(target_table, "shadow", execution_id)


def backup_table_name(target_table: str, execution_id) -> str:
    return _controlled_suffixed_name(target_table, "backup", execution_id)
