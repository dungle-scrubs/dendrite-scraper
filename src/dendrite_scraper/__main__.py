"""Entry point for `python -m dendrite_scraper` or `uv run dendrite-scraper`."""

import uvicorn

from dendrite_scraper.settings import settings


def main() -> None:
    """Start the uvicorn server."""
    uvicorn.run(
        "dendrite_scraper.server:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
