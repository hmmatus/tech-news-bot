# X (Twitter) News Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third news source that surfaces tech/AI/security stories circulating on X (Twitter), found via the OpenAI Responses API's built-in `web_search` tool, and merges them into the existing RSS + Hacker News pipeline.

**Architecture:** New self-contained module `src/x_news.py` exposes `fetch_x_news(cfg, since, client=None) -> list[Item]`, matching the shape of `fetch_rss`/`fetch_hackernews` in `src/sources.py`. It calls the OpenAI Responses API with `web_search` + a strict JSON schema, then keeps only items whose URL matches a real `url_citation` annotation the tool actually fetched — the model's raw JSON URLs are never trusted directly. `sources.collect()` gains one line to include it. Downstream (`filters.py`, `telegram.py`, `store.py`) needs zero changes — items are source-agnostic already.

**Tech Stack:** Python 3.12, httpx (already a dependency — no new packages), stdlib `unittest.mock` for the `collect()` wiring test, `httpx.MockTransport` for HTTP-level tests.

**Spec:** `docs/superpowers/specs/2026-09-06-x-news-source-design.md`

## Global Constraints

- `x_search_enabled` defaults to `False` — opt-in only, existing installs unaffected.
- Default model: `gpt-5.6-luna`. Default `search_context_size`: `low`. Both overridable in `feeds.yaml`.
- Endpoint: `POST https://api.openai.com/v1/responses`.
- Retry budget matches `src/sources.py`'s `fetch_bytes` convention: max 3 attempts, exponential backoff with full jitter capped at 30s, `Retry-After` respected on 429, backoff on 5xx and network exceptions, raise the original exception once exhausted.
- `OPENAI_API_KEY` access must be **non-raising** (returns `None` if unset) — unlike `telegram_token`/`telegram_chat_id`, a missing key here means "skip this source," not "crash the run."
- No item is ever built from a URL that isn't in the `web_search` tool's own citation list — this is the only trust boundary in v1 (no live HTTP re-verification pass).
- No changes to `filters.py`, `telegram.py`, or `store.py`.

---

### Task 1: Config support for `x_search`

**Files:**
- Modify: `src/config.py:21-31` (Config dataclass fields), `src/config.py:33-39` (properties), `src/config.py:75-85` (`load()` return)
- Test: `tools/test_pipeline.py` (append new assertions; no new test file — repo has no pytest, this script is the existing convention)

**Interfaces:**
- Produces: `Config.x_search_enabled: bool`, `Config.x_search_queries: list[str]`, `Config.x_search_max_items: int`, `Config.x_search_model: str`, `Config.x_search_context_size: str`, `Config.openai_api_key: str | None` (property, reads `OPENAI_API_KEY` env var, never raises).

- [ ] **Step 1: Write the failing test**

Add to `tools/test_pipeline.py`, after the existing `# 8) escapado de HTML` block (before the final `print("\n--- muestra del mensaje ---")` block):

```python
# 9) config: bloque x_search opcional, con defaults seguros si falta
# (tempfile ya está importado arriba, en la cabecera del script)
from src.config import load as _load_config

_feeds_yaml_sin_x_search = """
feeds:
  - name: Test Feed
    url: https://example.test/feed.xml
    tag: TECH
"""
_tmp_cfg_path = Path(tempfile.mkdtemp()) / "feeds.yaml"
_tmp_cfg_path.write_text(_feeds_yaml_sin_x_search, encoding="utf-8")
cfg_sin_x = _load_config(_tmp_cfg_path)
assert cfg_sin_x.x_search_enabled is False
assert cfg_sin_x.x_search_queries == []
assert cfg_sin_x.x_search_max_items == 5
assert cfg_sin_x.x_search_model == "gpt-5.6-luna"
assert cfg_sin_x.x_search_context_size == "low"
print("OK  x_search: defaults seguros cuando el bloque falta")

_feeds_yaml_con_x_search = _feeds_yaml_sin_x_search + """
x_search:
  enabled: true
  queries:
    - "AI news trending on X twitter"
  max_items: 3
  model: gpt-6-astra
  search_context_size: medium
"""
_tmp_cfg_path.write_text(_feeds_yaml_con_x_search, encoding="utf-8")
cfg_con_x = _load_config(_tmp_cfg_path)
assert cfg_con_x.x_search_enabled is True
assert cfg_con_x.x_search_queries == ["AI news trending on X twitter"]
assert cfg_con_x.x_search_max_items == 3
assert cfg_con_x.x_search_model == "gpt-6-astra"
assert cfg_con_x.x_search_context_size == "medium"
print("OK  x_search: bloque explícito se parsea correctamente")

import os

os.environ.pop("OPENAI_API_KEY", None)
assert cfg_con_x.openai_api_key is None
os.environ["OPENAI_API_KEY"] = "sk-test-123"
assert cfg_con_x.openai_api_key == "sk-test-123"
os.environ.pop("OPENAI_API_KEY", None)
print("OK  x_search: openai_api_key no lanza, devuelve None si falta")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tools/test_pipeline.py`
Expected: `AttributeError: 'Config' object has no attribute 'x_search_enabled'`

