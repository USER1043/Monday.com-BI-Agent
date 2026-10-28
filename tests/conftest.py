import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(autouse=True)
def data_source(monkeypatch):
    """
    Run tools against the Excel snapshots by default so tests are offline and
    deterministic. Set TEST_DATA_SOURCE=monday to check the live MCP path instead.
    """
    monkeypatch.setenv("DATA_SOURCE", os.environ.get("TEST_DATA_SOURCE", "excel"))
