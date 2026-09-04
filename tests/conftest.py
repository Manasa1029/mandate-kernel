from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.mock_razorpay import MockRazorpay  # noqa: E402
from tests.factories import build_world  # noqa: E402


@pytest.fixture()
def world():
    """Fresh in-memory kernel per test. No shared state, no ordering dependencies."""
    return build_world()


@pytest.fixture()
def provider():
    return MockRazorpay()


@pytest.fixture()
def sleeper():
    """Collects sleep durations instead of sleeping, so retry/backoff tests are
    instant and can assert on the backoff schedule."""
    calls: list[float] = []

    def fake(seconds: float) -> None:
        calls.append(seconds)

    fake.calls = calls  # type: ignore[attr-defined]
    return fake
