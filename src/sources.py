"""Obtención de noticias: feeds RSS/Atom y Hacker News (Algolia)."""
from __future__ import annotations

import json
import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .config import Config, Feed
from .models import Item
from . import x_news

log = logging.getLogger(__name__)

# UA honesto: se identifica como bot, con contacto. Es lo que la mayoría de
# WAFs (Akamai, Cloudflare) esperan de un cliente automatizado legítimo.
USER_AGENT = "Mozilla/5.0 (compatible; tech-news-bot/1.0; +https://github.com/hmmatus)"
# Último recurso ante un 403: algunos WAFs bloquean cualquier UA que declare
# ser un bot, sin importar qué tan honesto sea.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HN_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"

MAX_RETRIES = 3
MAX_BACKOFF_SECONDS = 30.0


def _exponential_backoff(attempt: int) -> float:
    """Full jitter: espera aleatoria entre 0 y 2**attempt (tope 30s) para no
    re-sincronizar reintentos de todos los feeds a la vez."""
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


def fetch_bytes(client: httpx.Client, url: str, user_agent: str | None = None) -> bytes:
    """GET con reintentos: escala UA en 403, respeta Retry-After en 429,
    backoff exponencial con jitter en 5xx/errores de red. Máx 3 intentos."""
    ua = user_agent or USER_AGENT
    escalated = False
    last_exc: Exception | None = None
    response: httpx.Response | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.get(url, headers={"User-Agent": ua, "Accept": ACCEPT})
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(_exponential_backoff(attempt))
            continue

        if response.status_code == 200:
            return response.content

        if response.status_code == 403:
            if not escalated:
                escalated = True
                ua = BROWSER_UA
                continue
            response.raise_for_status()

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


def _entry_datetime(entry) -> datetime | None:
    """feedparser normaliza la fecha a UTC en *_parsed; probamos published y luego updated."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _fetch_feed(client: httpx.Client, feed: Feed, since: datetime) -> list[Item]:
    try:
        content = fetch_bytes(client, feed.url, user_agent=feed.user_agent)
    except httpx.HTTPError as exc:
        log.warning("No se pudo leer %s: %s", feed.name, exc)
        return []

    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        log.warning("Feed ilegible: %s (%s)", feed.name, parsed.get("bozo_exception"))
        return []

    items: list[Item] = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        published = _entry_datetime(entry)
        if published is None:
            # Sin fecha no podemos saber si es nueva; la deduplicación la atrapará luego.
            published = datetime.now(timezone.utc)
        if published < since:
            continue
        items.append(
            Item(title=title, url=link, published=published, source=feed.name, tag=feed.tag)
        )
    log.info("%-22s %2d nota(s) recientes", feed.name, len(items))
    return items


def fetch_rss(cfg: Config, since: datetime) -> list[Item]:
    if not cfg.feeds:
        return []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            batches = pool.map(lambda f: _fetch_feed(client, f, since), cfg.feeds)
        return [item for batch in batches for item in batch]


def fetch_hackernews(cfg: Config, since: datetime) -> list[Item]:
    """Historias de HN por encima de un umbral de puntos: filtro de relevancia barato."""
    if not cfg.hn_enabled:
        return []

    params = {
        "tags": "story",
        "numericFilters": f"created_at_i>{int(since.timestamp())},points>{cfg.hn_min_points}",
        "hitsPerPage": 50,
    }
    try:
        url = str(httpx.URL(HN_ENDPOINT, params=params))
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            content = fetch_bytes(client, url)
            hits = json.loads(content).get("hits", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("No se pudo leer Hacker News: %s", exc)
        return []

    items: list[Item] = []
    for hit in hits:
        title = (hit.get("title") or "").strip()
        # Las "Ask HN"/"Show HN" sin URL apuntan al hilo de discusión.
        url = (hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}").strip()
        if not title:
            continue
        items.append(
            Item(
                title=title,
                url=url,
                published=datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc),
                source="Hacker News",
                tag="TECH",
                points=hit.get("points"),
            )
        )
    log.info("%-22s %2d nota(s) recientes", "Hacker News", len(items))
    return items


def collect(cfg: Config) -> list[Item]:
    since = datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)
    return fetch_rss(cfg, since) + fetch_hackernews(cfg, since) + x_news.fetch_x_news(cfg, since)