- [ ] **Step 3: Implement the config fields**

In `src/config.py`, replace lines 21-39 (the `Config` class body through the `telegram_chat_id` property) with:

```python
@dataclass
class Config:
    feeds: list[Feed]
    lookback_hours: int = 6
    max_items_per_run: int = 15
    timezone: str = "America/El_Salvador"
    keywords: dict[str, list[str]] = field(default_factory=dict)
    require_keyword: bool = False
    hn_min_points: int = 80
    hn_enabled: bool = True
    state_retention_days: int = 14
    x_search_enabled: bool = False
    x_search_queries: list[str] = field(default_factory=list)
    x_search_max_items: int = 5
    x_search_model: str = "gpt-5.6-luna"
    x_search_context_size: str = "low"

    @property
    def telegram_token(self) -> str:
        return _require_env("TELEGRAM_BOT_TOKEN")

    @property
    def telegram_chat_id(self) -> str:
        return _require_env("TELEGRAM_CHAT_ID")

    @property
    def openai_api_key(self) -> str | None:
        """A diferencia de telegram_token, esta NO lanza si falta: la
        búsqueda en X es una fuente opcional, no un requisito para correr."""
        return os.environ.get("OPENAI_API_KEY", "").strip() or None
```

Then in `load()` (currently lines 75-85), replace the `return Config(...)` call with:

```python
    x_search = raw.get("x_search", {}) or {}

    return Config(
        feeds=feeds,
        keywords=keywords,
        lookback_hours=int(settings.get("lookback_hours", 6)),
        max_items_per_run=int(settings.get("max_items_per_run", 15)),
        timezone=settings.get("timezone", "America/El_Salvador"),
        require_keyword=bool(settings.get("require_keyword", False)),
        hn_min_points=int(settings.get("hn_min_points", 80)),
        hn_enabled=bool(settings.get("hn_enabled", True)),
        state_retention_days=int(settings.get("state_retention_days", 14)),
        x_search_enabled=bool(x_search.get("enabled", False)),
        x_search_queries=[str(q) for q in (x_search.get("queries") or [])],
        x_search_max_items=int(x_search.get("max_items", 5)),
        x_search_model=str(x_search.get("model", "gpt-5.6-luna")),
        x_search_context_size=str(x_search.get("search_context_size", "low")),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tools/test_pipeline.py`
Expected: all `OK` lines print, including the three new ones, no traceback.

- [ ] **Step 5: Commit**

```bash
git add src/config.py tools/test_pipeline.py
git commit -m "$(cat <<'EOF'
Add x_search config block with non-raising OPENAI_API_KEY access

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EGtw5uS2B2ouZz4s3oC4CX
EOF
)"
```

---

### Task 2: `src/x_news.py` — request building and response parsing helpers

**Files:**
- Create: `src/x_news.py`
- Test: `tools/test_pipeline.py`

**Interfaces:**
- Consumes: `Config.x_search_queries: list[str]`, `Config.x_search_model: str`, `Config.x_search_context_size: str` (Task 1). `models.normalize_url(url: str) -> str` (existing).
- Produces: `_build_body(cfg: Config) -> dict`, `_extract_message(body: dict) -> dict | None`, `_citation_urls(message: dict) -> set[str]`, `_message_text(message: dict) -> str | None`. These are consumed by `fetch_x_news` in Task 3.

- [ ] **Step 1: Write the failing test**

Add to `tools/test_pipeline.py`, after the Task 1 assertions:

