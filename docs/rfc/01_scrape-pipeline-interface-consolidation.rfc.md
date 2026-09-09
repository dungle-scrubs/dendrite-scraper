---
number: 01
title: "Scrape pipeline interface consolidation"
type: refactor
status: Draft
author: Kevin Frilot
date: 2026-09-05
---

# RFC-01: Scrape pipeline interface consolidation

## Abstract

The scrape pipeline exposes one product contract (a URL in, a seven-field result out) on three surfaces: the HTTP API, the `scraper` CLI, and the library call. Today that contract, the SSRF pre-flight, the global deadline, and the fetcher error vocabulary are each owned by two to four places, kept in sync by hand-written tests and README text. One of them has already drifted: the CLI prints a three-key object on invalid input while the README promises eight keys. This RFC gives each of those concerns one owner. `ScrapeResult` becomes the single result model, `scrape` becomes the single place a URL is validated and the single owner of the deadline, fetcher failures become typed, the browser route guard moves next to the SSRF policy, and the settings surface, the container healthcheck, and the concurrency cap stop restating values by hand. Scope is the eight findings of the 2026-09-05 codebase audit. No security control is removed; each is named with its new location.

## Introduction

### Problem statement

The 2026-09-05 codebase audit (both smell families, whole repository) found eight places where one domain concept has several owners or where an interface makes every caller repeat the module's protocol. In ranked order:

1. The scrape response contract is defined four times and the CLI's invalid-input path already violates it.
2. A URL is validated and DNS-resolved two to three times per request before the browser launches. The invariant "validate before fetch" lives in a docstring, not a type. The fail-closed validation timeout is set at two of six call sites.
3. The global deadline is implemented twice with the same message and the default 120 is written in five places.
4. Fetcher failures are strings, and retry policy is decided by matching their prefixes.
5. Eighteen settings are described by hand in a docstring; seven of them are in the README.
6. The SSRF trust boundary is split across `safety.py` (policy) and `scraper.py` (browser enforcement), and its internal cache is a function parameter so tests can reach it.
7. The container healthcheck hardcodes port 8020 while the server reads it from settings.
8. The concurrency semaphore is sized from settings at module import, so the knob is honored only if set before import.

### Scope

In scope: every finding above, as changes P1 through P8 in Proposed Changes.

Out of scope, with the reason each was declined:

