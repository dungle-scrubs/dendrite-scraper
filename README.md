# web-scraper

Standalone web scraping service with anti-bot detection, Jina fallback,
and optional LLM cleanup. Runs independently — consumed by tool-proxy,
tallow, and marrow over HTTP.

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
uv run web-scraper
```

## Run with Docker

```bash
docker compose up -d
```

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | No | Enables gpt-4o-mini content cleanup |
| `WEB_SCRAPER_PORT` | No | Server port (default: 8020) |
| `WEB_SCRAPER_HOST` | No | Bind address (default: 0.0.0.0) |
| `WEB_SCRAPER_CRAWL_TIMEOUT_SECONDS` | No | Crawl4AI timeout (default: 25) |

## Consumer setup

All consumers need one env var:

```bash
WEB_SCRAPER_URL=http://localhost:8020
```

### tool-proxy

Replace `scrape_url()` in `apps/docs/scripts/docs_session.py` with:

```python
resp = httpx.post(f"{WEB_SCRAPER_URL}/scrape", json={"url": url})
data = resp.json()
return data["markdown"], bool(data["error"])
```

### tallow / marrow

```typescript
const resp = await fetch(`${process.env.WEB_SCRAPER_URL}/scrape`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ url }),
});
const { markdown, error } = await resp.json();
```

## Tests

```bash
uv run pytest tests/ -v
```

30 tests, all mocked — no network required.

## Port

8020 (registered in the shared port table).