```python
# 10) x_news: helpers de construcción de request y parseo de respuesta
from src.x_news import _build_body, _extract_message, _citation_urls, _message_text

cfg_x = _load_config(_tmp_cfg_path)  # el que tiene x_search habilitado, del bloque anterior
body = _build_body(cfg_x)
assert body["model"] == "gpt-6-astra"
assert body["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
assert body["text"]["format"]["type"] == "json_schema"
assert body["text"]["format"]["strict"] is True
assert "AI news trending on X twitter" in body["input"][1]["content"]
print("OK  x_news: _build_body arma el request correctamente")

_fake_response_body = {
    "output": [
        {"type": "web_search_call", "id": "ws_1"},
        {
            "type": "message",
            "content": [
                {
                    "text": '{"items": [{"title": "T", "url": "https://real.test/a", "source": "S"}]}',
                    "annotations": [
                        {"type": "url_citation", "url": "https://real.test/a", "title": "T"}
                    ],
                }
            ],
        },
    ]
}
msg = _extract_message(_fake_response_body)
assert msg is not None and msg["type"] == "message"
assert _citation_urls(msg) == {"https://real.test/a"}
assert "real.test/a" in _message_text(msg)
print("OK  x_news: _extract_message/_citation_urls/_message_text parsean la respuesta")

assert _extract_message({"output": []}) is None
assert _citation_urls({"content": []}) == set()
assert _message_text({"content": []}) is None
print("OK  x_news: helpers devuelven vacío/None ante respuesta sin datos")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tools/test_pipeline.py`
Expected: `ModuleNotFoundError: No module named 'src.x_news'`

- [ ] **Step 3: Implement the helpers**

Create `src/x_news.py`:

```python
"""Fuente de noticias vía OpenAI Responses API + web_search: rastrea qué se
está discutiendo en X/Twitter sobre tech/IA/seguridad.

Las URLs que el modelo devuelve en el JSON NUNCA se confían directamente:
solo sobreviven las que coinciden con una citación real (`url_citation`)
que la herramienta web_search haya devuelto, es decir, una página que de
verdad fue consultada. Esa es la única barrera de confianza en esta
versión (ver docs/superpowers/specs/2026-09-06-x-news-source-design.md).
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timezone

import httpx

from .config import Config
from .models import Item, normalize_url

log = logging.getLogger(__name__)

RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

MAX_RETRIES = 3
MAX_BACKOFF_SECONDS = 30.0

_SYSTEM_PROMPT = (
    "Busca noticias de tecnología, IA o ciberseguridad que se estén "
    "discutiendo activamente en X (Twitter) ahora mismo, usando la "
    "herramienta de búsqueda web. Devuelve solo notas que hayas encontrado "
    "por búsqueda, nunca inventadas de memoria. Para cada nota da un "
    "título, la URL canónica del artículo o fuente (no un tweet adivinado), "
    "y el nombre del medio o cuenta. Si no encuentras nada relevante, "
    "devuelve una lista vacía."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["title", "url", "source"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _build_body(cfg: Config) -> dict:
    query = "\n".join(f"- {q}" for q in cfg.x_search_queries) or "- tech news"
    return {
        "model": cfg.x_search_model,
        "input": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Temas a buscar:\n{query}"},
        ],
        "tools": [
            {"type": "web_search", "search_context_size": cfg.x_search_context_size}
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "x_news_items",
                "strict": True,
                "schema": _SCHEMA,
            }
        },
    }


def _extract_message(body: dict) -> dict | None:
    for entry in body.get("output", []):
        if entry.get("type") == "message":
            return entry
    return None


def _citation_urls(message: dict) -> set[str]:
    urls = set()
    for block in message.get("content", []):
        for annotation in block.get("annotations", []):
            url = annotation.get("url")
            if url:
                urls.add(normalize_url(url))
    return urls


def _message_text(message: dict) -> str | None:
    for block in message.get("content", []):
        text = block.get("text")
        if text:
            return text
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tools/test_pipeline.py`
Expected: all `OK` lines print including the three new ones, no traceback.

- [ ] **Step 5: Commit**

```bash
git add src/x_news.py tools/test_pipeline.py
git commit -m "$(cat <<'EOF'
Add x_news request/response parsing helpers

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EGtw5uS2B2ouZz4s3oC4CX
EOF
)"
```

---

### Task 3: Retry logic — `_post_with_retries`

**Files:**
- Modify: `src/x_news.py` (append)
- Test: `tools/test_pipeline.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_post_with_retries(client: httpx.Client, url: str, headers: dict, body: dict) -> httpx.Response`. Consumed by `fetch_x_news` in Task 4.

