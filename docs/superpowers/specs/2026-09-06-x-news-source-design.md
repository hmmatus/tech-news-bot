# X (Twitter) news source via OpenAI Responses API — design

**Date:** 2026-09-06
**Status:** approved, ready for implementation plan

## Problem

The bot currently pulls from RSS feeds and Hacker News (`src/sources.py`). We
want a third source: notable tech/AI/security news currently circulating on
X (Twitter), surfaced via an OpenAI API call, merged into the same item
pipeline and delivered through the existing Telegram flow.

## Constraint that shapes this design

A bare OpenAI chat completion has no live data — asked for "news on X" it
will invent plausible-looking tweet URLs and headlines. For a bot whose
entire value is real, clickable links, that's disqualifying. Plain OpenAI
API access also cannot read x.com directly (no crawler access, no API
token for X search included). The only viable combination without a paid
X API subscription is: **OpenAI Responses API with the built-in
`web_search` tool**, which returns real URLs as citations for pages it
actually fetched. Coverage is indirect — mostly news/blog coverage of what's
trending on X, not raw tweets — which is what "web_search" can actually see
of X (X blocks generic crawlers). This tradeoff was presented to the user
and approved.

## Non-goals

- No X API v2 integration (would need a paid tier).
- No CLI dependency (`codex exec`) in the GitHub Actions workflow.
- No change to `filters.py`, `telegram.py`, `store.py`, or the message
  format — new items are just `Item` instances flowing through the
  existing pipeline.

## Architecture

New module `src/x_news.py`, same shape as `fetch_rss`/`fetch_hackernews` in
`src/sources.py`:

```python
def fetch_x_news(cfg: Config, since: datetime) -> list[Item]:
    ...
```

Wired into `sources.collect()`:

```python
def collect(cfg: Config) -> list[Item]:
    since = datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)
    return fetch_rss(cfg, since) + fetch_hackernews(cfg, since) + fetch_x_news(cfg, since)
```

Everything downstream (`filters.classify`, `filters.rank`, `SeenStore`
dedup, `telegram.build_messages`) is source-agnostic already — items are
identified by `fingerprint` (canonical URL + slugified title), so an
X-sourced story that's also covered by TechCrunch collapses into one
message automatically. No changes needed there.

## API call shape

**Revised after live testing (see "Resolved risk" below) — this section
describes what actually shipped, not the original json_schema design.**

Endpoint: `POST https://api.openai.com/v1/responses`

```json
{
  "model": "gpt-5.6-luna",
  "input": [
    {"role": "system", "content": "<see prompt below>"},
    {"role": "user", "content": "<query built from cfg.x_search.queries>"}
  ],
  "tools": [
    {"type": "web_search", "search_context_size": "low"}
  ]
}
```

No `text.format.json_schema` block — see below for why. Model:
`gpt-5.6-luna` (cost-optimized tier, $0.20/$1.20 per Mtok input/output) by
default, overridable via config — this call runs on every scheduled
workflow trigger (4x/day) so cost matters more than peak reasoning quality.

**Resolved risk (was flagged as open, now verified against the live API):**
the original design asked for a strict `text.format.json_schema` alongside
`web_search`, planning to cross-check the model's structured `items[].url`
values against the tool's `url_citation` annotations. Live testing showed
this combination is silently broken: `web_search` still executes real
searches (confirmed via `web_search_call` output items), but the response's
`annotations` array comes back **empty** whenever `text.format.json_schema`
is set — the identical call without the schema constraint returns real
`url_citation` annotations. OpenAI's API doesn't error; it just drops the
citations. Since this feature's citation gate requires at least one
citation to trust anything, the schema-constrained version safely produced
zero items on every run — correct behavior given the gate's design, but
functionally dead.

**Fix, and the shipped design:** drop the JSON schema entirely. Don't ask
the model for a structured items list at all — build `Item`s directly from
`web_search`'s own `url_citation` annotations instead (see below). This
removes a whole layer of indirection (and the failure modes that came with
parsing model-authored JSON) along with the risk.

### Prompt (system message)

The prompt instructs the model to search for tech/AI/security news
currently being discussed on X/Twitter, using the configured query terms,
and to cite the real sources it finds — no structured-output instructions,
since there's no JSON for the model to produce anymore.

## Anti-hallucination gate (the core safety mechanism)

**Revised from the original design** (which asked the model for a
structured JSON items list, verified against citations after the fact) —
see "Resolved risk" above for why that approach was replaced.

There is no model-authored URL to verify anymore. `Item`s are built
directly from the API's own citation records:

1. Read `response.output` for the `message` item's
   `content[].annotations[]` — these are `url_citation` objects
   `{url, title, start_index, end_index}` corresponding to pages the
   `web_search` tool actually fetched. Each one already carries both a
   real `url` and a `title`.
2. Deduplicate by `models.normalize_url(url)`, keeping the first
   occurrence's original (un-normalized) URL and title.
3. Build one `Item` per surviving citation directly — `source` is derived
   from the citation URL's domain (`urlsplit(url).netloc`, minus a leading
   `www.`), since annotations carry no separate source/outlet field.
