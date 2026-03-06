FROM python:3.13-slim

# Playwright system deps for crawl4ai's headless Chromium.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

# Install uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (layer caching).
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy source.
COPY src/ src/

# Install the package.
RUN uv sync --no-dev --frozen

# Install Playwright browsers for crawl4ai.
RUN uv run crawl4ai-setup 2>/dev/null || uv run python -m playwright install chromium 2>/dev/null || true

EXPOSE 8020

CMD ["uv", "run", "dendrite-scraper"]
