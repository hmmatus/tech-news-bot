# Radar tech → Telegram

Bot que revisa feeds de tecnología (IA, ciberseguridad, lanzamientos de big tech) más Hacker News, y publica en un chat de Telegram cada nota nueva con **titular, enlace, fecha y hora, y tag**.

Corre en **GitHub Actions** con cron: sin servidor, sin costo. El estado de deduplicación se commitea al propio repo, así que nunca te repite una nota.

```
📡 Radar tech · 07 sep 2026, 06:00 (UTC-6)

🛡 Critical CVE-2026-1234 exploited in the wild
🔗 https://www.bleepingcomputer.com/...
🕒 07 sep 2026, 04:12 (UTC-6) · BleepingComputer
🏷 #CIBERSEGURIDAD

🤖 OpenAI launches new reasoning model
🔗 https://openai.com/news/...
🕒 07 sep 2026, 03:40 (UTC-6) · OpenAI
🏷 #IA
```

---

## Paso 1 — Crear el bot en Telegram

1. Abre Telegram y busca **@BotFather**.
2. `/newbot` → nombre visible → username terminado en `bot` (ej. `hmatus_radar_bot`).
3. BotFather te devuelve el **token**: `123456789:AAH...`. Guárdalo, es la credencial completa del bot.

## Paso 2 — Obtener el `chat_id` destino

**Chat privado contigo:**

1. Envíale cualquier mensaje a tu bot desde tu cuenta (el bot no puede escribirte primero).
2. Abre en el navegador:
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`
3. Copia `result[0].message.chat.id` — un número positivo, ej. `87654321`.

**Canal o grupo** (útil si luego quieres compartir el radar):

1. Agrega el bot al canal/grupo **como administrador**.
2. Publica un mensaje ahí y vuelve a llamar `getUpdates`.
3. El id empieza con `-100`, ej. `-1001234567890`. Inclúyelo con el signo negativo.

## Paso 3 — Repo y prueba local

```bash
git init tech-news-bot && cd tech-news-bot   # o descomprime este proyecto aquí
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Verifica que todas las fuentes respondan desde tu red
python tools/check_feeds.py

# Ver qué enviaría, sin tocar Telegram ni el estado
python -m src.main --dry-run
```

Cuando el dry-run se vea bien, prueba el envío real:

```bash
cp .env.example .env      # pega ahí tu token y chat_id
set -a && source .env && set +a
python -m src.main
```

> Si `check_feeds.py` marca **FALLA 403** en algún medio, ese sitio bloquea clientes no-navegador. Cambia `USER_AGENT` en `src/sources.py` por un UA de Chrome, o quita ese feed. No rompe al resto: cada fuente falla de forma aislada.

## Paso 4 — Subir a GitHub

```bash
git add -A && git commit -m "feat: bot de radar tech"
gh repo create tech-news-bot --private --source=. --push
```

El repo **debe ser privado o público, da igual, pero el token nunca va en el código** — solo en Secrets.

## Paso 5 — Configurar los secrets

En GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Nombre | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | el token de BotFather |
| `TELEGRAM_CHAT_ID` | el id del paso 2 |
| `OPENAI_API_KEY` | opcional — solo si activas `x_search.enabled: true` en `feeds.yaml` (ver [Sobre X (Twitter)](#sobre-x-twitter)) |

## Paso 6 — Permitir que el workflow commitee el estado

**Settings → Actions → General → Workflow permissions** → marca **Read and write permissions** → Save.

Sin esto, el bot envía las noticias pero no puede guardar `state/seen.json`, y en la siguiente corrida te repite todo.

## Paso 7 — Primera ejecución

**Actions → Radar tech → Telegram → Run workflow**. Debes recibir el mensaje en menos de un minuto.

Ya activo, el cron corre a las **06:00, 10:00, 14:00 y 18:00** hora de El Salvador.

---

## Ajustes

**Cambiar horarios** — `.github/workflows/news.yml`, línea del `cron`. Está en **UTC**: resta 6 horas a tu hora local para obtener la UTC. Si cambias la frecuencia, sube o baja `lookback_hours` en `feeds.yaml` para que quede algo mayor que el intervalo.

**Agregar o quitar fuentes** — `feeds.yaml`, sección `feeds`. Cada entrada necesita `name`, `url` y `tag` (`IA`, `CIBERSEGURIDAD`, `LANZAMIENTO` o `TECH`). Corre `tools/check_feeds.py` después de editar.

**Menos ruido** — pon `require_keyword: true` en `settings`: descarta lo que venga de un feed genérico y no coincida con ninguna palabra clave. También puedes bajar `max_items_per_run` o subir `hn_min_points`.

**Afinar los tags** — la sección `keywords` reclasifica por título. La prioridad es `CIBERSEGURIDAD > IA > LANZAMIENTO > TECH`: una nota de TechCrunch sobre un CVE sale como `#CIBERSEGURIDAD` aunque el feed sea genérico.

---

## Problemas comunes

| Síntoma | Causa |
|---|---|
| El cron no dispara | GitHub deshabilita workflows programados tras **60 días sin actividad en el repo**. El commit de estado de cada corrida cuenta como actividad, así que solo pasa si el bot estuvo mucho tiempo sin enviar nada. Reactívalo desde la pestaña Actions. |
| Notas repetidas | Falta el permiso del Paso 6, o el commit de estado falla. Revisa el log del step "Guardar estado". |
| `Telegram respondió 400: chat not found` | `chat_id` mal copiado; para canales debe incluir el `-100`. |
| `Telegram respondió 403` | El bot no está en el grupo/canal, o no es administrador. |
| Llega poco o nada | Normal fuera de horario laboral de EE.UU. Baja `hn_min_points` y sube `lookback_hours` si quieres más volumen. |
| El cron llega tarde | GitHub retrasa los cron hasta ~15 min en horas pico. No se pierden notas: `lookback_hours` cubre el retraso. |

## Estructura

```
src/config.py     carga feeds.yaml y valida las variables de entorno
src/models.py     Item + normalización de URL y fingerprint para dedup
src/sources.py    lectura de RSS (paralela) y de Hacker News vía Algolia
src/x_news.py     noticias de X (Twitter) vía OpenAI Responses API + web_search (opcional)
src/filters.py    reclasificación por keywords y orden por recencia
src/store.py      estado JSON de notas ya enviadas, con purga por antigüedad
src/telegram.py   formato del mensaje, escapado HTML y corte a 4096 chars
src/main.py       orquestación y flag --dry-run
tools/            verificador de feeds y pruebas del pipeline
```

## Sobre X (Twitter)

X ya no se descarta como fuente: desde julio de 2023 la API oficial de lectura no tiene tier gratuito (el plan Basic ronda los **200 USD/mes**), y hacer scraping directo rompe los ToS y se cae cada vez que X cambia el frontend. En vez de eso, `src/x_news.py` usa la herramienta `web_search` de la **Responses API de OpenAI** para encontrar qué se está discutiendo ahora mismo en/sobre X en tecnología, IA y ciberseguridad — sin credenciales de X ni scraping.

Es una fuente **opcional y desactivada por defecto** (`x_search.enabled: false` en `feeds.yaml`): actívala solo si aceptas el costo por corrida de llamar a la API de OpenAI. Como salvaguarda, ninguna nota se construye a partir de una URL que el modelo simplemente haya mencionado: solo se aceptan URLs que aparezcan como citación real (`url_citation`) devuelta por la propia herramienta `web_search`, es decir, páginas que de verdad fueron consultadas.
