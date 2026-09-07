"""Formato del mensaje y envío a Telegram."""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .models import Item

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_CHARS = 4096  # límite duro de Telegram por mensaje
MESES = "ene feb mar abr may jun jul ago sep oct nov dic".split()
EMOJI = {"IA": "🤖", "CIBERSEGURIDAD": "🛡", "LANZAMIENTO": "🚀", "TECH": "⚙"}


def _fecha(dt: datetime, tz: str) -> str:
    local = dt.astimezone(ZoneInfo(tz))
    offset = local.utcoffset()
    hours = int(offset.total_seconds() // 3600) if offset else 0
    return f"{local.day:02d} {MESES[local.month - 1]} {local.year}, {local:%H:%M} (UTC{hours:+d})"


def format_item(item: Item, tz: str) -> str:
    titulo = html.escape(item.title)
    fuente = html.escape(item.source)
    puntos = f" · {item.points} pts" if item.points else ""
    return (
        f"{EMOJI.get(item.tag, '📰')} <b>{titulo}</b>\n"
        f"🔗 {html.escape(item.url)}\n"
        f"🕒 {_fecha(item.published, tz)} · {fuente}{puntos}\n"
        f"🏷 #{item.tag}"
    )


def build_messages(items: list[Item], tz: str) -> list[str]:
    """Agrupa varias notas por mensaje sin pasar el límite de caracteres."""
    encabezado = f"📡 <b>Radar tech</b> · {_fecha(datetime.now(ZoneInfo(tz)), tz)}"
    bloques = [format_item(i, tz) for i in items]

    mensajes: list[str] = []
    actual = encabezado
    for bloque in bloques:
        candidato = f"{actual}\n\n{bloque}"
        if len(candidato) > MAX_CHARS:
            mensajes.append(actual)
            actual = bloque
        else:
            actual = candidato
    mensajes.append(actual)
    return mensajes


def send(token: str, chat_id: str, texto: str, *, intentos: int = 3) -> None:
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    with httpx.Client(timeout=30.0) as client:
        for intento in range(1, intentos + 1):
            response = client.post(API.format(token=token), json=payload)
            if response.status_code == 200:
                return
            if response.status_code == 429:
                espera = response.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram pide esperar %ss; reintentando.", espera)
                time.sleep(espera + 1)
                continue
            if response.status_code >= 500 and intento < intentos:
                time.sleep(2 * intento)
                continue
            raise RuntimeError(f"Telegram respondió {response.status_code}: {response.text}")
    raise RuntimeError("Telegram no aceptó el mensaje tras varios intentos.")


def deliver(token: str, chat_id: str, items: list[Item], tz: str) -> int:
    mensajes = build_messages(items, tz)
    for i, mensaje in enumerate(mensajes):
        send(token, chat_id, mensaje)
        if i < len(mensajes) - 1:
            time.sleep(1)  # margen ante el rate limit de Telegram
    return len(mensajes)