4. If zero citations survive, return `[]` — same as any other empty-source
   case.

There is no longer a "the model said X but did it really cite X" check to
get wrong, because nothing routes through model-authored JSON at all — the
citation *is* the item.

This makes fabrication structurally hard: the model can put whatever it
wants in the JSON `url` field, but if it doesn't match a URL the tool
actually visited, it's discarded before ever becoming an `Item`.

No second-stage `fetch_bytes` HEAD-check against each surviving URL in
this version — citation-matching against `web_search`'s own fetch record
is the trust boundary. (Adding a live HTTP re-verification pass is a
reasonable future hardening step, not required for v1.)

## Config additions

`feeds.yaml` — new top-level block, all optional with safe defaults:

```yaml
x_search:
  enabled: true
  queries:
    - "AI news trending on X twitter"
    - "cybersecurity news trending on X twitter"
  max_items: 5
  model: gpt-5.6-luna
  search_context_size: low
```

`src/config.py` — new fields on `Config`:

```python
x_search_enabled: bool = False   # default OFF; explicit opt-in
x_search_queries: list[str] = field(default_factory=list)
x_search_max_items: int = 5
x_search_model: str = "gpt-5.6-luna"
x_search_context_size: str = "low"
```

Default `x_search_enabled: False` — this is a new paid external
dependency; it should not silently turn on for existing installs pulling
a fresh `feeds.yaml` example, and installs that omit the block entirely
get the same behavior as today.

**Not** a `_require_env`-style property — `telegram_token`/`telegram_chat_id`
raise `SystemExit` on absence because a missing credential there means the
whole run cannot deliver anything, so crashing loud is correct. A missing
`OPENAI_API_KEY` must not have that effect: this is one optional source
among three, and its failure posture (below) is "log and return `[]`",
identical to a feed with a bad URL. So instead, `fetch_x_news` reads the
env var directly with a non-raising accessor:

```python
@property
def openai_api_key(self) -> str | None:
    return os.environ.get("OPENAI_API_KEY", "").strip() or None
```

`fetch_x_news` checks this for `None` itself and returns `[]` with a
WARNING log — never lets a `SystemExit` or exception escape, matching
every other source in `sources.py`.

## Failure posture

Same contract as every other source in `sources.py`: never raises out of
`fetch_x_news`, never crashes the run.

| Failure | Behavior |
|---|---|
| `x_search_enabled: false` (default) | Return `[]` immediately, no API call, no key check. |
| `OPENAI_API_KEY` unset while enabled | Log WARNING, return `[]`. |
| HTTP/network error calling OpenAI | Log WARNING (reuse `fetch_bytes`-style backoff — same retry budget: 3 attempts, exponential backoff on 5xx/timeouts; 429 respects `Retry-After`), return `[]` on exhaustion. |
| Malformed / non-JSON response body, or an unexpected envelope shape (top-level not a dict, `content`/`annotations` not lists of dicts, etc.) | Log WARNING, return `[]`. |
| `web_search` returns zero citations (nothing found, or nothing worth citing) | Log WARNING, return `[]`. This is the only "found nothing" case now — there's no separate structured-output-conflict scenario to distinguish, since there's no structured output to conflict. |

## Workflow change

One line in `.github/workflows/news.yml`, inside the existing "Enviar
noticias" step's `env:` block:

```yaml
OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

The user adds `OPENAI_API_KEY` as a repo secret manually (not something
this implementation can do). No new step, no new job — `fetch_x_news`
runs inline inside `python -m src.main` like every other source.

## Testing

`tools/test_pipeline.py` runs offline (network is blocked in the test
container) and asserts against synthetic data — the same style continues:

1. Citation extraction: given a fake OpenAI response payload with several
   `annotations`, including a duplicate URL differing only by tracking
   params, assert the resulting items are deduplicated by normalized URL
   and built from the citations' own `url`/`title`.
2. `x_search_enabled: false` (default): assert `fetch_x_news` returns `[]`
   without attempting any network call (verifiable by not needing
   `OPENAI_API_KEY` set in the test env at all).
3. Missing `OPENAI_API_KEY` while enabled: assert `[]` + no exception.
4. Malformed JSON body or unexpected envelope shape: assert `[]` + no
   exception.
5. Happy path: valid payload with real citations produces correctly
   shaped `Item` objects that flow through `filters.classify` /
   `filters.rank` / dedup identically to RSS-sourced items.

This requires the OpenAI HTTP call to be injectable/mockable — the
implementation plan should structure `fetch_x_news` to accept an
`httpx.Client` (or a thin request function) as a parameter, matching how
`_fetch_feed` already accepts `client: httpx.Client`, so tests can swap in
a fake transport instead of hitting the real API.

## Out of scope / future hardening (not this pass)

- Live HTTP re-verification of surviving URLs before sending.
- Paid X API v2 integration for actual tweet-level data.
- Per-query independent rate limiting/backoff tuning beyond the shared
  `fetch_bytes`-style retry budget.
