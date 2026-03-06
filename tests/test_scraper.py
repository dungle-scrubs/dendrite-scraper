"""Unit tests for the scraping pipeline.

All tests mock external calls (crawl4ai, Jina, OpenAI) — no network required.
"""

from web_scraper.scraper import (
    clean_markdown_content,
    is_scrape_artifact_line,
    looks_like_bot_block,
    looks_noisy,
)

# ── is_scrape_artifact_line ──────────────────────────────────


class TestIsScrapeArtifactLine:
    """Tests for scrape artifact line detection."""

    def test_skip_to_content_link(self) -> None:
        assert is_scrape_artifact_line("[Skip to content](/main)")

    def test_dismiss_alert(self) -> None:
        assert is_scrape_artifact_line("Dismiss alert")

    def test_mustache_template(self) -> None:
        assert is_scrape_artifact_line("{{ message }}")

    def test_github_signed_in(self) -> None:
        assert is_scrape_artifact_line("You signed in with another tab or window.")

    def test_github_signed_out(self) -> None:
        assert is_scrape_artifact_line("You signed out in another tab or window.")

    def test_normal_content_not_artifact(self) -> None:
        assert not is_scrape_artifact_line("This is normal documentation content.")

    def test_empty_line_not_artifact(self) -> None:
        assert not is_scrape_artifact_line("")

    def test_whitespace_only_not_artifact(self) -> None:
        assert not is_scrape_artifact_line("   ")


# ── clean_markdown_content ───────────────────────────────────


class TestCleanMarkdownContent:
    """Tests for markdown content cleaning."""

    def test_removes_artifacts(self) -> None:
        raw = "[Skip to content](/main)\n\n# Hello\n\nWorld\n"
        cleaned = clean_markdown_content(raw)
        assert "[Skip to content]" not in cleaned
        assert "# Hello" in cleaned
        assert "World" in cleaned

    def test_collapses_duplicate_lines(self) -> None:
        raw = "# Title\n# Title\n\nContent\n"
        cleaned = clean_markdown_content(raw)
        assert cleaned.count("# Title") == 1

    def test_collapses_excessive_newlines(self) -> None:
        raw = "# Title\n\n\n\n\n\nContent\n"
        cleaned = clean_markdown_content(raw)
        assert "\n\n\n" not in cleaned

    def test_empty_input(self) -> None:
        assert clean_markdown_content("") == ""

    def test_preserves_code_blocks(self) -> None:
        raw = "# Code\n\n```python\ndef hello():\n    pass\n```\n"
        cleaned = clean_markdown_content(raw)
        assert "```python" in cleaned
        assert "def hello():" in cleaned

    def test_trailing_newline(self) -> None:
        cleaned = clean_markdown_content("Hello")
        assert cleaned.endswith("\n")


# ── looks_like_bot_block ─────────────────────────────────────


class TestLooksLikeBotBlock:
    """Tests for bot protection detection."""

    def test_cloudflare_challenge(self) -> None:
        page = "Just a moment...\nPerforming security verification\nPlease wait"
        assert looks_like_bot_block(page)

    def test_captcha_page(self) -> None:
        page = "Please complete the CAPTCHA to continue"
        assert looks_like_bot_block(page)

    def test_access_denied(self) -> None:
        page = "Access denied. You don't have permission."
        assert looks_like_bot_block(page)

    def test_long_page_not_bot_block(self) -> None:
        """Long pages with bot phrases are real content, not challenge pages."""
        page = "Cloudflare is a company\n" * 200
        assert not looks_like_bot_block(page)

    def test_partial_js_render(self) -> None:
        """Tables with many empty pipe cells indicate partial JS rendering."""
        rows = "| Name | Score | Status |\n|---|---|---|\n"
        rows += "| |  | |\n" * 10  # Empty data rows
        assert looks_like_bot_block(rows)

    def test_real_table_not_flagged(self) -> None:
        rows = "| Name | Score |\n|---|---|\n"
        rows += "| Alice | 95 |\n| Bob | 87 |\n"
        assert not looks_like_bot_block(rows)

    def test_normal_content(self) -> None:
        page = "# Welcome\n\nThis is a real page with actual content.\n" * 20
        assert not looks_like_bot_block(page)


# ── looks_noisy ──────────────────────────────────────────────


class TestLooksNoisy:
    """Tests for noise detection heuristic."""

    def test_link_heavy_header(self) -> None:
        """Dense links in the first 2000 chars indicates nav chrome."""
        lines = [f"[Link {i}](https://example.com/{i})" for i in range(50)]
        markdown = "\n".join(lines) + "\n\n# Real Content\n\nParagraph here.\n"
        assert looks_noisy(markdown)

    def test_clean_documentation(self) -> None:
        """Normal docs with few links should not be flagged."""
        lines = [f"Paragraph {i} with some text about things." for i in range(50)]
        lines.insert(5, "[one link](https://example.com)")
        markdown = "\n".join(lines)
        assert not looks_noisy(markdown)

    def test_empty_content(self) -> None:
        assert not looks_noisy("")
