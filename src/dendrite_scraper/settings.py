"""Application settings resolved from environment variables.

OPENAI_API_KEY is optional — when set, scraped markdown is post-processed
through gpt-4o-mini for cleaner content extraction.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Dendrite scraper service configuration.

    @param openai_api_key: Optional OpenAI key for gpt-4o-mini cleanup pass.
    @param host: Bind address for the HTTP server.
    @param port: Bind port for the HTTP server.
    @param crawl_timeout_seconds: Per-URL crawl4ai timeout.
    @param jina_timeout_seconds: Per-URL Jina Reader timeout.
    @param llm_clean_timeout_seconds: gpt-4o-mini request timeout.
    @param llm_clean_max_input_chars: Truncation limit for LLM cleanup input.
    @param max_retries: Crawl4AI retry attempts on transient errors.
    @param retry_delay_seconds: Delay between retries.
    """

    openai_api_key: str | None = None
    host: str = "0.0.0.0"
    port: int = 8020
    crawl_timeout_seconds: int = 25
    jina_timeout_seconds: int = 30
    llm_clean_timeout_seconds: int = 90
    llm_clean_max_input_chars: int = 80_000
    max_retries: int = 2
    retry_delay_seconds: float = 1.0

    model_config = {"env_prefix": "DENDRITE_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
