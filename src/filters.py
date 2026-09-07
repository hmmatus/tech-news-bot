"""Reglas de etiquetado y relevancia."""
from __future__ import annotations

from .config import Config
from .models import Item

# Un tag más específico gana sobre uno genérico proveniente del feed.
_PRIORITY = ["CIBERSEGURIDAD", "IA", "LANZAMIENTO", "TECH"]


def _match_tag(item: Item, cfg: Config) -> str | None:
    haystack = f"{item.title} {item.source}".lower()
    for tag in _PRIORITY:
        for word in cfg.keywords.get(tag, []):
            if word in haystack:
                return tag
    return None


def classify(items: list[Item], cfg: Config) -> list[Item]:
    """Reasigna el tag por palabras clave y descarta lo irrelevante si require_keyword."""
    kept: list[Item] = []
    for item in items:
        matched = _match_tag(item, cfg)
        if matched:
            item.tag = matched
        elif cfg.require_keyword and item.tag == "TECH":
            continue
        kept.append(item)
    return kept


def rank(items: list[Item]) -> list[Item]:
    """Más reciente primero; a igual minuto, lo más votado en HN arriba."""
    return sorted(items, key=lambda i: (i.published, i.points or 0), reverse=True)
