# Hugo — Agente de control de calidad de catálogo (B2Box)

Hugo es el tercer agente del ecosistema B2Box. Su responsabilidad es **mantener la
base de datos de productos limpia, sin duplicados y con precios actualizados**.

## El ecosistema

```
       ┌──────────┐         ┌──────────┐         ┌──────────┐
       │  Luis    │ ──────▶ │  Hugo    │ ──────▶ │  Paco    │
       │ descubre │  check  │ verifica │  ok     │ enriquece│
       │ virales  │         │ duplicado│         │ y sube   │
       └──────────┘         └────┬─────┘         └────┬─────┘
                                 │                    │
                                 │                    ▼
                                 │              ┌──────────┐
                                 └─────────────▶│ Vendure  │
                                  audit + price │   (DB)   │
                                                └──────────┘
```

- **Luis** → busca productos virales (Alibaba/AliExpress/etc).
- **Paco** → enriquece datos y sube el producto a Vendure.
- **Hugo** → verifica que no se duplique y mantiene precios alineados con la fuente y los competidores.

## Qué hace Hugo

### 1. Anti-duplicados (3 capas, en orden de confianza)

1. **Source URL match** — si dos productos vienen del mismo `sourceUrl` (custom field en Vendure), son el mismo. Match exacto.
2. **Image perceptual hash** — `pHash` sobre las imágenes principales. Pesca duplicados aunque vengan de fuentes distintas.
3. **Fuzzy text** — similitud (`rapidfuzz`) sobre `name + description`. Última red.

El módulo `dedup/orchestrator.py` combina las tres y devuelve un score de confianza (0-1).

### 2. Comparación de precios

- **Fuente original**: re-fetch del precio en Alibaba/AliExpress vía `pricing/source_check.py`.
- **Competidores**: scraping configurable de tiendas competidoras (`pricing/competitor_check.py`).
- **Diff**: si la desviación supera el umbral configurado, dispara update + log.

### 3. Cuándo actúa

- **Tiempo real (webhook)**: Paco/Luis le pegan a `POST /verify` antes de subir/descubrir.
- **Programado (scheduler)**: APScheduler corre auditorías periódicas (configurable).
- **On-demand**: `POST /audit` para correr una auditoría completa manualmente.

### 4. Acción

- Auto-actualiza precios cuando la desviación es razonable.
- Marca duplicados de alta confianza como `disabled` en Vendure.
- Loguea TODO en SQLite (`AuditLog`).
- Manda email a `tech@b2box.pro` con resumen diario y alertas críticas.

## Estructura

```
backend/
├── app/
│   ├── main.py              # Entry point FastAPI
│   ├── config.py            # Settings (pydantic-settings)
│   ├── vendure/
│   │   └── client.py        # Cliente GraphQL Vendure Admin API
│   ├── dedup/
│   │   ├── url_match.py
│   │   ├── image_hash.py
│   │   ├── fuzzy_text.py
│   │   └── orchestrator.py
│   ├── pricing/
│   │   ├── source_check.py
│   │   ├── competitor_check.py
│   │   └── diff.py
│   ├── scheduler/
│   │   └── jobs.py          # APScheduler
│   ├── notifier/
│   │   └── email.py         # SMTP a tech@b2box.pro
│   ├── api/
│   │   └── routes.py        # /verify, /audit, /products/{id}/check
│   └── db/
│       ├── models.py        # SQLModel: PriceHistory, AuditLog
│       └── session.py
└── tests/
    ├── test_dedup_orchestrator.py
    └── test_pricing_diff.py
```

## Setup local (sin Docker)

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp ../.env.example ../.env
# editar .env con credenciales reales
uvicorn app.main:app --reload
```

## Run con Docker (recomendado para producción)

Requisitos: Docker + Docker Compose v2.

```bash
# 1) Asegurate que .env existe en la raíz del proyecto (mismo nivel que docker-compose.yml)
cp .env.example .env
# editar .env con las credenciales reales

# 2) Build + up en segundo plano
docker compose up -d --build

# 3) Ver logs en vivo
docker compose logs -f hugo

# 4) Verificar que está sano
curl http://localhost:8000/health
# → {"status":"ok","agent":"hugo"}

# 5) Disparar una auditoría on-demand (sin esperar al scheduler)
curl -X POST http://localhost:8000/audit
```

**Persistencia**: la DB vive en **Supabase** (Postgres managed). Sobrevive a
`docker compose down`, rebuilds, redeploys y a borrar el container completo.

**Updates** (cuando haya código nuevo):

```bash
git pull
docker compose up -d --build
```

Las migraciones de schema (columnas nuevas) son automáticas: al arrancar, Hugo
detecta columnas faltantes en las tablas existentes y hace `ALTER TABLE ADD COLUMN`.
No vas a tener que borrar la DB cada vez que crezca el modelo.

## Deploy en Coolify

1. **Conectar el repo** a Coolify (Settings → Sources → tu GitHub).
2. **Crear nuevo Resource** → "Application" → seleccionar este repo.
3. **Build pack**: Docker Compose (Coolify detecta el `docker-compose.yml` solo).
4. **Variables de entorno**: copiar el contenido de tu `.env` local en
   Coolify → Environment Variables. Las críticas:
   - `DATABASE_URL` (Supabase Session Pooler, ver más abajo)
   - `VENDURE_API_URL`, `VENDURE_BEARER`, `VENDURE_CHANNEL_TOKEN`
   - `RAPIDAPI_KEY`
   - `ALERT_SMTP_*` y `ALERT_EMAIL_TO`
5. **Domain**: asignar un dominio (ej. `hugo.b2box.app`) en Coolify.
6. **Deploy**.

Updates futuros: cada `git push` a `main` puede gatillar redeploy automático
si activás el webhook en Coolify.

## Connection string de Supabase

Para `DATABASE_URL`, ir a Supabase Dashboard:

1. Project Settings → Database → **Connection pooling**
2. Modo: **Session** (puerto `5432` vía pooler — soporta DDL para migraciones)
3. Copiar el URI y reemplazar `[YOUR-PASSWORD]` con la pass de la DB
4. Cambiar el prefijo `postgresql://` por `postgresql+psycopg://`

Resultado típico:

```
postgresql+psycopg://postgres.<project>:<pass>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

## Endpoints

- `GET  /health` — liveness probe (lo usa el healthcheck de Docker)
- `POST /verify` — Paco/Luis preguntan si un candidato es duplicado
- `POST /audit?target=all|duplicates|prices` — auditoría on-demand
- `GET  /products/{id}/check` — chequea un producto puntual (precio fuente)
- `GET  /audit-log?limit=N` — últimas N acciones (para dashboard)

## Variables de entorno

Ver `.env.example`. Las críticas:

- `VENDURE_API_URL`, `VENDURE_BEARER`, `VENDURE_CHANNEL_TOKEN` — Vendure Admin API.
- `RAPIDAPI_KEY` — proxy a 1688 vía OTAPI (sin esto Hugo no puede consultar precios fuente).
- `ALERT_SMTP_*`, `ALERT_EMAIL_TO` — notificaciones por email.
- `ALERT_WEBHOOK_URL` — opcional, Slack/Discord/n8n/CallMeBot.
- `DEDUP_*_THRESHOLD` — umbrales de confianza de cada estrategia (0-1).
- `PRICE_DRIFT_THRESHOLD` — % mínimo de variación que dispara alerta.
- `AUDIT_INTERVAL_HOURS` — cada cuánto corre la auditoría completa.
