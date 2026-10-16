import sys
from pathlib import Path

import pytest

SANDBOX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SANDBOX))
sys.path.insert(0, str(SANDBOX / "images" / "egress"))


@pytest.fixture
def evidence(request):
    """Print one evidence line per test (run pytest with -s to see them)."""
    def emit(msg: str) -> None:
        print(f"\nEVIDENCE {request.node.name}: {msg}")
    return emit