This mirrors the retry policy already implemented for `fetch_bytes` in `src/sources.py` (max 3 attempts, `Retry-After` on 429, exponential backoff with jitter capped at 30s on 5xx/network errors), applied to a POST call instead of GET. It's a separate small function rather than a shared import because `fetch_bytes` also does 403→browser-UA escalation, which has no meaning for an authenticated JSON API call — reusing it would mean threading an irrelevant parameter through.

- [ ] **Step 1: Write the failing test**

Add to `tools/test_pipeline.py`, after the Task 2 assertions:

```python
# 11) x_news: reintentos con backoff (429 con Retry-After, 5xx, agotamiento)
from src.x_news import _post_with_retries
import httpx as _httpx

_calls = {"n": 0}


def _handler_429_then_ok(request):
    _calls["n"] += 1
    if _calls["n"] == 1:
        return _httpx.Response(429, headers={"Retry-After": "0"}, json={})
    return _httpx.Response(200, json={"ok": True})


_client = _httpx.Client(transport=_httpx.MockTransport(_handler_429_then_ok))
_resp = _post_with_retries(_client, "https://api.openai.com/v1/responses", {}, {})
assert _resp.status_code == 200 and _calls["n"] == 2
print("OK  x_news: 429 respeta Retry-After y reintenta hasta 200")

_calls["n"] = 0


def _handler_exhausted_5xx(request):
    _calls["n"] += 1
    return _httpx.Response(503, json={})


_client2 = _httpx.Client(transport=_httpx.MockTransport(_handler_exhausted_5xx))
try:
    _post_with_retries(_client2, "https://api.openai.com/v1/responses", {}, {})
    raise AssertionError("debía lanzar tras agotar reintentos")
except _httpx.HTTPStatusError:
    pass
assert _calls["n"] == 3
print("OK  x_news: 5xx agota los 3 intentos y lanza la excepción original")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tools/test_pipeline.py`
Expected: `ImportError: cannot import name '_post_with_retries' from 'src.x_news'`

- [ ] **Step 3: Implement `_post_with_retries`**

Append to `src/x_news.py` (after `_message_text`, before any later functions):

```python
def _exponential_backoff(attempt: int) -> float:
    """Full jitter: espera aleatoria entre 0 y 2**attempt (tope 30s)."""
    ceiling = min(MAX_BACKOFF_SECONDS, float(2**attempt))
    return random.uniform(0, ceiling)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(float(value), MAX_BACKOFF_SECONDS)
    except ValueError:
        return None


def _post_with_retries(
    client: httpx.Client, url: str, headers: dict, body: dict
) -> httpx.Response:
    """POST con reintentos (máx 3): Retry-After en 429, backoff exponencial
    con jitter en 5xx/errores de red. Agotados los intentos, lanza la
    excepción/status original."""
    last_exc: Exception | None = None
    response: httpx.Response | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(_exponential_backoff(attempt))
            continue

        if response.status_code == 200:
            return response

        if response.status_code == 429:
            if attempt < MAX_RETRIES:
                wait = _retry_after_seconds(response)
                if wait is None:
                    wait = _exponential_backoff(attempt)
                time.sleep(wait)
                continue
            response.raise_for_status()

        if response.status_code >= 500:
            if attempt < MAX_RETRIES:
                time.sleep(_exponential_backoff(attempt))
                continue
            response.raise_for_status()

        response.raise_for_status()

    if last_exc is not None:
        raise last_exc
    assert response is not None
    response.raise_for_status()
    raise AssertionError("unreachable")  # pragma: no cover
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tools/test_pipeline.py`
Expected: all `OK` lines print including the two new ones, no traceback. (Runs fast — `Retry-After: 0` and the jitbetred backoff on a 503 with `attempt < 3` sleep for at most a couple seconds.)

- [ ] **Step 5: Commit**

```bash
git add src/x_news.py tools/test_pipeline.py
git commit -m "$(cat <<'EOF'
Add retry/backoff to x_news OpenAI calls

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EGtw5uS2B2ouZz4s3oC4CX
EOF
)"
```

---

### Task 4: `fetch_x_news` orchestration + citation gate

**Files:**
- Modify: `src/x_news.py` (append)
- Test: `tools/test_pipeline.py`

