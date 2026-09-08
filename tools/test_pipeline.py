"""Valida el pipeline con datos sintéticos (la red de este contenedor bloquea los feeds)."""
import json, sys, tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src import filters, telegram
from src.config import load
from src.models import Item, normalize_url
from src.store import SeenStore
import feedparser

cfg = load()
now = datetime.now(timezone.utc)

# 1) parsing real de RSS con feedparser
rss = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Apple unveils M5 MacBook Pro</title><link>https://apple.com/a?utm_source=rss&amp;id=9</link>
<pubDate>{(now-timedelta(hours=2)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
<item><title>Critical CVE-2026-1234 exploited in the wild</title><link>https://x.test/b/</link>
<pubDate>{(now-timedelta(hours=1)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
</channel></rss>"""
p = feedparser.parse(rss.encode())
assert len(p.entries) == 2, p.entries
from src.sources import _entry_datetime
assert _entry_datetime(p.entries[0]).tzinfo is not None
print("OK  parsing RSS + fechas tz-aware")

# 2) normalización de URL y fingerprint
a = Item("Apple unveils M5 MacBook Pro", "https://apple.com/a?utm_source=rss&id=9", now, "Apple Newsroom", "LANZAMIENTO")
b = Item("Apple unveils M5 MacBook Pro", "https://APPLE.com/a/?id=9&fbclid=zz", now, "TechCrunch", "TECH")
assert a.canonical_url == b.canonical_url == "https://apple.com/a?id=9", a.canonical_url
assert a.fingerprint == b.fingerprint
print("OK  dedup entre fuentes (misma nota, distinto medio)")

# 3) clasificación por keywords
items = [
    Item("Critical CVE-2026-1234 exploited in the wild", "https://x.test/b", now, "TechCrunch", "TECH"),
    Item("OpenAI launches new reasoning model", "https://x.test/c", now, "TechCrunch", "TECH"),
    Item("Apple unveils M5 MacBook Pro", "https://x.test/d", now, "Apple Newsroom", "LANZAMIENTO"),
    Item("Some laptop review roundup", "https://x.test/e", now, "The Verge", "TECH"),
]
tags = [i.tag for i in filters.classify(items, cfg)]
assert tags == ["CIBERSEGURIDAD", "IA", "LANZAMIENTO", "TECH"], tags
print("OK  etiquetado por prioridad:", tags)

# 4) require_keyword descarta ruido genérico
cfg.require_keyword = True
assert len(filters.classify(items, cfg)) == 3
cfg.require_keyword = False

# 5) orden: más reciente primero
old = Item("vieja", "https://x.test/f", now-timedelta(hours=5), "S", "TECH")
assert filters.rank([old, items[0]])[0].title == items[0].title
print("OK  ranking por recencia")

# 6) estado persistente
tmp_state = Path(tempfile.mkdtemp()) / "seen.json"
store = SeenStore(tmp_state, retention_days=14)
assert store.is_new(items[0])
store.mark(items + [a]); store.save()
store2 = SeenStore(tmp_state)
assert not store2.is_new(items[0]) and not store2.is_new(b)
print("OK  persistencia y dedup entre corridas")

# 7) formato y chunking del mensaje
msgs = telegram.build_messages(items, cfg.timezone)
assert len(msgs) == 1 and all(len(m) <= 4096 for m in msgs)
assert "#CIBERSEGURIDAD" in msgs[0] and "UTC-6" in msgs[0]
muchos = [Item(f"Nota numero {n} con un titulo largo "*3, f"https://x.test/{n}", now, "Fuente", "IA") for n in range(120)]
chunks = telegram.build_messages(muchos, cfg.timezone)
assert len(chunks) > 1 and all(len(c) <= 4096 for c in chunks), [len(c) for c in chunks]
print(f"OK  chunking: {len(muchos)} notas -> {len(chunks)} mensajes, max {max(len(c) for c in chunks)} chars")

# 8) escapado de HTML (un título con < > & no debe romper parse_mode=HTML)
peligroso = Item("Bug in <script> & \"quotes\"", "https://x.test/z", now, "S", "TECH")
assert "&lt;script&gt;" in telegram.format_item(peligroso, cfg.timezone)
print("OK  escapado HTML")

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

# 10) x_news: helpers de construcción de request y parseo de respuesta
from src.x_news import _build_body, _extract_message, _citations, _source_from_url

cfg_x = _load_config(_tmp_cfg_path)  # el que tiene x_search habilitado, del bloque anterior
body = _build_body(cfg_x)
assert body["model"] == "gpt-6-astra"
assert body["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
assert "text" not in body, "no debe pedirse json_schema: web_search + strict schema deja las citas vacías"
assert "AI news trending on X twitter" in body["input"][1]["content"]
print("OK  x_news: _build_body arma el request correctamente (sin json_schema)")

_fake_response_body = {
    "output": [
        {"type": "web_search_call", "id": "ws_1"},
        {
            "type": "message",
            "content": [
                {
                    "text": "Aquí hay una nota real sobre IA.",
                    "annotations": [
                        {"type": "url_citation", "url": "https://real.test/a", "title": "T"},
                        {"type": "url_citation", "url": "https://real.test/a?utm_source=x", "title": "T dup"},
                        {"type": "url_citation", "url": "https://real.test/b", "title": "T2"},
                    ],
                }
            ],
        },
    ]
}
msg = _extract_message(_fake_response_body)
assert msg is not None and msg["type"] == "message"
cites = _citations(msg)
assert cites == [
    {"url": "https://real.test/a", "title": "T"},
    {"url": "https://real.test/b", "title": "T2"},
], cites
print("OK  x_news: _extract_message/_citations parsean la respuesta y deduplican por URL normalizada")

assert _extract_message({"output": []}) is None
assert _citations({"content": []}) == []
print("OK  x_news: helpers devuelven vacío/None ante respuesta sin datos")

assert _source_from_url("https://www.TechCrunch.com/a/b") == "techcrunch.com"
assert _source_from_url("https://real.test/a") == "real.test"
print("OK  x_news: _source_from_url deriva el dominio de la cita")

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

print("\n--- muestra del mensaje ---")
print(msgs[0])

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

# 12c) con key: los Item se construyen directamente desde las citas de
# web_search (título y URL vienen de la propia anotación, no de JSON
# generado por el modelo), deduplicadas por URL normalizada.
os.environ["OPENAI_API_KEY"] = "sk-test-123"
cfg_on = _load_config(_tmp_cfg_path)


def _handler_real_citations(request):
    return _httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "text": "Aquí hay noticias reales sobre IA y seguridad.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://techcrunch.com/a",
                                    "title": "Real",
                                },
                                {
                                    "type": "url_citation",
                                    "url": "https://techcrunch.com/a?utm_source=openai",
                                    "title": "Real dup",
                                },
                            ],
                        }
                    ],
                }
            ]
        },
    )


_client_real = _httpx.Client(transport=_httpx.MockTransport(_handler_real_citations))
result = fetch_x_news(cfg_on, _now, client=_client_real)
assert len(result) == 1, result
assert result[0].url == "https://techcrunch.com/a"
assert result[0].title == "Real"
assert result[0].source == "techcrunch.com"
assert result[0].tag == "TECH"
print("OK  x_news: Item se construye desde la cita real, deduplicado")

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

# 12f) mensaje sin ninguna cita (web_search no encontró/citó nada) -> []
def _handler_no_citations(request):
    return _httpx.Response(
        200,
        json={
            "output": [
                {"type": "message", "content": [{"text": "No encontré nada relevante.", "annotations": []}]}
            ]
        },
    )


_client_no_cit = _httpx.Client(transport=_httpx.MockTransport(_handler_no_citations))
assert fetch_x_news(cfg_on, _now, client=_client_no_cit) == []
print("OK  x_news: sin citas web_search devuelve []")

# 12i) crash-hardening: top-level de la respuesta es una lista, no un dict
def _handler_top_level_list(request):
    return _httpx.Response(200, json=[1, 2, 3])


_client_top_list = _httpx.Client(transport=_httpx.MockTransport(_handler_top_level_list))
assert fetch_x_news(cfg_on, _now, client=_client_top_list) == []
print("OK  x_news: top-level JSON es una lista devuelve [] sin lanzar")

# 12j) crash-hardening: HTTP 204 (2xx que no es 200) no debe reventar el
# guard "unreachable" de _post_with_retries
def _handler_204(request):
    return _httpx.Response(204)


_client_204 = _httpx.Client(transport=_httpx.MockTransport(_handler_204))
assert fetch_x_news(cfg_on, _now, client=_client_204) == []
print("OK  x_news: HTTP 204 devuelve [] sin lanzar AssertionError")

# 12k) crash-hardening: 429 con Retry-After negativo no debe llegar a
# time.sleep() con un valor negativo
def _handler_429_negative_retry_after(request):
    return _httpx.Response(429, headers={"Retry-After": "-5"}, json={})


_client_429_neg = _httpx.Client(transport=_httpx.MockTransport(_handler_429_negative_retry_after))
assert fetch_x_news(cfg_on, _now, client=_client_429_neg) == []
print("OK  x_news: 429 con Retry-After negativo devuelve [] sin lanzar ValueError")

# 12l) max_items <= 0 no debe colar ningún item (el chequeo del tope debe
# aplicarse ANTES de aceptar un item, no después)
cfg_zero_max = _load_config(_tmp_cfg_path)
cfg_zero_max.x_search_max_items = 0
_client_zero_max = _httpx.Client(transport=_httpx.MockTransport(_handler_real_citations))
assert fetch_x_news(cfg_zero_max, _now, client=_client_zero_max) == []
print("OK  x_news: x_search_max_items=0 no produce ningún item")

os.environ.pop("OPENAI_API_KEY", None)

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
