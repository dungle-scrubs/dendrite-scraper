"""Core scraping pipeline: crawl4ai → bot detection → Jina fallback → artifact stripping.

This module is the entire reason this service exists. It wraps crawl4ai with
multi-layer resilience and content cleaning that callers shouldn't have to
reimplement.

Pipeline:
  1. Crawl4AI (local Playwright) with retry on transient errors
  2. Bot detection — Cloudflare/CAPTCHA phrases + partial JS render heuristic
  3. Jina Reader fallback — free cloud re-fetch when bot-blocked or crawl fails
  4. Artifact stripping — regex patterns for common scraper artifacts
"""

import asyncio
import contextlib
import logging
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from scraper.safety import (
    UrlRejected,
    validate_url_async,
)
from scraper.settings import settings

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────

JINA_READER_PREFIX = "https://r.jina.ai/"

# Crawl errors that should not be retried (vs. transient failures). Matched by
# prefix against crawl_url's own messages — not arbitrary page content.
NON_RETRYABLE_CRAWL_PREFIXES = (
    "crawl4ai is not installed",
    "Crawl timed out",
    "Blocked:",
)

BOT_DETECTION_PHRASES = (
    "performing security verification",
    "verifies you are not a bot",
    # Tightened from bare "cloudflare"/"captcha" (F8): those are common words
    # that appear in legitimate content. These phrases name the challenge page
    # itself, so a real doc merely discussing the provider no longer trips it.
    "checking your browser before accessing",
    "captcha to continue",
    "complete the captcha",
    "enable javascript to continue",
    "this page was lost in training",
    "access denied",
    "just a moment",
)

# Strong, specific bot-challenge signals. Any one of these is decisive on its
# own (F8): these strings only appear on an actual challenge/interstitial page,
# so they flag a block regardless of heading presence or page length - a real
# doc discussing a provider never emits a "cf-browser-verification" token or a
# rendered "attention required! | cloudflare" heading.
STRONG_BOT_SIGNALS = (
    "cloudflare ray id",
    "cf-browser-verification",
    "attention required! | cloudflare",
)

# Challenge-page heuristic thresholds (see looks_like_bot_block). Requiring all
# three (phrase + no heading + short) avoids false positives on legitimate docs
# that merely mention a provider name like "cloudflare".
BOT_CHALLENGE_MAX_CHARS = 1500
BOT_CHALLENGE_MAX_WORDS = 150

# Partial-JS-render heuristic thresholds. A markdown table row with empty cells
# between the pipes (e.g. "|  |  |") signals the DOM rendered but the data did
# not. Whitespace inside the cells is collapsed before matching so tabs / varied
# padding don't defeat detection. Requiring a minimum count of empty-cell rows
# AND that empty cells DOMINATE the table (BOT_EMPTY_CELL_MIN_RATIO of all pipe
# rows) - plus the absence of a heading / substantive prose - keeps a legit 6+
# row comparison table with a couple of blanks from being flagged (F3).
BOT_EMPTY_CELL_MIN_ROWS = 5
BOT_EMPTY_CELL_MIN_RATIO = 0.7
# Words of non-table, non-heading text above which a page is treated as real
# content rather than a blank rendered table (F3).
BOT_TABLE_PROSE_MIN_WORDS = 25
_EMPTY_CELL_RE = re.compile(r"\|\s*\|")

SCRAPE_ARTIFACT_LINE_PATTERNS = (
    re.compile(r"^\[Skip to content\]\([^)]*\)$", re.IGNORECASE),
    re.compile(r"^Dismiss alert$", re.IGNORECASE),
    re.compile(r"^\{\{\s*message\s*\}\}$", re.IGNORECASE),
)

SCRAPE_ARTIFACT_LINE_SUBSTRINGS = (
    "You signed in with another tab or window.",
    "You signed out in another tab or window.",
    "You switched accounts on another tab or window.",
)


# ── Result types ─────────────────────────────────────────────


@dataclass
class ScrapeResult:
    """Outcome of the scraping pipeline.

    @param markdown: Cleaned markdown content (empty string on failure).
    @param source: Which backend produced the content.
    @param url: The URL that was scraped.
    @param bot_detected: Whether bot protection was detected on the initial crawl.
    @param error: Error message if the scrape failed entirely.
    @param elapsed_ms: Wall-clock time for the full pipeline.
    @param attempts: Log of what was tried.
    """

    markdown: str = ""
    source: str = "none"
    url: str = ""
    bot_detected: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0
    attempts: list[str] = field(default_factory=list)


# ── Resource bounding ────────────────────────────────────────