**Interfaces:**
- Consumes: `Config.x_search_enabled`, `Config.openai_api_key`, `Config.x_search_max_items` (Task 1); `_build_body`, `_extract_message`, `_citation_urls`, `_message_text` (Task 2); `_post_with_retries` (Task 3); `Item`, `normalize_url` (existing, `src/models.py`).
- Produces: `fetch_x_news(cfg: Config, since: datetime, client: httpx.Client | None = None) -> list[Item]`. Consumed by `sources.collect()` in Task 5.

Note on `since`: accepted for signature parity with `fetch_rss`/`fetch_hackernews` (so `collect()` can call all three identically) but not used to filter — `web_search` results carry no reliable original-publish timestamp, so every surviving item is stamped `datetime.now(timezone.utc)`, same fallback `_fetch_feed` already uses for RSS entries with no date. Cross-run dedup is handled entirely by `SeenStore`, same as today.

- [ ] **Step 1: Write the failing test**

Add to `tools/test_pipeline.py`, after the Task 3 assertions:

```python
# 12) x_news: fetch_x_news end-to-end (citation gate, disabled, sin key, JSON malo)
from src.x_news import fetch_x_news
from datetime import datetime as _dt, timezone as _tz

_now = _dt.now(_tz.utc)

# 12a) deshabilitado por defecto: no debe llamar a la red en absoluto
cfg_off = _load_config(_tmp_cfg_path)
cfg_off.x_search_enabled = False


def _handler_should_not_be_called(request):
    raise AssertionError("no debía llamar a la red con x_search deshabilitado")


_client_off = _httpx.Client(transport=_httpx.MockTransport(_handler_should_not_be_called))
assert fetch_x_news(cfg_off, _now, client=_client_off) == []
print("OK  x_news: deshabilitado no llama a la red")

# 12b) habilitado pero sin OPENAI_API_KEY: devuelve [] sin lanzar
os.environ.pop("OPENAI_API_KEY", None)
cfg_on_sin_key = _load_config(_tmp_cfg_path)
assert cfg_on_sin_key.x_search_enabled is True
assert fetch_x_news(cfg_on_sin_key, _now, client=_client_off) == []
print("OK  x_news: sin OPENAI_API_KEY devuelve [] sin llamar a la red")

# 12c) con key: citation gate descarta URL no citada, conserva la citada
os.environ["OPENAI_API_KEY"] = "sk-test-123"
cfg_on = _load_config(_tmp_cfg_path)


def _handler_mixed_citations(request):
    return _httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "items": [
                                        {"title": "Real", "url": "https://real.test/a", "source": "S"},
                                        {"title": "Inventada", "url": "https://fake.test/z", "source": "S"},
                                    ]
                                }
                            ),
                            "annotations": [
                                {"type": "url_citation", "url": "https://real.test/a", "title": "Real"}
                            ],
                        }
                    ],
                }
            ]
        },
    )


_client_mixed = _httpx.Client(transport=_httpx.MockTransport(_handler_mixed_citations))
result = fetch_x_news(cfg_on, _now, client=_client_mixed)
assert len(result) == 1
assert result[0].url == "https://real.test/a"
assert result[0].title == "Real"
assert result[0].tag == "TECH"
print("OK  x_news: citation gate descarta URLs no citadas")

# 12d) JSON malformado -> [] sin lanzar
def _handler_bad_json(request):
    return _httpx.Response(200, content=b"not json")


_client_bad = _httpx.Client(transport=_httpx.MockTransport(_handler_bad_json))
assert fetch_x_news(cfg_on, _now, client=_client_bad) == []
print("OK  x_news: respuesta no-JSON devuelve [] sin lanzar")

# 12e) sin mensaje de salida (p.ej. solo web_search_call) -> []
def _handler_no_message(request):
    return _httpx.Response(200, json={"output": [{"type": "web_search_call"}]})


_client_no_msg = _httpx.Client(transport=_httpx.MockTransport(_handler_no_message))
assert fetch_x_news(cfg_on, _now, client=_client_no_msg) == []
print("OK  x_news: respuesta sin mensaje devuelve []")

os.environ.pop("OPENAI_API_KEY", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tools/test_pipeline.py`
Expected: `ImportError: cannot import name 'fetch_x_news' from 'src.x_news'`

- [ ] **Step 3: Implement `fetch_x_news`**

Append to `src/x_news.py`:

