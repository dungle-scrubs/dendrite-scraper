"""Shared fixtures for dendrite-scraper tests."""

import pytest
from fastapi.testclient import TestClient

from dendrite_scraper.server import app


@pytest.fixture
def client() -> TestClient:
    """Synchronous test client for the FastAPI app."""
    return TestClient(app)