# Depth-counted so concurrent crawls in one event loop don't corrupt sys.stdout:
# only the outermost entry saves/restores the real stream (a naive save/restore
# per call would capture another in-flight call's already-swapped stream).
#
# Atomicity note: the depth counter is mutated without a lock. This is safe in
# practice because `crawl_url` is the only caller and it holds the body of this
# context manager synchronously (no `await` between the depth change and the
# `yield`). A truly interleaving re-entry across separate coroutines is not a
# supported usage; if one is ever introduced, guard with a per-task structure
# (e.g. contextvars) instead of a module-level counter.
_stdout_suppress_depth = 0
_stdout_real: Any = None


@contextlib.contextmanager
def _suppressed_stdout() -> Iterator[None]:
    """Silence direct stdout writes (crawl4ai progress bars) — concurrency-safe.

    Safe under nested re-entry from the same call stack (see the atomicity note
    on the module-level counter above).

    @returns: Context manager that routes stdout to stderr for its duration.
    """
    global _stdout_suppress_depth, _stdout_real
    if _stdout_suppress_depth == 0:
        _stdout_real = sys.stdout
        sys.stdout = sys.stderr
    _stdout_suppress_depth += 1
    try:
        yield
    finally:
        _stdout_suppress_depth -= 1
        if _stdout_suppress_depth == 0:
            sys.stdout = _stdout_real
            _stdout_real = None


def _is_non_retryable_crawl_error(message: str) -> bool:
    """Return True for crawl errors that retrying cannot fix.

    @param message: Error message returned by `crawl_url`.
    @returns: True when the crawl should not be retried.
    """
    return message.startswith(NON_RETRYABLE_CRAWL_PREFIXES)


def _cap_markdown(markdown: str) -> str:
    """Bound markdown length before it reaches the heuristics and cleaners.

    @param markdown: Raw markdown from any source.
    @returns: Markdown truncated to `settings.max_markdown_chars`.
    """
    cap = settings.max_markdown_chars
    return markdown if len(markdown) <= cap else markdown[:cap]


# ── Artifact stripping ───────────────────────────────────────


def is_scrape_artifact_line(line: str) -> bool:
    """Return True when a markdown line matches known scraper noise patterns.

    @param line: Single line of markdown text.
    @returns: True if this line is a scraper artifact that should be removed.
    """
    stripped = line.strip()
    if not stripped:
        return False

    if any(pattern.match(stripped) for pattern in SCRAPE_ARTIFACT_LINE_PATTERNS):
        return True

    return any(fragment in stripped for fragment in SCRAPE_ARTIFACT_LINE_SUBSTRINGS)


def clean_markdown_content(markdown: str) -> str:
    """Remove known scrape artifacts and normalize markdown whitespace.

    Fenced code blocks are emitted verbatim: while inside a fence, artifact
    stripping, adjacent-duplicate collapsing, and blank-run collapsing are all
    suspended so legitimate code (repeated `print(1)` lines, stray `}`, identical
    config lines, intentional blank lines) survives intact. A fence toggles on any
    line whose stripped form starts with a code fence marker (``` or ~~~). Only
    whole-document leading/trailing whitespace is trimmed.

    @param markdown: Raw markdown from any scraping source.
    @returns: Cleaned markdown with artifacts removed and whitespace normalized.
    """
    cleaned_lines: list[str] = []
    in_code_fence = False

    for line in markdown.splitlines():
        normalized = line.rstrip()

        stripped = normalized.lstrip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")

        # Inside a fence, emit lines verbatim - no artifact stripping, no dedup.
        if in_code_fence:
            cleaned_lines.append(normalized)
            if is_fence:
                in_code_fence = False
            continue

        if is_fence:
            in_code_fence = True
            cleaned_lines.append(normalized)
            continue

        if is_scrape_artifact_line(normalized):
            continue

        # Collapse duplicate adjacent lines from page chrome extraction.
        if normalized and cleaned_lines and normalized == cleaned_lines[-1]:
            continue

        # Collapse blank-line runs to a single blank line. Done here (outside
        # fences) rather than with a whole-string regex so intentional blank
        # lines inside code blocks - emitted verbatim above - are preserved.
        # Also drops leading blank lines; trailing ones are trimmed below.
        if not normalized and (not cleaned_lines or cleaned_lines[-1] == ""):
            continue

        cleaned_lines.append(normalized)

    cleaned = "\n".join(cleaned_lines).strip()
    return f"{cleaned}\n" if cleaned else ""


# ── Bot detection ────────────────────────────────────────────


