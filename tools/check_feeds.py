"""Verifica que cada feed de feeds.yaml responda y traiga entradas.

    python tools/check_feeds.py
    python tools/check_feeds.py cisa   # filtra por coincidencia parcial de nombre

Úsalo tras editar feeds.yaml: algunos medios devuelven 403 a clientes sin
User-Agent de navegador, y así lo detectas antes de que el cron falle en silencio.
"""
from __future__ import annotations

import sys
from pathlib import Path

import feedparser
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import load  # noqa: E402
from src.sources import ACCEPT, BROWSER_UA, TIMEOUT, USER_AGENT  # noqa: E402


def _ua_label(ua: str) -> str:
    if ua == USER_AGENT:
        return "default"
    if ua == BROWSER_UA:
        return "browser-fallback"
    return "custom"


def _probe(client: httpx.Client, url: str, preferred_ua: str) -> tuple[httpx.Response, str]:
    """Prueba con el UA preferido; si da 403, reintenta una vez con BROWSER_UA."""
    ua = preferred_ua
    response = client.get(url, headers={"User-Agent": ua, "Accept": ACCEPT})
    if response.status_code == 403 and ua != BROWSER_UA:
        ua = BROWSER_UA
        response = client.get(url, headers={"User-Agent": ua, "Accept": ACCEPT})
    return response, ua


def main() -> int:
    cfg = load()
    feeds = cfg.feeds
    if len(sys.argv) > 1:
        needle = sys.argv[1].lower()
        feeds = [f for f in feeds if needle in f.name.lower()]
        if not feeds:
            print(f"Sin coincidencias para {sys.argv[1]!r}.")
            return 1

    fallos = 0
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for feed in feeds:
            preferred_ua = feed.user_agent or USER_AGENT
            try:
                response, ua = _probe(client, feed.url, preferred_ua)
                response.raise_for_status()
                n = len(feedparser.parse(response.content).entries)
                sufijo = f"[{response.status_code} UA={_ua_label(ua)}]"
                if n:
                    estado = f"OK   {n:3d} entradas  {sufijo}"
                else:
                    estado = f"VACÍO  (responde pero sin items)  {sufijo}"
                    fallos += 1
            except httpx.HTTPError as exc:
                estado = f"FALLA  {exc}"
                fallos += 1
            print(f"{feed.name:<22} [{feed.tag:<15}] {estado}")

    print(f"\n{len(feeds) - fallos}/{len(feeds)} feeds sanos.")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
