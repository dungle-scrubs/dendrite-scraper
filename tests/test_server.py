"""Tests for the HTTP server endpoints.

Uses FastAPI's TestClient — no real server needed.
Scraping calls are mocked to avoid network access.
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from dendrite_scraper.scraper import ScrapeResult


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_returns_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_response_shape_is_stable(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"status"}


class TestScrapeEndpoint:
    """Tests for POST /scrape."""

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_successful_scrape(self, mock_scrape: AsyncMock, client: TestClient) -> None:
        mock_scrape.return_value = ScrapeResult(
            markdown="# Hello\n\nWorld\n",
            source="crawl4ai",
            url="https://example.com",
            elapsed_ms=150.0,
            attempts=["crawl4ai attempt 1"],
        )

        resp = client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert set(body.keys()) == {
            "markdown",
            "source",
            "url",
            "bot_detected",
            "llm_cleaned",
            "error",
            "elapsed_ms",
            "attempts",
        }
        assert body["markdown"] == "# Hello\n\nWorld\n"
        assert body["source"] == "crawl4ai"
        assert body["url"] == "https://example.com"
        assert body["bot_detected"] is False
        assert body["llm_cleaned"] is False
        assert body["error"] is None
        assert body["elapsed_ms"] == 150.0
        assert body["attempts"] == ["crawl4ai attempt 1"]

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_failed_scrape(self, mock_scrape: AsyncMock, client: TestClient) -> None:
        mock_scrape.return_value = ScrapeResult(
            url="https://example.com",
            error="Both crawl4ai and Jina failed",
            elapsed_ms=5000.0,
            attempts=["crawl4ai attempt 1", "crawl4ai attempt 2", "jina fallback"],
        )

        resp = client.post("/scrape", json={"url": "https://example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert set(body.keys()) == {
            "markdown",
            "source",
            "url",
            "bot_detected",
            "llm_cleaned",
            "error",
            "elapsed_ms",
            "attempts",
        }
        assert body["markdown"] == ""
        assert body["source"] == "none"
        assert body["url"] == "https://example.com"
        assert body["bot_detected"] is False
        assert body["llm_cleaned"] is False
        assert body["error"] == "Both crawl4ai and Jina failed"
        assert body["elapsed_ms"] == 5000.0
        assert body["attempts"] == ["crawl4ai attempt 1", "crawl4ai attempt 2", "jina fallback"]

    @patch("dendrite_scraper.server.scrape", new_callable=AsyncMock)
    def test_bot_detected_with_jina_fallback(
        self, mock_scrape: AsyncMock, client: TestClient
    ) -> None:
        mock_scrape.return_value = ScrapeResult(
            markdown="# Real content from Jina\n",
            source="jina",
            url="https://protected.example.com",
            bot_detected=True,
            elapsed_ms=3200.0,
            attempts=["crawl4ai attempt 1", "bot protection detected", "jina fallback"],
        )

        resp = client.post("/scrape", json={"url": "https://protected.example.com"})
        assert resp.status_code == 200

        body = resp.json()
        assert body["source"] == "jina"
        assert body["bot_detected"] is True
        assert body["markdown"] != ""

    def test_invalid_url_rejected(self, client: TestClient) -> None:
        resp = client.post("/scrape", json={"url": "not-a-url"})
        assert resp.status_code == 422

    def test_missing_url_rejected(self, client: TestClient) -> None:
        resp = client.post("/scrape", json={})
        assert resp.status_code == 422