def looks_like_bot_block(markdown: str) -> bool:
    """Return True when scraped markdown looks like a bot-protection or failed-render page.

    Detects two failure modes:
    1. Cloudflare/CAPTCHA challenge pages (short, contains bot-protection phrases).
    2. Partial JS rendering — page structure loaded but dynamic data is missing,
       e.g. table rows with blank cells where content should be.

    @param markdown: Markdown from crawl4ai.
    @returns: True if the content should be re-fetched via Jina Reader.
    """
    lower = markdown.lower()

    # Strong, specific signals are decisive on their own (F8): these strings only
    # appear on an actual challenge/interstitial page, so they flag a block
    # regardless of the heading / length gating that governs the weaker phrases.
    if any(signal in lower for signal in STRONG_BOT_SIGNALS):
        return True

    has_heading = any(line.lstrip().startswith("#") for line in markdown.splitlines())

    # Challenge pages are short, heading-less, and terse. Requiring all three
    # avoids false positives on legitimate short docs that merely mention a
    # provider name like "cloudflare".
    has_phrase = any(phrase in lower for phrase in BOT_DETECTION_PHRASES)
    word_count = len(lower.split())
    if (
        has_phrase
        and not has_heading
        and len(markdown) < BOT_CHALLENGE_MAX_CHARS
        and word_count < BOT_CHALLENGE_MAX_WORDS
    ):
        return True

    # Partial JS rendering: rows with many empty cells between pipes. Cells are
    # matched after collapsing internal whitespace so tabs / varied padding
    # don't slip past the old "|  |" / "| |" substring check. To avoid flagging a
    # legit comparison table that carries a few blanks (F3), require BOTH a
    # minimum count of empty-cell rows AND that empty cells DOMINATE the table
    # (>= BOT_EMPTY_CELL_MIN_RATIO of all pipe rows), AND that the page has no
    # heading or substantive prose anchoring it as real content.
    pipe_rows = [line for line in markdown.splitlines() if "|" in line and "---" not in line]
    if pipe_rows:
        empty_cell_rows = sum(1 for row in pipe_rows if _EMPTY_CELL_RE.search(row))
        dominates = empty_cell_rows >= BOT_EMPTY_CELL_MIN_RATIO * len(pipe_rows)
        prose_words = sum(
            len(line.split())
            for line in markdown.splitlines()
            if line.strip() and "|" not in line and not line.lstrip().startswith("#")
        )
        has_substantive_prose = prose_words >= BOT_TABLE_PROSE_MIN_WORDS
        if (
            empty_cell_rows > BOT_EMPTY_CELL_MIN_ROWS
            and dominates
            and not has_heading
            and not has_substantive_prose
        ):
            return True

    return False


# ── Crawl4AI ─────────────────────────────────────────────────


async def _guard_route(route: Any) -> None:
    """Playwright route handler: abort any in-browser request to a blocked host.

    Fires on every request the page makes, including redirect hops, so a public
    URL that 3xx-redirects toward an internal/metadata host is aborted rather
    than followed. Uses `validate_url_async` (thread-offloaded) so the blocking
    `getaddrinfo` can't stall the event loop — this handler runs once per
    sub-resource request, so a sync call would freeze the loop on DNS-heavy pages.

    Interception contract: this only sees requests that crawl4ai/Playwright route
    through `page.route("**/*", ...)`. HTTP 3xx responses followed at the fetch
    layer (not surfaced as a new Playwright navigation/request event) would not
    re-enter this guard; the per-navigation hop counter and the initial pre-flight
    validation in `crawl_url` are the remaining defenses. The fail-loud
    `set_hook` check in `crawl_url` guarantees the guard is actually attached.

    @param route: Playwright route for the intercepted request.
    """
    try:
        await validate_url_async(route.request.url)
    except UrlRejected:
        await route.abort()
        return
    await route.continue_()


def _make_route_guard() -> Any:
    """Build a per-page route handler that host-validates and caps redirect hops.

    Each page context gets a fresh handler with its own navigation counter so a
    redirect loop to public hosts can't spin forever (DoS), on top of the per-hop
    host validation that closes SSRF.

    @returns: An async Playwright route handler.
    """
    nav_count = 0

    async def guard(route: Any) -> None:
        nonlocal nav_count
        is_nav = getattr(route.request, "is_navigation_request", None)
        if callable(is_nav) and is_nav():
            nav_count += 1
            # Allow the initial navigation plus `max_redirects` hops.
            if nav_count > settings.max_redirects + 1:
                await route.abort()
                return
        await _guard_route(route)

    return guard