- Re-auditing or changing the SSRF policy in `safety.py`, the request body cap middleware, the API-key check, or the bind-safety guard. These are deep modules and the security remediation (#15, #16) settled them. This RFC moves the browser route guard and preserves every other control in place.
- A shared browser or context pool instead of one Chromium per request. Performance work, not interface work.
- Renaming `SCRAPER_SERVER_TIMEOUT_SECONDS`. See Open Question 1.
- Re-exporting `scrape` from the package root, the CLI's reflection of internal exception text, the pre-rename names in `.plans/`, and the CONTRIBUTING checklist. The audit rejected these as naming, per-surface policy, or documentation with no behavior behind it.

Fit check on the changes that touch product behavior. Users: operators running the server locally or in the container, CLI consumers that parse stdout, and Python callers importing `scrape`. Purpose, from README.md: a web scraping service with anti-bot detection and Jina fallback, exposed as a server and a CLI with a stable JSON contract. Each behavior change below serves that purpose and holds scope; none widens it:

- CLI exit-2 output gains the five missing keys. This fulfils the contract the README already states.
- The CLI `--timeout` default reads the same setting the server uses. One deadline concept, one default.
- `ScrapeResult.url` becomes the sanitized URL that was fetched. The README defines the field as "the URL that was scraped".
- The container healthcheck honors `SCRAPER_PORT`. Operator-facing correctness.

### Motivation

The CLI contract drift is live today and shipped in 0.2.0. The other seven are latent: each is a place where the next change has to be made in several files, and the tests that pin the duplicates are the only thing that catches a miss. The security remediation added the guard, the deadline, the caps, and the semaphore quickly and correctly. This RFC is the consolidation pass that follows.

### Context

- The audit report was delivered in chat on 2026-09-05 and is restated with its evidence in Current State.
- `.plans/security-remediation/rfc.md` and `progress-report.md` record the controls this RFC has to preserve.
- Vocabulary follows the codebase-design skill: module, interface, seam, depth, adapter.

## Terminology

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED, MAY, and OPTIONAL in this document are to be interpreted as described in RFC 2119.

- **Surface**: a place the product is used or read. The HTTP API, the CLI, the library call, the README, the Dockerfile, docker-compose.yml, and CI.
- **Pipeline**: the `scrape` function in `src/scraper/scraper.py` and everything it calls.
- **Fetcher**: `crawl_url` (Playwright through crawl4ai) or `jina_fetch` (Jina Reader over httpx). A fetcher turns a target into raw markdown.
- **Guard**: `validate_url` and `validate_url_async` in `src/scraper/safety.py`. The guard owns the allow-or-block decision for a URL.
- **ValidatedTarget**: the frozen dataclass the guard returns. It carries the sanitized URL, scheme, host, port, and the validated addresses.
- **Route guard**: the Playwright request handler that re-validates every in-browser request and caps navigation hops.
- **Boundary**: a place untrusted input enters. The HTTP endpoint, the CLI argument or stdin, and the library call.
- **Deadline**: the wall-clock limit on one full pipeline run.
- **Result model**: the seven-field object every surface returns: `markdown`, `source`, `url`, `bot_detected`, `error`, `elapsed_ms`, `attempts`.

## Current State

### Module map

```
src/scraper/
  safety.py    SSRF policy: validate_url, validate_url_async, classify_ip, ValidatedTarget, UrlRejected
  scraper.py   pipeline: scrape; fetchers crawl_url, jina_fetch; heuristics; route guard (4 functions)
  server.py    FastAPI: POST /scrape, GET /health, body cap, API key, bind safety, semaphore
  cli.py       argparse: scrape, serve; exit codes; two JSON serializers
  settings.py  pydantic-settings, 18 fields, SCRAPER_ prefix
```

Both surfaces call `scrape(url: str) -> ScrapeResult`. Both validate the URL before calling it. `scrape` validates again, then each fetcher validates again.

### Findings and evidence

**F1. Result contract, four owners.** `src/scraper/scraper.py:110-128` (dataclass), `src/scraper/server.py:234-252` (pydantic twin; docstring at 237-243 restates 113-119), `src/scraper/server.py:313-321` (field-by-field copy), `src/scraper/cli.py:41-50` (asdict plus `ok`). README.md:19-30 and :57-68 write the shape by hand twice. `tests/test_server.py:49-57`, `:79-87` and `tests/test_cli.py:68-77` pin the key set. `src/scraper/cli.py:53-60` is a second serializer that emits three keys. README.md:68-71 says the structure is always the same. The real CLI on every exit-2 path prints only `ok`, `error`, `url`:

```
$ scraper scrape ftp://example.com
{"ok": false, "error": "Invalid or blocked URL (scheme-not-allowed: ftp): ftp://example.com", "url": "ftp://example.com"}
```

**F2. Validation at every layer.** Guard call sites: `src/scraper/server.py:299`, `src/scraper/cli.py:99`, `src/scraper/scraper.py:608`, `:462`, `:540`, and the per-hop route guard at `:382`. A raw `url: str` passes endpoint to pipeline to fetcher and is parsed and resolved again at each frame. Resolver calls per request with a fake crawler, measured by script:

| Entry path | Resolutions before the browser launches |
|---|---|
| library `scrape()` | 2 |
| server `POST /scrape` | 3 |
| CLI `scrape <url>` | 3 |

The pipeline's own check at `:608` is redundant: `crawl_url` blocks at `:462-464` with a `Blocked:` message that `:41-45` and `:633-634` treat as non-retryable. `validate_timeout_seconds` is passed at `server.py:299` and `scraper.py:382` and omitted at the other four sites. The invariant is prose at `src/scraper/safety.py:3-5`.

**F3. Deadline, two owners.** `src/scraper/cli.py:108-124` and `src/scraper/server.py:303-309` each wrap `scrape` in a deadline and build the same `Global timeout exceeded` result (`cli.py:118`, `server.py:308`). The default 120 appears at `cli.py:35`, `settings.py:38`, README.md:86, README.md:125, and `tests/test_settings.py:22`. The CLI does not read settings for its default.

**F4. Stringly-typed fetch failures.** Both fetchers return `tuple[str, bool]` (`src/scraper/scraper.py:448`, `:528`). Retryability is a prefix match: the tuple at `:41-45`, the predicate at `:170-176`, the decision at `:633`, against messages spelled at `:464`, `:469`, `:513`, `:516`, `:520`. `tests/test_scraper.py:558-577` mirrors the strings. The `attempts` log is a second literal vocabulary at `:611-661`. The markdown cap runs inside each fetcher (`:522`, `:588`) and cleaning in each success branch (`:647`, `:664`).

**F5. Settings described by hand.** `src/scraper/settings.py:34-55` declares 18 fields. The docstring at `:10-31` restates each. README.md:119-127 documents 7. Undocumented: `scrape_acquire_timeout_seconds`, `validate_timeout_seconds`, `jina_timeout_seconds`, `jina_max_bytes`, `max_markdown_chars`, `max_retries`, `retry_delay_seconds`, `max_redirects`, `crawl_render_delay_seconds`, `crawl_page_timeout_ms`, `crawl_word_count_threshold`, `max_request_body_bytes`.

**F6. Trust boundary in two modules.** `src/scraper/safety.py:3-5`, `:14-16`, `:244-245` point at `scraper._guard_route` as the other half of the boundary. That half is `src/scraper/scraper.py:330-445`: `RouteHostKey`, `_route_host_key`, `_guard_route`, `_make_route_guard`, `_install_route_guard`, plus closure state. The `cache` parameter on `_guard_route` (`:352`) exists so state can be shared and injected (`tests/test_scraper.py:1067`, `:1079`, `:1094`). The test file imports six private names at `:20-26`.

**F7. Port mirrored into the container.** 8020 at `src/scraper/settings.py:37`, `Dockerfile:35` and `:40`, `docker-compose.yml:9`, `:14`, `:32`, README.md:121, :168, :189. CMD reads settings; HEALTHCHECK is a literal. `docker run -e SCRAPER_PORT=9000` starts on 9000 and the probe still hits 8020.

**F8. Semaphore sized at import.** `src/scraper/server.py:38`. Tests replace the object at `tests/test_server.py:158` and `:204` because the setting is not honored after import.

## Proposed Changes

### P1. One result model

`ScrapeResult` in `src/scraper/scraper.py` MUST become a pydantic `BaseModel` with the same seven fields, defaults, and mutability it has today. `ScrapeResponse` and the field-by-field copy in `src/scraper/server.py` MUST be removed; the endpoint MUST declare `response_model=ScrapeResult` and return the pipeline's result directly.

```python
class ScrapeResult(BaseModel):
    markdown: str = ""
    source: str = "none"
    url: str = ""
    bot_detected: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0
    attempts: list[str] = Field(default_factory=list)
```

The CLI MUST have exactly one serializer. It takes a `ScrapeResult` and an `ok` flag and emits the model's fields plus `ok`. Every exit path, including exit 2 for a missing URL, malformed stdin, or a rejected URL, MUST build a `ScrapeResult` with `url` and `error` set and go through that serializer. The three-key serializer at `cli.py:53-60` MUST be deleted. JSON key order is not part of the contract.

`ScrapeResult.url` MUST be the sanitized URL from `ValidatedTarget.url` when the guard accepted the input, so userinfo never appears in any output. On exit 2 before validation (no URL, bad stdin) it echoes the raw input as today.

### P2. Validate once, at the pipeline entry

`scrape` MUST be the only place a caller-supplied URL string is validated. Its signature becomes:

```python
async def scrape(url: str, *, deadline: float | None = None) -> ScrapeResult:
    """Raises UrlRejected when the guard blocks the URL. Never raises for fetch failures."""
```

`scrape` MUST call `validate_url_async` exactly once with `timeout=settings.validate_timeout_seconds`, and MUST raise `UrlRejected` unchanged when validation fails. It MUST NOT return an error-shaped result for a rejected URL; the `attempts` entry `blocked: ...` at `scraper.py:611` goes away.

The fetchers MUST take the proof, not the string:

```python
async def crawl_url(target: ValidatedTarget) -> str
async def jina_fetch(target: ValidatedTarget) -> str
```

Neither fetcher calls the guard. `crawl_url` navigates `target.url`; `jina_fetch` builds `JINA_READER_PREFIX + target.url`. Both keep their names and remain module-public; `scrape` is the only supported entry and the module docstring says so.

Each boundary maps `UrlRejected` to its own failure shape and does nothing else with the URL:

```python
# server.py
try:
    result = await scrape(url, deadline=settings.server_timeout_seconds)
except UrlRejected as exc:
    raise HTTPException(status_code=400, detail="URL rejected") from exc

# cli.py, inside _run_scrape
try:
    result = await scrape(url, deadline=timeout)
except UrlRejected as exc:
    return ScrapeResult(url=url, error=f"Invalid or blocked URL ({exc.reason})"), EXIT_INVALID_INPUT
```

The endpoint's pre-flight at `server.py:299` and the CLI's `_validate_url` at `cli.py:89-102` MUST be removed. The semaphore acquire-before-validate order (F7 of the security remediation) is preserved: the endpoint acquires, then calls `scrape`, which validates first.

The route guard's per-request validation is unchanged (see P6). Seeding its cache with the pre-validated target is Open Question 2.

### P3. `scrape` owns the deadline

`scrape` MUST enforce `deadline` itself with `asyncio.wait_for` around its body. The semantics match `asyncio.wait_for`: `None` means no limit. On expiry `scrape` MUST return, not raise, a result with `error` set to `Global timeout exceeded (<seconds>s)`, `attempts` extended with `deadline exceeded`, `elapsed_ms` set, and whatever `attempts` the run had already recorded. The exact number formatting inside the parentheses is not part of the contract.

The deadline wrappers at `cli.py:108-124` and `server.py:303-309` MUST be removed. `cli.DEFAULT_TIMEOUT_SECONDS` MUST be derived from `settings.server_timeout_seconds` so there is one default. README.md:86 and :125 then describe one value.

### P4. Typed fetch failures

Fetchers MUST report failure by raising, not by returning a string:

```python
class FailureKind(StrEnum):
    NOT_INSTALLED = "not_installed"     # crawl4ai import failed
    TIMEOUT = "timeout"                 # crawl exceeded crawl_timeout_seconds
    CRAWL_FAILED = "crawl_failed"       # crawl4ai reported success=False
    EMPTY = "empty"                     # fetcher returned no markdown
    TOO_LARGE = "too_large"             # Jina response over jina_max_bytes
    UPSTREAM_STATUS = "upstream_status" # Jina non-200
    TRANSPORT = "transport"             # httpx transport error

    @property
    def retryable(self) -> bool:
        return self in {FailureKind.CRAWL_FAILED, FailureKind.EMPTY}


class FetchError(Exception):
    def __init__(self, kind: FailureKind, message: str) -> None: ...
```

Retryability MUST be a property of the kind. `NON_RETRYABLE_CRAWL_PREFIXES` and `_is_non_retryable_crawl_error` MUST be deleted. The retryable set matches today's behavior: everything except not-installed, timeout, and blocked is retried, and blocked is no longer reachable because the fetcher takes a `ValidatedTarget`.

`scrape` MUST catch `FetchError` and decide by `exc.kind.retryable`. Any other exception from a fetcher MUST still be caught, logged with its traceback, surfaced as the generic message `Scrape crashed`, and treated as retryable, as today.

`FetchError.message` MUST NOT contain upstream response bodies or transport exception text. The messages that exist today are kept as the `message` for each kind.

`scrape` MUST own post-fetch treatment in one place: cap to `settings.max_markdown_chars`, then bot check (crawl path only), then clean. The cap calls inside the fetchers at `scraper.py:522` and `:588` MUST move to `scrape` and MUST run before any heuristic reads the markdown. The `jina_max_bytes` byte cap stays inside `jina_fetch`; it bounds the network read, not the markdown.

When both fetchers fail, `result.error` MUST be Jina's message when Jina ran, else the crawl message, as today.

The `attempts` strings SHOULD be produced by one private helper from a small set of named events, so the vocabulary has one definition. The strings themselves are unchanged: `crawl4ai attempt N`, `bot protection detected`, `jina fallback`, `jina disabled`, plus the new `deadline exceeded`.

### P5. Settings descriptions as the single source

Every field on `Settings` MUST carry `Field(description=...)`. The per-field lines in the class docstring at `settings.py:10-31` MUST be removed; the docstring keeps a one-line summary.

A script `scripts/render_env_table.py` MUST render a markdown table (variable, default, description) from `Settings.model_fields` and the model's `env_prefix`, and write it between `<!-- env-table:start -->` and `<!-- env-table:end -->` markers in README.md. With `--check` it MUST exit non-zero when the README block differs from the rendered output. CI MUST run the check. The block MUST carry a comment that it is generated and not edited by hand. The table MUST list all 18 settings.

### P6. The browser route guard beside the policy

The route guard MUST move out of `scraper.py` into a new module `src/scraper/browser_guard.py`. `safety.py` stays free of Playwright and crawl4ai knowledge; the new module is its adapter for the browser seam.

```python
class BrowserRouteGuard:
    def __init__(self, *, max_redirects: int, validate_timeout: float) -> None: ...
    async def __call__(self, route: Any) -> None: ...   # Playwright request handler
    async def attach(self, page: Any) -> None: ...      # page.route("**/*", self)


async def on_page_context_created(*args: Any, **kwargs: Any) -> Any:
    """crawl4ai hook: find the page among the arguments, attach a fresh guard, return the page."""
```

The per-origin decision cache and the navigation counter MUST be private instance state. Behavior MUST be unchanged: every request validated through `validate_url_async` with the fail-closed timeout, one resolution per origin per page, abort after `max_redirects + 1` navigations, fail loud when the hook receives no page-like object. `crawl_url` MUST keep its fail-loud checks for `crawler_strategy` and `set_hook` and register `browser_guard.on_page_context_created`. The docstrings in `safety.py` that point at `scraper._guard_route` MUST point at the new module.

### P7. Container healthcheck honors the port setting

The HEALTHCHECK in the Dockerfile and the healthcheck in docker-compose.yml MUST read `SCRAPER_PORT` from the environment with 8020 as the fallback:

```
python -c "import os, urllib.request as u; u.urlopen(f'http://localhost:{os.environ.get(\"SCRAPER_PORT\", \"8020\")}/health').read()"
```

The `SCRAPER_PORT=8020` line in docker-compose.yml SHOULD be removed; it restates the default. `EXPOSE 8020`, the compose port mapping, and the README lines stay: they are static container metadata and documentation.

### P8. Semaphore built at startup

The concurrency semaphore MUST be constructed inside `lifespan` from `settings.max_concurrent_scrapes` and stored on `app.state`. The endpoint MUST read it from the request's application state. The module-level `_scrape_semaphore` at `server.py:38` MUST be removed. The test client fixture in `tests/conftest.py` MUST use the context-manager form so `lifespan` runs.

### Public interface after the change

| Surface | Before | After |
|---|---|---|
| Library | `scrape(url) -> ScrapeResult`; rejected URL returns `error` | `scrape(url, *, deadline=None) -> ScrapeResult`; rejected URL raises `UrlRejected`; deadline returns `error` |
| Fetchers | `crawl_url(url) -> tuple[str, bool]`, same for Jina | `crawl_url(target: ValidatedTarget) -> str`, raises `FetchError`; same for Jina |
| HTTP | 400 / 503 / 200 with `ScrapeResponse` | same status codes; 200 body is `ScrapeResult`, same seven keys |
| CLI | eight keys on exit 0/1/3/4, three keys on exit 2 | eight keys plus `ok` on every exit code |
| Settings | docstring and partial README table | `Field(description=...)`, generated full README table |
| Container | healthcheck probes 8020 | healthcheck probes `SCRAPER_PORT` |

## Error Handling

The pipeline has three failure classes after this RFC. Each has one owner and one shape.

```
E1  UrlRejected (safety.py)                       raised by scrape
    Recovery: none; the input is refused.
    Surface: HTTP 400 "URL rejected"; CLI exit 2 with the full result shape;
             library callers catch the exception.

E2  FetchError (scraper.py)                        raised by a fetcher, caught by scrape
    Kinds and retry policy: see P4. Retryable kinds are retried up to
    settings.max_retries with settings.retry_delay_seconds between attempts.
    Non-retryable kinds end the crawl loop; the Jina fallback runs if enabled.
    Surface: HTTP 200 with error set; CLI exit 1; library result.error.

E3  Deadline exceeded (scraper.py)                 returned by scrape
    Recovery: the in-flight fetch is cancelled by asyncio.wait_for.
    Surface: HTTP 200 with error set; CLI exit 3; library result.error.
```

Unexpected exceptions inside a fetcher remain E2 with kind-less message `Scrape crashed`, logged with traceback, retried as today. Unexpected exceptions outside the pipeline remain the CLI's exit 4 and FastAPI's 500.

CLI exit codes are unchanged: 0 success, 1 scrape failed, 2 invalid input, 3 timeout, 4 internal error.

## Security Considerations

Every control from the security remediation is preserved. The table names where each lives after this RFC.

| Control | Before | After |
|---|---|---|
| SSRF pre-flight on the URL string | endpoint, CLI, `scrape`, each fetcher | `scrape`, once, with the fail-closed timeout |
| Type-level guarantee that a fetcher only sees validated input | none (fetchers took `str`) | fetchers take `ValidatedTarget`; `ty` rejects a `str` |
| Per-hop in-browser re-validation and redirect cap | `scraper.py` route guard | `browser_guard.py`, behavior unchanged |
| Credential stripping before egress | `ValidatedTarget.url` | unchanged; also the value of `ScrapeResult.url` |
| Error non-reflection (M10, M11) | generic messages in fetchers | `FetchError.message` MUST NOT carry upstream body or transport text |
| Markdown cap before heuristics (M9) | inside each fetcher | inside `scrape`, before bot check and clean |
| Jina byte cap | `jina_fetch` | unchanged |
| Request body cap, API key, bind safety | `server.py` | unchanged |
| Global deadline | endpoint and CLI wrappers | `scrape`, passed by each boundary |
| Concurrency cap, acquire-before-validate order | module-level semaphore | `app.state` semaphore built at startup; order unchanged |

Trust boundaries: the only trusted construction of a `ValidatedTarget` is inside `validate_url`. Python cannot forbid a caller from building one by hand, so the type is a guarantee against accidental bypass, not a hostile one. The class docstring MUST say so. This is the same posture the frozen dataclass has today.

Blast radius: the pre-flight now has one site instead of four. A regression there affects every entry path at once, which is also why one site is easier to test than four. The route guard remains an independent second layer for every in-browser request, so a pre-flight regression still cannot reach an internal host through the browser.

The `.env` read by `pydantic-settings` at import is unchanged and outside this RFC.

## Migration Strategy

Contract changes for consumers, all shipped together as one breaking release:

- Library: `scrape` raises `UrlRejected` for a rejected URL instead of returning an error result. `crawl_url` and `jina_fetch` take a `ValidatedTarget` and raise `FetchError`. `ScrapeResult` is a pydantic model; keyword construction is unchanged, `dataclasses.asdict` no longer applies. `ScrapeResult.url` is the sanitized URL.
- CLI: exit-2 output gains `markdown`, `source`, `bot_detected`, `elapsed_ms`, `attempts`. Existing keys are unchanged. The `--timeout` default follows `SCRAPER_SERVER_TIMEOUT_SECONDS`.
- HTTP: no change to status codes or the response key set. The `url` value is sanitized.
- Container: the healthcheck follows `SCRAPER_PORT`.

Version: 0.2.0 to 0.3.0. Commits that change a contract MUST use the `!` breaking marker so release-please records them under the pre-1.0 minor bump. The README sections for the library, the CLI JSON, the timeout, and the environment table MUST be updated in the same release.

Backward compatibility: none for the library signatures. Library callers already handle `result.error`; they add one `except UrlRejected`. No feature flag or dual path: each phase below is one PR and is reverted as a unit.

## Risk Assessment

- **P2 and P3 have the largest churn.** 29 call sites in `tests/test_scraper.py` pass strings to `crawl_url`, `jina_fetch`, or `scrape`, and about twenty fetcher tests assert on message text. These are mechanical edits, and `ty` finds every missed signature.
- **Cancellation moves inward.** Today `asyncio.wait_for` cancels `scrape` from outside; after P3 it cancels the pipeline body from inside `scrape`. The cancelled awaitable is the same crawl. The existing test that checks stdout is restored on timeout (`tests/test_scraper.py:621`) covers the suppression context manager under cancellation.
- **Single pre-flight site.** Covered under Security Considerations.
- **Lifespan in every test.** After P8 the test client runs `lifespan`, so `check_bind_safety` runs for every server test. Default settings pass it. Tests that set a non-local host already use the context-manager form.
- **README generation.** A hand edit inside the markers is overwritten by the script. The marker comment and the CI check make that visible before merge.
- **Rollback.** Each phase is one PR and reverts cleanly. Phases 1 and 2 touch the CLI output and the fetcher signatures and are the ones a consumer would notice.

## Testing Strategy

All existing tests MUST pass after each phase, re-pointed to the new signatures where a signature changed. Security tests from the remediation MUST NOT be deleted; each is moved with the code it covers.

New or changed tests per phase:

- P1: one parametrized CLI test that every exit-2 path (no URL, malformed stdin, missing `url` field, rejected URL) emits the full key set plus `ok`. The existing key-set assertions in `tests/test_server.py` and `tests/test_cli.py` stay as contract pins.
- P2: a test that counts resolver calls with a fake crawler and asserts one resolution before the browser on each entry path (library, HTTP, CLI). The scratch script from the audit is the starting point. A test that `crawl_url` and `jina_fetch` are never called with a `str` is the type checker's job: `uv run ty check` MUST pass.
- P3: the CLI and server timeout tests move their assertion to `scrape`'s own deadline; each surface keeps one test that the deadline value it passes is the configured one.
- P4: `TestNonRetryableCrawlError` is replaced by a test over `FailureKind.retryable`. Fetcher tests assert on `exc.kind` and on the message where the message is part of the security contract (non-reflection).
- P5: CI runs `scripts/render_env_table.py --check`. A unit test asserts the rendered table has one row per `Settings` field.
- P6: the thirteen route-guard tests construct `BrowserRouteGuard` and call it; none injects a cache.
- P7: no automated test. Container builds are still not run in CI (security remediation DF-3). Verified by hand with `docker run -e SCRAPER_PORT=9000` and `docker inspect` health status.
- P8: the 503 tests set `settings.max_concurrent_scrapes` or replace `app.state.scrape_semaphore` after startup.

CI gate for every phase: `ruff format --check`, `ruff check`, `ty check`, `pytest`, `pip-audit`.

## Implementation Plan

Each phase is one PR against `main`, merged only with CI green. Phases 1 through 4 touch `scraper.py` and are sequential to avoid conflicts. Phases 5 through 7 are independent of each other and of the first four.

1. **P1 result model and CLI shape.** Delivers the contract fix. Verify: parametrized exit-2 test, server response tests.
2. **P4 typed fetch failures, cap and clean in `scrape`.** Internal only. Verify: retry-policy tests over `FailureKind`, security non-reflection tests.
3. **P2 and P3 together: `scrape` validates once and owns the deadline.** These change the same function and ship as one PR. Verify: resolution-count test, timeout tests, `ty check`.
4. **P6 browser guard module.** Move only, no behavior change. Verify: the thirteen guard tests against the class.
5. **P8 semaphore at startup.** Verify: 503 tests through the setting.
6. **P5 settings descriptions and generated env table.** Verify: CI check step.
7. **P7 healthcheck.** Verify by hand as noted in Testing Strategy.

Release: after phase 7, the release-please PR carries 0.3.0 with the breaking entries from phases 1 and 3. Owner: the repository author.

## Open Questions

1. **Rename `SCRAPER_SERVER_TIMEOUT_SECONDS`?** After P3 it sets the CLI default too, so `SERVER` in the name is misleading. Options: (a) keep the name and document the dual role; (b) rename to `SCRAPER_TIMEOUT_SECONDS` and accept the old name as an alias for one release; (c) rename without an alias. Recommended: (a) for this RFC, (b) as a follow-up once pydantic-settings alias behavior under `env_prefix` is verified. Criterion: whether any deployment sets the variable. docker-compose.yml does not. Decision recorded as machine-made pending the author's answer.
2. **Seed the route guard's origin cache with the pre-validated target?** Options: (a) no; the guard resolves the first navigation itself; (b) yes; `crawl_url` passes the target's origin key as an allowed entry and the first navigation skips one resolution. Recommended: (a). The guard stays an independent layer, and the cost is one resolution per crawl. Criterion: measured latency of that resolution on the target hardware. Decision recorded as machine-made.
3. **Keep `crawl_url` and `jina_fetch` public?** Options: (a) keep the names; document `scrape` as the only supported entry; (b) prefix with an underscore now that the signatures change anyway. Recommended: (a); the tests use them as internal seams and the names are stable. Decision recorded as machine-made.

## References

### Normative

- [RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) - key word definitions used in this document.
- `README.md` - the product contract this RFC consolidates: API response, CLI JSON, exit codes, environment table.
- `src/scraper/safety.py`, `src/scraper/scraper.py`, `src/scraper/server.py`, `src/scraper/cli.py`, `src/scraper/settings.py` - the modules changed; line references in Current State are against commit f559cb1.

### Informative

- `.plans/security-remediation/rfc.md` - the security controls this RFC preserves, with their original rationale.
- `.plans/security-remediation/progress-report.md` - which controls shipped and which follow-ups (DF-3 container verification) remain open.
- Codebase audit, 2026-09-05, delivered in chat - the ranked findings and the considered-and-rejected list.
- Codebase-design skill - the vocabulary (module, interface, seam, depth, adapter) used here.
