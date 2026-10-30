"""Test configuration.

Puts ``tools/`` on the import path so the tests can ``import tsupgrade``
without the package needing to be installed. The toolkit is deliberately
dependency-free and runs straight from a clone, so the tests do too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def basic_matrix_path() -> Path:
    """Path to the hand-written fixture matrix."""
    return FIXTURES / "matrix_basic.json"


@pytest.fixture
def write_matrix(tmp_path: Path):
    """Write a matrix document to a temp file and return its path.

    Used for malformed inputs, which are clearer written inline next to the
    assertion than kept as a fixture file each.
    """

    def _write(doc: Any, name: str = "matrix.json") -> Path:
        path = tmp_path / name
        if isinstance(doc, (dict, list)):
            path.write_text(json.dumps(doc), encoding="utf-8")
        else:
            path.write_text(str(doc), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def valid_entry() -> Dict[str, Any]:
    """A minimal well-formed entry, for tests that mutate one field."""
    return {
        "source_pg": "16",
        "source_ts": "2.28",
        "target_pg": "17",
        "target_ts": "2.30",
        "verdict": "in_place",
        "reason": "fixture",
        "steps": [],
        "verified": True,
        "evidence": {
            "empty": "verify/evidence/fixture.empty.json",
            "seeded": "verify/evidence/fixture.seeded.json",
        },
        "references": ["https://example.invalid/fixture"],
    }
