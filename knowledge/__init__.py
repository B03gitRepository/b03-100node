"""Versioned packet-network facts and work-order knowledge for OM and RC."""
from pathlib import Path
import sys

# Existing public providers import the shared runtime as top-level modules from src/.
_source = str(Path(__file__).resolve().parents[1] / "src")
if _source not in sys.path:
    sys.path.append(_source)