async def _install_route_guard(*args: Any, **kwargs: Any) -> Any:
    """crawl4ai `on_page_context_created` hook: attach the route guard to the page.

    Locates the Playwright page among the hook arguments (signatures vary across
    crawl4ai versions) and installs a fresh counting route guard on all requests.

    Fail loud (F4): if no page-like object is found among the hook arguments,
    raise rather than crawl without the guard — a signature shift that still
    delivers a strategy but passes the page differently would otherwise reopen a
    redirect-hop SSRF bypass.

    @param args: Positional hook arguments.
    @param kwargs: Keyword hook arguments.
    @returns: The page object (crawl4ai expects the page back).
    @throws RuntimeError: When no page-like object is found in the hook args.
    """
    candidates = [*args, *kwargs.values()]
    page = next((c for c in candidates if hasattr(c, "route") and hasattr(c, "goto")), None)
    if page is None:
        raise RuntimeError(
            "crawl4ai on_page_context_created delivered no page; in-browser SSRF "
            "route guard cannot be attached"
        )
    await page.route("**/*", _make_route_guard())
    return page


async def crawl_url(url: str) -> tuple[str, bool]:
    """Crawl a single URL with Crawl4AI and return (markdown, is_error).

    Suppresses crawl4ai's stdout pollution (progress bars bypass Python logging
    and corrupt JSON protocols in subprocess mode).

    @param url: URL to scrape.
    @returns: Tuple of (content_or_error_message, is_error).
    """
    # Pre-flight SSRF guard: reject before launching a browser at all.
    # Offloaded to a thread so the blocking getaddrinfo can't stall the loop.
    # Capture the sanitized target so the browser navigates the userinfo-stripped,
    # host-normalized URL rather than the raw input (matches jina_fetch).
    try:
        target = await validate_url_async(url)
    except UrlRejected as exc:
        return f"Blocked: {exc.reason}", True

    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except ImportError:
        return "crawl4ai is not installed. Run: pip install crawl4ai && crawl4ai-setup", True

    # Suppress crawl4ai's own logging.
    for logger_name in ("crawl4ai", "crawl4ai.async_webcrawler", "crawl4ai.async_crawler_strategy"):
        logging.getLogger(logger_name).setLevel(logging.CRITICAL)

    browser_config = BrowserConfig(headless=True, verbose=False)
    run_config = CrawlerRunConfig(
        word_count_threshold=settings.crawl_word_count_threshold,
        exclude_external_links=True,
        excluded_tags=["nav", "footer", "header", "aside"],
        process_iframes=False,
        page_timeout=settings.crawl_page_timeout_ms,
        delay_before_return_html=settings.crawl_render_delay_seconds,
    )

    # Suppress crawl4ai's direct stdout writes (progress bars) for the crawl's
    # duration without corrupting stdout across concurrent requests.
    try:
        with _suppressed_stdout():
            async with AsyncWebCrawler(config=browser_config) as crawler:
                # Re-validate every in-browser request/redirect hop (defeats
                # redirect bypass; covers what the initial pre-flight cannot).
                # Fail loud (F4): if the crawl4ai internal API shifted and we
                # can't attach the guard, raise rather than silently fail open —
                # an unattached guard reopens a redirect-hop SSRF bypass.
                strategy = getattr(crawler, "crawler_strategy", None)
                if strategy is None:
                    raise RuntimeError(
                        "crawl4ai crawler_strategy not found; in-browser SSRF "
                        "route guard cannot be attached"
                    )
                if not hasattr(strategy, "set_hook"):
                    raise RuntimeError(
                        "crawl4ai crawler_strategy has no set_hook; in-browser SSRF "
                        "route guard cannot be attached"
                    )
                strategy.set_hook("on_page_context_created", _install_route_guard)

                result = await asyncio.wait_for(
                    crawler.arun(url=target.url, config=run_config),
                    timeout=settings.crawl_timeout_seconds,
                )
    except TimeoutError:
        return f"Crawl timed out after {settings.crawl_timeout_seconds}s", True

    if not result.success:
        return f"Crawl failed: {result.error_message}", True

    markdown = result.markdown or ""
    if not markdown.strip():
        return "No markdown content returned", True

    return _cap_markdown(markdown), False


# ── Jina Reader fallback ─────────────────────────────────────


