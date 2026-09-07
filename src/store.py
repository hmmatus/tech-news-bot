"""Estado de deduplicación persistido en JSON (se commitea de vuelta al repo)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Item

log = logging.getLogger(__name__)


class SeenStore:
    def __init__(self, path: str | Path, retention_days: int = 14):
        self.path = Path(path)
        self.retention_days = retention_days
        self._seen: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.info("Sin estado previo en %s: primera ejecución.", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._seen = dict(data.get("seen", {}))
        except (json.JSONDecodeError, OSError) as exc:
            # Un estado corrupto no debe romper la corrida; peor caso, se repite una nota.
            log.warning("Estado ilegible (%s); se empieza de cero.", exc)
            self._seen = {}

    def is_new(self, item: Item) -> bool:
        return item.fingerprint not in self._seen

    def mark(self, items: list[Item]) -> None:
        stamp = datetime.now(timezone.utc).isoformat()
        for item in items:
            self._seen[item.fingerprint] = stamp

    def _prune(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        fresh = {}
        for key, stamp in self._seen.items():
            try:
                if datetime.fromisoformat(stamp) >= cutoff:
                    fresh[key] = stamp
            except ValueError:
                continue
        removed = len(self._seen) - len(fresh)
        self._seen = fresh
        return removed

    def save(self) -> None:
        removed = self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(self._seen),
            "seen": self._seen,
        }
        # Escritura atómica: evita dejar un JSON a medias si el job se cancela.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        log.info("Estado guardado: %d notas vistas (%d purgadas).", len(self._seen), removed)
