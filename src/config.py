"""Carga y validación de configuración."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Feed:
    name: str
    url: str
    tag: str
    user_agent: str | None = None


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


def _env_bool_override(name: str) -> bool | None:
    """Lee un booleano desde una variable de entorno; None si no está
    definida (o está vacía), para que el valor de feeds.yaml siga
    aplicando. Definida, sobreescribe el yaml en cualquier dirección."""
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return None
    return value not in ("0", "false", "no", "off")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Falta la variable de entorno {name}. "
            "Defínela en GitHub Secrets o en tu archivo .env local."
        )
    return value


def load(path: str | Path = ROOT / "feeds.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    feeds = []
    for entry in raw.get("feeds", []):
        missing = {"name", "url", "tag"} - entry.keys()
        if missing:
            raise SystemExit(f"Feed inválido en {path}: faltan campos {sorted(missing)}")
        feeds.append(
            Feed(
                name=entry["name"],
                url=entry["url"],
                tag=entry["tag"].upper(),
                user_agent=entry.get("user_agent"),
            )
        )

    if not feeds and not raw.get("settings", {}).get("hn_enabled", True):
        raise SystemExit("No hay fuentes configuradas: agrega feeds o habilita Hacker News.")

    settings = raw.get("settings", {}) or {}
    keywords = {k.upper(): [w.lower() for w in v] for k, v in (raw.get("keywords") or {}).items()}
    x_search = raw.get("x_search", {}) or {}
    x_search_enabled = bool(x_search.get("enabled", False))
    env_override = _env_bool_override("X_SEARCH_ENABLED")
    if env_override is not None:
        x_search_enabled = env_override

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
        x_search_enabled=x_search_enabled,
        x_search_queries=[str(q) for q in (x_search.get("queries") or [])],
        x_search_max_items=int(x_search.get("max_items", 5)),
        x_search_model=str(x_search.get("model", "gpt-5.6-luna")),
        x_search_context_size=str(x_search.get("search_context_size", "low")),
    )
