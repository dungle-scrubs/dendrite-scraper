# dendrite-scraper

Web scraping service with anti-bot detection, Jina fallback, and optional
LLM cleanup. Runs as a standalone service or installs as a Python package.

## Install

```bash
pip install dendrite-scraper
```

## API

```
POST /scrape {"url": "https://example.com"}
GET  /health
```

### Response

```json
{
  "markdown": "# Page Title\n\nClean content...\n",
  "source": "crawl4ai",
  "url": "https://example.com",
  "bot_detected": false,
  "llm_cleaned": false,
  "error": null,
  "elapsed_ms": 1234.5,
  "attempts": ["crawl4ai attempt 1"]
}
```

## Pipeline

1. **Crawl4AI** — local Playwright headless Chromium, retries on transient
   errors
2. **Bot detection** — Cloudflare/CAPTCHA phrases + partial JS render
   heuristic (empty table cells)
3. **Jina Reader fallback** — free cloud re-fetch when bot-blocked or crawl
   fails
4. **Noise detection** — link-density heuristic for nav/sidebar chrome
5. **LLM cleanup** — optional gpt-4o-mini pass to strip non-content noise
   (requires `OPENAI_API_KEY`)
6. **Artifact stripping** — regex patterns for "Skip to content", GitHub
   chrome, duplicate lines

## Run locally

```bash
uv sync
uv run dendrite-scraper
```

## Run with Docker

```bash
docker compose up -d
```

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | No | Enables gpt-4o-mini content cleanup |
| `DENDRITE_PORT` | No | Server port (default: 8020) |
| `DENDRITE_HOST` | No | Bind address (default: 0.0.0.0) |
| `DENDRITE_CRAWL_TIMEOUT_SECONDS` | No | Crawl4AI timeout (default: 25) |

## Use as a library

```python
from dendrite_scraper.scraper import scrape

result = await scrape("https://example.com")
print(result.markdown)
```

## Use as a service

All consumers need one env var:

```bash
DENDRITE_SCRAPER_URL=http://localhost:8020
```

```python
import httpx

resp = httpx.post(f"{DENDRITE_SCRAPER_URL}/scrape", json={"url": url})
data = resp.json()
markdown = data["markdown"]
```

## Tests

```bash
uv run pytest tests/ -v
```

30 tests, all mocked — no network required.

## Port

8020 (registered in the shared port table).

## License

MIT