```python
def fetch_x_news(
    cfg: Config, since: datetime, client: httpx.Client | None = None
) -> list[Item]:
    if not cfg.x_search_enabled:
        return []

    api_key = cfg.openai_api_key
    if api_key is None:
        log.warning("OPENAI_API_KEY no configurada; se omite la búsqueda en X.")
        return []

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = _build_body(cfg)

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=TIMEOUT)

    try:
        response = _post_with_retries(client, RESPONSES_ENDPOINT, headers, body)
    except httpx.HTTPError as exc:
        log.warning("No se pudo consultar noticias de X: %s", exc)
        return []
    finally:
        if owns_client:
            client.close()

    try:
        payload = response.json()
    except ValueError as exc:
        log.warning("Respuesta inválida de OpenAI: %s", exc)
        return []

    message = _extract_message(payload)
    if message is None:
        log.warning("Respuesta de OpenAI sin mensaje de salida.")
        return []

    citations = _citation_urls(message)
    if not citations:
        log.warning("Respuesta de OpenAI sin citas web_search; se descarta.")
        return []

    text = _message_text(message)
    if not text:
        log.warning("Respuesta de OpenAI sin contenido de texto.")
        return []

    try:
        parsed = json.loads(text)
        raw_items = parsed["items"]
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("JSON de OpenAI con forma inesperada: %s", exc)
        return []

    items: list[Item] = []
    for raw in raw_items:
        url = (raw.get("url") or "").strip()
        title = (raw.get("title") or "").strip()
        source = (raw.get("source") or "X").strip()
        if not url or not title:
            continue
        if normalize_url(url) not in citations:
            log.debug("Descartada por falta de cita: %s", url)
            continue
        items.append(
            Item(
                title=title,
                url=url,
                published=datetime.now(timezone.utc),
                source=source,
                tag="TECH",
            )
        )
        if len(items) >= cfg.x_search_max_items:
            break

    log.info("%-22s %2d nota(s) recientes", "X (Twitter)", len(items))
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tools/test_pipeline.py`
Expected: all `OK` lines print including the five new ones (12a-12e), no traceback.

- [ ] **Step 5: Commit**

```bash
git add src/x_news.py tools/test_pipeline.py
git commit -m "$(cat <<'EOF'
Add fetch_x_news with citation-matching anti-hallucination gate

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EGtw5uS2B2ouZz4s3oC4CX
EOF
)"
```

---

### Task 5: Wire into `sources.collect()`

**Files:**
- Modify: `src/sources.py:197-199` (`collect()`)
- Test: `tools/test_pipeline.py`

**Interfaces:**
- Consumes: `x_news.fetch_x_news(cfg, since, client=None) -> list[Item]` (Task 4).
- Produces: `sources.collect(cfg: Config) -> list[Item]` now includes X-sourced items (signature unchanged).

- [ ] **Step 1: Write the failing test**

Add to `tools/test_pipeline.py`, after the Task 4 assertions:

```python
# 13) collect(): concatena rss + hackernews + x_news (sin red real, con mocks)
from unittest.mock import patch as _patch
from src import sources as _sources

_item_rss = Item("R", "https://x.test/r", now, "RSS", "TECH")
_item_hn = Item("H", "https://x.test/h", now, "HN", "TECH")
_item_x = Item("X", "https://x.test/x", now, "X", "TECH")

with _patch.object(_sources, "fetch_rss", return_value=[_item_rss]), \
     _patch.object(_sources, "fetch_hackernews", return_value=[_item_hn]), \
     _patch("src.x_news.fetch_x_news", return_value=[_item_x]):
    collected = _sources.collect(cfg)

assert {i.title for i in collected} == {"R", "H", "X"}
print("OK  collect(): concatena las tres fuentes")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tools/test_pipeline.py`
Expected: `AssertionError` — `collected` is missing the "X" item because `collect()` doesn't call `fetch_x_news` yet (the patch on `src.x_news.fetch_x_news` has nothing to intercept in `collect()`'s call graph).

- [ ] **Step 3: Wire it in**

In `src/sources.py`, add the import and update `collect()` (currently lines 197-199):

At the top of the file, alongside the existing `from .models import Item` line, add:

```python
from . import x_news
```

Replace the `collect()` function:

```python
def collect(cfg: Config) -> list[Item]:
    since = datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)
    return fetch_rss(cfg, since) + fetch_hackernews(cfg, since) + x_news.fetch_x_news(cfg, since)
```

