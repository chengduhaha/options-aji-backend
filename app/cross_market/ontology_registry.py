"""Ontology YAML loader singleton."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ONTOLOGY_SRC = _ROOT / "packages" / "ontology-py" / "src"
if _ONTOLOGY_SRC.is_dir() and str(_ONTOLOGY_SRC) not in sys.path:
    sys.path.insert(0, str(_ONTOLOGY_SRC))

from ontology import OntologyLoader

ONTOLOGY_ROOT = _ROOT / "ontology"
ontology = OntologyLoader(ONTOLOGY_ROOT)
