"""Valida el pipeline con datos sintéticos (la red de este contenedor bloquea los feeds)."""
import json, sys, tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from src import filters, telegram
from src.config import load
from src.models import Item, normalize_url
from src.store import SeenStore
import feedparser

cfg = load()
now = datetime.now(timezone.utc)

# 1) parsing real de RSS con feedparser
rss = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Apple unveils M5 MacBook Pro</title><link>https://apple.com/a?utm_source=rss&amp;id=9</link>
<pubDate>{(now-timedelta(hours=2)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
<item><title>Critical CVE-2026-1234 exploited in the wild</title><link>https://x.test/b/</link>
<pubDate>{(now-timedelta(hours=1)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
</channel></rss>"""
p = feedparser.parse(rss.encode())
assert len(p.entries) == 2, p.entries
from src.sources import _entry_datetime
assert _entry_datetime(p.entries[0]).tzinfo is not None
print("OK  parsing RSS + fechas tz-aware")

# 2) normalización de URL y fingerprint
a = Item("Apple unveils M5 MacBook Pro", "https://apple.com/a?utm_source=rss&id=9", now, "Apple Newsroom", "LANZAMIENTO")
b = Item("Apple unveils M5 MacBook Pro", "https://APPLE.com/a/?id=9&fbclid=zz", now, "TechCrunch", "TECH")
assert a.canonical_url == b.canonical_url == "https://apple.com/a?id=9", a.canonical_url
assert a.fingerprint == b.fingerprint
print("OK  dedup entre fuentes (misma nota, distinto medio)")

# 3) clasificación por keywords
items = [
    Item("Critical CVE-2026-1234 exploited in the wild", "https://x.test/b", now, "TechCrunch", "TECH"),
    Item("OpenAI launches new reasoning model", "https://x.test/c", now, "TechCrunch", "TECH"),
    Item("Apple unveils M5 MacBook Pro", "https://x.test/d", now, "Apple Newsroom", "LANZAMIENTO"),
    Item("Some laptop review roundup", "https://x.test/e", now, "The Verge", "TECH"),
]
tags = [i.tag for i in filters.classify(items, cfg)]
assert tags == ["CIBERSEGURIDAD", "IA", "LANZAMIENTO", "TECH"], tags
print("OK  etiquetado por prioridad:", tags)

# 4) require_keyword descarta ruido genérico
cfg.require_keyword = True
assert len(filters.classify(items, cfg)) == 3
cfg.require_keyword = False

# 5) orden: más reciente primero
old = Item("vieja", "https://x.test/f", now-timedelta(hours=5), "S", "TECH")
assert filters.rank([old, items[0]])[0].title == items[0].title
print("OK  ranking por recencia")

# 6) estado persistente
tmp_state = Path(tempfile.mkdtemp()) / "seen.json"
store = SeenStore(tmp_state, retention_days=14)
assert store.is_new(items[0])
store.mark(items + [a]); store.save()
store2 = SeenStore(tmp_state)
assert not store2.is_new(items[0]) and not store2.is_new(b)
print("OK  persistencia y dedup entre corridas")

# 7) formato y chunking del mensaje
msgs = telegram.build_messages(items, cfg.timezone)
assert len(msgs) == 1 and all(len(m) <= 4096 for m in msgs)
assert "#CIBERSEGURIDAD" in msgs[0] and "UTC-6" in msgs[0]
muchos = [Item(f"Nota numero {n} con un titulo largo "*3, f"https://x.test/{n}", now, "Fuente", "IA") for n in range(120)]
chunks = telegram.build_messages(muchos, cfg.timezone)
assert len(chunks) > 1 and all(len(c) <= 4096 for c in chunks), [len(c) for c in chunks]
print(f"OK  chunking: {len(muchos)} notas -> {len(chunks)} mensajes, max {max(len(c) for c in chunks)} chars")

# 8) escapado de HTML (un título con < > & no debe romper parse_mode=HTML)
peligroso = Item("Bug in <script> & \"quotes\"", "https://x.test/z", now, "S", "TECH")
assert "&lt;script&gt;" in telegram.format_item(peligroso, cfg.timezone)
print("OK  escapado HTML")

print("\n--- muestra del mensaje ---")
print(msgs[0])
