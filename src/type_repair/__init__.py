"""
Aegis_TypeRepair
Phase 3.1.2 -- the pure, database-independent verified type-conversion
engine. See Docs/phase3_1_verified_type_repair_spec.md for the full
safety contract this package implements.

This package must never import FastAPI, SQLAlchemy sessions,
repositories, or PostgreSQL connectors -- its tests are required to
run with only Python and pandas installed. Database and API
integration belongs in later Phase 3.1 stages (3.1.3 onward), not here.
"""

from .converter import analyze_and_convert
from .models import (
    ColumnConversionResult,
    ConversionDiagnostic,
    ConversionStatus,
    ReasonCode,
)
from .policy import ConversionPolicy, DEFAULT_POLICY

__all__ = [
    "analyze_and_convert",
    "ColumnConversionResult",
    "ConversionDiagnostic",
    "ConversionStatus",
    "ReasonCode",
    "ConversionPolicy",
    "DEFAULT_POLICY",
]
