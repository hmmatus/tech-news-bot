"""Punto de entrada: python -m src.main [--dry-run]"""
from __future__ import annotations

import argparse
import logging
import sys

from . import filters, sources, telegram
from .config import ROOT, load
from .store import SeenStore

log = logging.getLogger("bot")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Radar de noticias tech a Telegram.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime en consola sin enviar a Telegram ni tocar el estado.",
    )
    parser.add_argument("--config", default=str(ROOT / "feeds.yaml"))
    parser.add_argument("--state", default=str(ROOT / "state" / "seen.json"))
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    cfg = load(args.config)
    store = SeenStore(args.state, cfg.state_retention_days)

    crudos = sources.collect(cfg)
    log.info("Recolectadas %d notas en las últimas %dh.", len(crudos), cfg.lookback_hours)

    relevantes = filters.classify(crudos, cfg)
    nuevas, vistos = [], set()
    for item in filters.rank(relevantes):
        # Dedup contra el historial y contra duplicados dentro de la misma corrida.
        if item.fingerprint in vistos or not store.is_new(item):
            continue
        vistos.add(item.fingerprint)
        nuevas.append(item)

    nuevas = nuevas[: cfg.max_items_per_run]
    if not nuevas:
        log.info("Nada nuevo que enviar.")
        return 0

    if args.dry_run:
        print("\n" + "\n\n".join(telegram.build_messages(nuevas, cfg.timezone)))
        log.info("Dry-run: %d nota(s), sin enviar ni guardar estado.", len(nuevas))
        return 0

    enviados = telegram.deliver(cfg.telegram_token, cfg.telegram_chat_id, nuevas, cfg.timezone)
    store.mark(nuevas)
    store.save()
    log.info("Enviadas %d nota(s) en %d mensaje(s).", len(nuevas), enviados)
    return 0


if __name__ == "__main__":
    sys.exit(main())