(The test patches `"src.x_news.fetch_x_news"` — calling it as `x_news.fetch_x_news(...)`, via the module reference rather than a `from .x_news import fetch_x_news` import, is what makes that patch target correct. A direct-name import would bind `fetch_x_news` into `sources`'s namespace at import time, and patching `src.x_news.fetch_x_news` afterward wouldn't affect that already-bound reference.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python tools/test_pipeline.py`
Expected: all `OK` lines print including the new one, no traceback.

- [ ] **Step 5: Commit**

```bash
git add src/sources.py tools/test_pipeline.py
git commit -m "$(cat <<'EOF'
Wire fetch_x_news into sources.collect()

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EGtw5uS2B2ouZz4s3oC4CX
EOF
)"
```

---

### Task 6: `feeds.yaml` documentation, workflow secret, final verification

**Files:**
- Modify: `feeds.yaml` (append documented, disabled-by-default example block)
- Modify: `.github/workflows/news.yml:33-37` (add `OPENAI_API_KEY` to the existing step's `env:`)

**Interfaces:**
- Consumes: nothing new — this task is documentation/wiring only, no new code.
- Produces: nothing consumed by later tasks (this is the last task).

- [ ] **Step 1: Add the documented example block to `feeds.yaml`**

Append at the end of `feeds.yaml`, after the existing `settings:` block:

```yaml

# ---------------------------------------------------------------------------
# Búsqueda en X (Twitter) vía OpenAI Responses API + web_search. Deshabilitado
# por defecto: es una fuente opcional que requiere OPENAI_API_KEY (secret de
# GitHub Actions o variable de entorno local) y tiene costo por corrida.
# Ver docs/superpowers/specs/2026-09-06-x-news-source-design.md
# ---------------------------------------------------------------------------
x_search:
  enabled: false
  queries:
    - "AI news trending on X twitter"
    - "cybersecurity news trending on X twitter"
  max_items: 5
  model: gpt-5.6-luna
  search_context_size: low
```

- [ ] **Step 2: Add the secret to the workflow**

In `.github/workflows/news.yml`, the "Enviar noticias" step currently reads (lines 33-37):

```yaml
      - name: Enviar noticias
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python -m src.main
```

Add one line to `env:`:

```yaml
      - name: Enviar noticias
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: python -m src.main
```

This is safe even while `x_search.enabled: false` — `fetch_x_news` returns `[]` before ever touching `cfg.openai_api_key` (Task 4, Step 3, first check). If the `OPENAI_API_KEY` secret isn't set in the repo, this resolves to an empty string in the environment, same as any unset GitHub secret — harmless.

- [ ] **Step 3: Run the full test suite**

Run: `python tools/test_pipeline.py`
Expected: all `OK` lines print (1 through 13 plus the config/x_news additions from Tasks 1-5), no traceback.

- [ ] **Step 4: Regression-check the real pipeline still runs (manual, network-dependent)**

Run: `python tools/check_feeds.py`
Expected: same feed health as before this plan (this plan touches no feed-fetching code — `fetch_rss`/`_fetch_feed` are untouched). This step is a smoke check that nothing in Tasks 1-5 broke an unrelated import path (e.g. a circular import between `sources.py` and `x_news.py`), not a new automated test.

- [ ] **Step 5: Commit**

```bash
git add feeds.yaml .github/workflows/news.yml
git commit -m "$(cat <<'EOF'
Document x_search config and wire OPENAI_API_KEY into the workflow

Disabled by default (x_search.enabled: false) — opt-in only. Users who
want the feature add OPENAI_API_KEY as a repo secret and flip the flag.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01EGtw5uS2B2ouZz4s3oC4CX
EOF
)"
```

**Note for whoever wants to actually turn this on:** flip `x_search.enabled: true` in `feeds.yaml`, add `OPENAI_API_KEY` as a GitHub repo secret, and set it locally (`.env` or shell export) for `--dry-run` testing. The two open risks from the spec still apply and haven't been re-verified against a live API call in this plan: (1) whether `web_search` + strict `text.format.json_schema` can coexist in one request is unconfirmed by OpenAI's docs — if the real API rejects the combination, `_build_body`'s `text.format` block needs to be dropped in favor of free-text parsing with a defensive `json.loads` + retry-with-correction prompt; (2) `web_search`'s coverage of X itself is indirect (mostly news coverage of what's trending on X, since X blocks generic crawlers) — this was an accepted tradeoff, not a bug to fix later.
