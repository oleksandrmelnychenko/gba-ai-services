"""Ensure the project root is importable so `import app` works under bare `pytest`
(without requiring an editable install), matching how scripts/ put root on sys.path."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
