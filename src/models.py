"""Modelo común para una noticia, sin importar la fuente."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_", "mc_", "ref_")
_TRACKING_EXACT = {"ref", "source", "fbclid", "gclid", "igshid", "at_medium", "at_campaign"}


def normalize_url(url: str) -> str:
    """Quita parámetros de tracking para que la deduplicación funcione entre fuentes."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in _TRACKING_EXACT
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:120]


@dataclass
class Item:
    title: str
    url: str
    published: datetime  # siempre timezone-aware, en UTC
    source: str
    tag: str
    points: int | None = None

    @property
    def canonical_url(self) -> str:
        return normalize_url(self.url)

    @property
    def fingerprint(self) -> str:
        """Hash estable: la URL limpia identifica la nota; el título cubre republicaciones."""
        basis = f"{self.canonical_url}|{_slug(self.title)}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
