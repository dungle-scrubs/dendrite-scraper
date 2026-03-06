"""Entry point for `python -m web_scraper` or `uv run web-scraper`."""

import uvicorn

from web_scraper.settings import settings


def main() -> None:
    """Start the uvicorn server."""
    uvicorn.run(
        "web_scraper.server:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
