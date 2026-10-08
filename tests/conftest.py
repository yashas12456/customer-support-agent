import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from support_agent import build_agent  # noqa: E402
from support_agent.rag import PolicyIndex  # noqa: E402
from support_agent.store import Store  # noqa: E402


@pytest.fixture(scope="session")
def policies():
    return PolicyIndex()


@pytest.fixture
def store():
    return Store(runtime_dir=None)      # fresh in-memory copy per test


@pytest.fixture
def make_agent(store, policies):
    def _make(email="alex@example.com"):
        return build_agent(email, brain="offline", persist=False, store=store, policies=policies)
    return _make