async def jina_fetch(url: str) -> tuple[str, bool]:
    """Fetch a URL via Jina Reader (free, no auth required for <20 req/min).

    Jina Reader runs headless Chromium on Google Cloud and bypasses most
    bot-protection. Used as a fallback when crawl4ai hits Cloudflare/CAPTCHA.

    @param url: URL to fetch.
    @returns: Tuple of (content_or_error_message, is_error).
    """
    # Validate the target and strip any userinfo before it leaves to the third party.
    # Offloaded to a thread so the blocking getaddrinfo can't stall the loop.
    try:
        target = await validate_url_async(url)
    except UrlRejected as exc:
        return f"Jina target blocked: {exc.reason}", True

    cap = settings.jina_max_bytes
    try:
        async with (
            httpx.AsyncClient() as client,
            client.stream(
                "GET",
                f"{JINA_READER_PREFIX}{target.url}",
                headers={"Accept": "text/markdown"},
                timeout=settings.jina_timeout_seconds,
                follow_redirects=False,
            ) as resp,
        ):
            # Fast reject by declared length before reading any body.
            declared = resp.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > cap:
                return f"Jina Reader response too large ({declared} bytes)", True

            if resp.status_code != 200:
                # Log upstream detail; do not reflect the body to the caller.
                body = await resp.aread()
                logger.warning(
                    "Jina Reader %d for %s: %s", resp.status_code, target.host, body[:200]
                )
                return f"Jina Reader error {resp.status_code}", True

            # Stream the body, aborting once it exceeds the byte cap (handles
            # chunked responses with no declared Content-Length).
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > cap:
                    return f"Jina Reader response too large (>{cap} bytes)", True
                chunks.append(chunk)
    except httpx.HTTPError:
        # Log the library detail server-side; do not reflect it to the caller -
        # raw transport exception text can leak internal host/proxy details (M10).
        logger.warning("Jina Reader transport error for %s", target.host, exc_info=True)
        return "Jina Reader failed", True

    markdown = b"".join(chunks).decode("utf-8", errors="replace")
    if not markdown.strip():
        return "Jina Reader returned empty content", True

    return _cap_markdown(markdown), False


# ── Pipeline orchestration ───────────────────────────────────


async def scrape(url: str) -> ScrapeResult:
    """Full scraping pipeline: crawl4ai → bot detect → Jina fallback → artifacts.

    This is the single entry point callers should use.

    @param url: URL to scrape.
    @returns: ScrapeResult with cleaned markdown or error details.
    """
    start = time.monotonic()
    result = ScrapeResult(url=url)

    # ── Step 0: SSRF guard — reject internal/special targets up front ──
    # Non-blocking (thread-offloaded) so DNS can't stall the event loop.
    try:
        await validate_url_async(url)
    except UrlRejected as exc:
        result.error = f"URL rejected: {exc.reason}"
        result.attempts.append(f"blocked: {exc.reason}")
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # ── Step 1: Crawl4AI with retries ────────────────────────
    crawl_error: str | None = None
    for attempt in range(settings.max_retries):
        result.attempts.append(f"crawl4ai attempt {attempt + 1}")
        try:
            markdown, err = await crawl_url(url)
        except Exception:
            # Log the full traceback server-side; return a generic message so raw
            # library exception text never reaches the caller (M10).
            logger.exception("Scrape crashed on attempt %d for %s", attempt + 1, url)
            crawl_error = "Scrape crashed"
            if attempt < settings.max_retries - 1:
                await asyncio.sleep(settings.retry_delay_seconds)
            continue

        if err:
            crawl_error = markdown
            # Don't retry non-transient errors (missing dep, timeout, blocked URL).
            if _is_non_retryable_crawl_error(markdown):
                break
            if attempt < settings.max_retries - 1:
                await asyncio.sleep(settings.retry_delay_seconds)
            continue

        # Success — check for bot block.
        if looks_like_bot_block(markdown):
            crawl_error = "Bot protection detected"
            result.bot_detected = True
            result.attempts.append("bot protection detected")
            break

        # Clean crawl4ai result.
        cleaned = clean_markdown_content(markdown)

        result.markdown = cleaned
        result.source = "crawl4ai"
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # ── Step 2: Jina Reader fallback (opt-in third-party egress) ──
    if not settings.jina_enabled:
        result.attempts.append("jina disabled")
        result.error = crawl_error
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    result.attempts.append("jina fallback")
    jina_md, jina_err = await jina_fetch(url)
    if not jina_err:
        cleaned = clean_markdown_content(jina_md)

        result.markdown = cleaned
        result.source = "jina"
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # ── Both failed ──────────────────────────────────────────
    # Jina ran last and its failure is the most relevant to the caller, so
    # surface Jina's own error; fall back to the crawl error only when Jina
    # produced no message (F9).
    result.error = jina_md or crawl_error
    result.elapsed_ms = (time.monotonic() - start) * 1000
    return result
