"""Use real HA classes, synthetic credentials, and intercepted HTTP only."""

import sys
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def aioresponses_aiohttp_314_compat(monkeypatch):
    """aioresponses 0.7.9 predates aiohttp's new required stream_writer argument."""

    class MockResponse(aiohttp.ClientResponse):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("stream_writer", SimpleNamespace(output_size=0))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("aioresponses.core.ClientResponse", MockResponse)
