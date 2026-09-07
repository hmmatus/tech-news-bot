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

Endpoint: `POST https://api.openai.com/v1/responses`

```json
{
  "model": "gpt-5.6-luna",
  "input": [
    {"role": "system", "content": "<see prompt below>"},
    {"role": "user", "content": "<query built from cfg.x_search.queries + since>"}
  ],
  "tools": [
    {"type": "web_search", "search_context_size": "low"}
  ],
  "text": {
    "format": {
      "type": "json_schema",
      "name": "x_news_items",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "items": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "title": {"type": "string"},
                "url": {"type": "string"},
                "source": {"type": "string"}
              },
              "required": ["title", "url", "source"],
              "additionalProperties": false
            }
          }
        },
        "required": ["items"],
        "additionalProperties": false
      }
    }
  }
}
```

Model: `gpt-5.6-luna` (cost-optimized tier, $0.20/$1.20 per Mtok input/output)
by default, overridable via config — this call runs on every scheduled
workflow trigger (4x/day) so cost matters more than peak reasoning quality.

**Open risk, flagged for the implementation plan:** OpenAI's docs do not
confirm that `web_search` and strict `text.format.json_schema` can be used
in the same call. The implementation must verify this against a live call
early. If they conflict, fallback: call `web_search` without structured
output, then run a second cheap call (or manual JSON parsing with a
try/except and a retry-with-correction prompt) to coerce the free-text
result into the schema.

### Prompt (system message)

The prompt instructs the model to:
- Search for tech/AI/security news currently being discussed on X/Twitter
  from the last `cfg.lookback_hours` hours, using the configured query
  terms.
- Return only items it found via search (not from memory/training data).
- For each item, give the title, the **canonical article/source URL**
  (not a la carte tweet ID guesses), and the outlet/account name.
- Return `{"items": []}` if nothing relevant turns up — never pad with
  invented items to fill a quota.

## Anti-hallucination gate (the core safety mechanism)

The model's structured JSON `items[].url` values are **not trusted
directly**. Instead:

1. Read `response.output` for the `message` item's
   `content[].annotations[]` — these are `url_citation` objects
   `{url, title, start_index, end_index}` corresponding to pages the
   `web_search` tool actually fetched.
2. Build a set of citation URLs (normalized via the existing
   `models.normalize_url`).
3. For each parsed JSON item, keep it only if its `url` (normalized)
   exactly matches one of the citation URLs. Drop anything else and log
   it at DEBUG (not WARNING — expected to happen sometimes, not an error).
4. If zero items survive this gate, return `[]` — same as any other
   empty-source case.

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
| Malformed / non-JSON response body | Log WARNING, return `[]`. |
| JSON parses but citation gate drops everything | Log INFO ("0 nota(s) recientes" — same as any empty source), return `[]`. |
| OpenAI returns items but `annotations` missing entirely (structured-output/web_search conflict from the open risk above) | Log WARNING once distinctly ("no citations returned, discarding N item(s)"), return `[]`. This is the fail-safe for the unverified API-combination risk. |

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

1. Citation-matching gate: given a fake OpenAI response payload where one
   item's URL is in `annotations` and one isn't, assert only the cited one
   survives.
2. `x_search_enabled: false` (default): assert `fetch_x_news` returns `[]`
   without attempting any network call (verifiable by not needing
   `OPENAI_API_KEY` set in the test env at all).
3. Missing `OPENAI_API_KEY` while enabled: assert `[]` + no exception.
4. Malformed JSON body: assert `[]` + no exception.
5. Happy path: valid payload with matching citations produces correctly
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
