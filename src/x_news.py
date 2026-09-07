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
