import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

sys.path.insert(0, str(SRC_DIR))
# Also needed so `api.app`'s internal `from src.x import y` imports resolve --
# without this, any test importing api.app fails at import time, before
# a single test runs. Existing tests are unaffected: they only ever import
# unqualified (`from inspector import ...`), which still resolves via SRC_DIR.
sys.path.insert(0, str(PROJECT_ROOT))
