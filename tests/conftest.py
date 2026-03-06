"""Shared fixtures for web-scraper tests."""

import pytest
from fastapi.testclient import TestClient

from web_scraper.server import app


@pytest.fixture
def client() -> TestClient:
    """Synchronous test client for the FastAPI app."""
    return TestClient(app)
