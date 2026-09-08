"""Fuente de noticias vía OpenAI Responses API + web_search: rastrea qué se
está discutiendo en X/Twitter sobre tech/IA/seguridad.

No se le pide al modelo una lista JSON estructurada de noticias -- eso
requeriría confiar en una URL que el propio modelo escribió, verificada
después contra las citas (y en la práctica, combinar `web_search` con un
`text.format` de json_schema estricto hace que la API deje de adjuntar
citas del todo: la búsqueda ocurre, pero las anotaciones vienen vacías).

En vez de eso, los `Item` se construyen directamente a partir de las citas
(`url_citation`) que la propia herramienta web_search devuelve: cada cita
ya trae su URL y su título reales, de una página que sí fue consultada. No
hay ninguna URL de por medio que no haya sido citada (ver
docs/superpowers/specs/2026-09-06-x-news-source-design.md).
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

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
    "herramienta de búsqueda web. Cita las fuentes reales que encuentres. "
    "Si no encuentras nada relevante, dilo brevemente y no inventes nada."
)


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
    }


def _extract_message(body: dict) -> dict | None:
    for entry in body.get("output", []):
        if entry.get("type") == "message":
            return entry
    return None


def _citations(message: dict) -> list[dict[str, str]]:
    """Extrae citas reales de web_search: cada una ya trae su propia url y
    title tal como la herramienta las devolvió. Deduplicadas por URL
    normalizada, se preserva la primera aparición."""
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for block in message.get("content", []):
        if not isinstance(block, dict):
            continue
        for annotation in block.get("annotations", []):
            if not isinstance(annotation, dict):
                continue
            url = annotation.get("url")
            if not isinstance(url, str) or not url:
                continue
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            title = annotation.get("title")
            result.append(
                {"url": url, "title": title if isinstance(title, str) and title else url}
            )
    return result


def _source_from_url(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "X"


def _exponential_backoff(attempt: int) -> float:
    """Full jitter: espera aleatoria entre 0 y 2**attempt (tope 30s)."""
    ceiling = min(MAX_BACKOFF_SECONDS, float(2**attempt))
    return random.uniform(0, ceiling)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, min(float(value), MAX_BACKOFF_SECONDS))
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
    except Exception as exc:
        # Cualquier fallo al llamar a OpenAI (red, timeout, status HTTP, o el
        # AssertionError "unreachable" de _post_with_retries ante un 2xx
        # inesperado que no sea 200) no debe abortar la corrida completa: es
        # una fuente opcional, RSS y Hacker News ya se recolectaron aparte.
        log.warning("No se pudo consultar noticias de X: %s", exc)
        return []
    finally:
        if owns_client:
            client.close()

    try:
        payload = response.json()
        message = _extract_message(payload)
        if message is None:
            log.warning("Respuesta de OpenAI sin mensaje de salida.")
            return []
        citations = _citations(message)
    except Exception as exc:
        # Cubre JSON inválido o forma inesperada (top-level no es un dict,
        # "output"/"content"/"annotations" con tipos que no son lista/dict,
        # etc.) que haría que .get()/[...] lance AttributeError/TypeError/
        # ValueError más adelante en el parseo.
        log.warning("Respuesta de OpenAI con forma inesperada: %s", exc)
        return []

    if not citations:
        log.warning("Respuesta de OpenAI sin citas web_search; no hay nada que reportar.")
        return []

    items: list[Item] = []
    for citation in citations:
        if cfg.x_search_max_items <= 0 or len(items) >= cfg.x_search_max_items:
            break
        items.append(
            Item(
                title=citation["title"],
                url=citation["url"],
                published=datetime.now(timezone.utc),
                source=_source_from_url(citation["url"]),
                tag="TECH",
            )
        )

    log.info("%-22s %2d nota(s) recientes", "X (Twitter)", len(items))
    return items
