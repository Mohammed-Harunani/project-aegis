"""
Pure-Python tests for the schema-definition format: validation and
fingerprinting. No database dependency -- these run in any environment
with pandas installed, unlike the rest of Phase 2.4's tests.
"""

from registry.schema_definition import (
    InvalidSchemaDefinitionError,
    validate_schema_name,
    validate_schema_definition,
    validate_and_normalize_created_by,
    compute_fingerprint,
    to_gold_schema_dict,
)


def _expect_invalid(format_version, columns):
    try:
        validate_schema_definition(format_version, columns)
        assert False, f"expected InvalidSchemaDefinitionError for {columns!r}"
    except InvalidSchemaDefinitionError:
        pass


def test_valid_definition_passes():
    validate_schema_definition(1, [{"name": "customer_id", "dtype": "int64"}])


def test_empty_columns_rejected():
    _expect_invalid(1, [])


def test_empty_column_name_rejected():
    _expect_invalid(1, [{"name": "", "dtype": "int64"}])


def test_duplicate_column_name_rejected():
    _expect_invalid(1, [{"name": "a", "dtype": "int64"}, {"name": "a", "dtype": "float64"}])


def test_unsupported_dtype_rejected():
    _expect_invalid(1, [{"name": "a", "dtype": "not_a_real_dtype"}])


def test_unsupported_format_version_rejected():
    _expect_invalid(2, [{"name": "a", "dtype": "int64"}])


def test_valid_schema_names():
    for name in ["customer_master", "general_ledger", "supplier_records", "sales_transactions"]:
        validate_schema_name(name)  # must not raise


def test_invalid_schema_names_rejected():
    for name in ["Customer_Master", "1_customer", "customer master", "", "customer-master"]:
        try:
            validate_schema_name(name)
            assert False, f"expected rejection for {name!r}"
        except InvalidSchemaDefinitionError:
            pass


def test_fingerprint_is_deterministic():
    cols = [{"name": "customer_id", "dtype": "int64"}, {"name": "customer_name", "dtype": "object"}]
    assert compute_fingerprint(1, cols) == compute_fingerprint(1, list(cols))


def test_fingerprint_changes_with_column_order():
    a = [{"name": "customer_id", "dtype": "int64"}, {"name": "customer_name", "dtype": "object"}]
    b = [{"name": "customer_name", "dtype": "object"}, {"name": "customer_id", "dtype": "int64"}]
    assert compute_fingerprint(1, a) != compute_fingerprint(1, b)


def test_fingerprint_changes_with_dtype():
    a = [{"name": "customer_id", "dtype": "int64"}]
    b = [{"name": "customer_id", "dtype": "float64"}]
    assert compute_fingerprint(1, a) != compute_fingerprint(1, b)


def test_fingerprint_ignores_metadata_keys_on_columns():
    a = [{"name": "customer_id", "dtype": "int64"}]
    b = [{"name": "customer_id", "dtype": "int64", "unexpected_field": "ignored"}]
    assert compute_fingerprint(1, a) == compute_fingerprint(1, b)


def test_to_gold_schema_dict_conversion():
    cols = [{"name": "customer_id", "dtype": "int64"}, {"name": "customer_name", "dtype": "object"}]
    assert to_gold_schema_dict(cols) == {"customer_id": "int64", "customer_name": "object"}


def test_created_by_is_stripped_of_surrounding_whitespace():
    assert validate_and_normalize_created_by("  mohammed  ") == "mohammed"
    assert validate_and_normalize_created_by("mohammed") == "mohammed"


def test_empty_created_by_rejected():
    try:
        validate_and_normalize_created_by("")
        assert False, "expected InvalidSchemaDefinitionError"
    except InvalidSchemaDefinitionError:
        pass


def test_whitespace_only_created_by_rejected():
    """
    The specific gap: Pydantic's Field(min_length=1) alone does not
    catch this -- min_length is a plain character-count check on the
    string as given, run before any stripping. Confirmed directly.
    """
    for bad in ["   ", "\t", "\n  \t"]:
        try:
            validate_and_normalize_created_by(bad)
            assert False, f"expected rejection for {bad!r}"
        except InvalidSchemaDefinitionError:
            pass


def test_none_created_by_rejected():
    try:
        validate_and_normalize_created_by(None)
        assert False, "expected InvalidSchemaDefinitionError"
    except InvalidSchemaDefinitionError:
        pass
