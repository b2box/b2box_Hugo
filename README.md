# Hugo — Agente de control de calidad de catálogo (B2Box)

Hugo es el tercer agente del ecosistema B2Box. Su responsabilidad es **mantener la
base de datos de productos limpia y sin duplicados, y avisar cuando el precio del
proveedor cambia**. Hugo no modifica precios de venta en Vendure.

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
- **Hugo** → verifica que no se duplique y vigila el precio del proveedor (1688) para avisar cuando cambia.

## Qué hace Hugo

### 1. Anti-duplicados (3 capas, en orden de confianza)

1. **Source URL match** — si dos productos vienen del mismo `sourceUrl` (custom field en Vendure), son el mismo. Match exacto.
2. **Image perceptual hash** — `pHash` sobre las imágenes principales. Pesca duplicados aunque vengan de fuentes distintas.
3. **Fuzzy text** — similitud (`rapidfuzz`) sobre `name + description`. Última red.

El módulo `dedup/orchestrator.py` combina las tres y devuelve un score de confianza (0-1).

### 2. Vigilancia de precios fuente

- **Fuente original**: re-fetch del precio del proveedor (1688 vía OTAPI;
  Alibaba/AliExpress por JSON-LD) en `pricing/source_check.py`, con un budget
  diario de llamadas a OTAPI.
- **Diff**: cada precio se guarda como snapshot (`PriceHistory`); si la variación
  contra el snapshot anterior supera `PRICE_DRIFT_THRESHOLD`, se crea un evento
  `price_flagged` y se manda la alerta.

Hugo **no toca el precio de venta** en Vendure: solo avisa. Tampoco compara
contra competidores: `pricing/competitor_check.py` existe pero no tiene ningún
llamador (ni job, ni endpoint); es código muerto que quedó de la idea original.

### 3. Cuándo actúa

- **Tiempo real (webhook)**: Paco/Luis le pegan a `POST /verify` antes de subir/descubrir.
- **Programado (scheduler)**: APScheduler corre auditorías cada
  `AUDIT_INTERVAL_HOURS` (default 336 h = 14 días). La última corrida de cada
  auditoría se guarda en la DB (`settings`, clave `_meta:last_run:<job>`), así
  un redeploy **no reinicia el reloj**: al arrancar, la próxima corrida se
  calcula como última + intervalo (si ya venció, corre 5 min después de
  levantar). La primera vez, sin marcador, espera un intervalo completo.
- **On-demand**: `POST /audit` para correr una auditoría completa manualmente.

### 4. Búsqueda por imagen del b2box app (`POST /app/lookup`)

El app manda **una URL** y Hugo responde si el producto está en el catálogo.

```
URL del cliente (MercadoLibre / Alibaba / 1688 / AliExpress / foto propia)
   │
   ▼  ingest/image_from_url.py — saca la foto (JSON-LD → og:image → regex de CDN)
   ▼  match contra el catálogo:
        1. source URL exacta          (gratis, seguro)
        2. embeddings CLIP            (mismo producto aunque sea otra foto)
        3. pHash                      (fallback si el modelo no está)
   │
   ├── match → PA (código de variante) + precio + botón "comprar ahora"
   └── sin match → se abre el formulario del app en Cloud_B2BOX
```

Sin match, Hugo abre **el mismo formulario que ya usa el app**: la edge function
`form-app-submit` de `b2b-flow-pro`, que escribe en `form_app_consultations` +
`form_app_consultation_products`. Aparece en la sección Forms del tablero, igual
que si el cliente lo hubiera cargado a mano. El link original va en
`reference_link`, la foto en `image_urls`, y Hugo agrega en `notes` por qué no lo
encontró y cuál fue el mejor candidato del catálogo.

Como el formulario exige **nombre, email y teléfono**, el app tiene que mandarlos
en `client`. Si faltan, Hugo no llama a Cloud y devuelve `cloud_request.missing_fields`
para que el app los pida y reintente.

> **Dos límites de `form-app-submit` que hay que mirar antes de escalar:**
>
> * `checkRateLimit` permite **5 submissions cada 10 min por IP**. Está pensado
>   para un browser, pero Hugo es un solo servidor: del 6º lookup sin match en
>   10 minutos en adelante, Cloud responde 429. Para volumen real hace falta un
>   bypass server-to-server en la edge function.
> * `verifyRecaptcha` corre en modo `monitor` por defecto y deja pasar a Hugo.
>   Si alguien pone `RECAPTCHA_MODE=enforce`, Hugo empieza a comer 403: no puede
>   generar un token de reCAPTCHA v3.
>
> Hugo detecta los dos casos y devuelve el error explicado en `cloud_request.error`.

**Por qué CLIP y no solo pHash.** pHash compara píxeles: sirve cuando la
publicación reusa la foto oficial del proveedor, pero se cae con la foto propia
del cliente. Medido sobre la misma foto transformada (recorte + brillo, rotación
18°, espejo + desaturado):

| variante                      | CLIP  | pHash |
|-------------------------------|-------|-------|
| misma foto                    | 1.000 | 1.000 |
| recorte + brillo              | 0.923 | 0.500 |
| rotada 18°                    | 0.929 | 0.594 |
| espejo + fondo desaturado     | 0.982 | 0.500 |
| **otro producto sin relación**| 0.544 | 0.625 |

pHash puntúa **más bajo** al mismo producto que a uno sin relación. Por eso el
threshold por defecto (`EMBED_MATCH_THRESHOLD=0.88`) es sobre CLIP.

El modelo es la torre visual de CLIP ViT-B/32 en ONNX (~350 MB), bakeada en la
imagen Docker; corre en CPU sin torch (~24 ms/imagen). Los embeddings del
catálogo se precalculan en un índice en memoria y se persisten en
`image_embed_cache`, así un reinicio no vuelve a descargar ni inferir nada.

El Dockerfile baja una **revisión fija** del repo de Hugging Face
(`Qdrant/clip-ViT-B-32-vision @ e0c24ed0`) y verifica el **sha256** del archivo:
si no coincide, el build falla. Subir de versión implica cambiar los dos `ARG`
juntos, recalibrar los thresholds y vaciar `image_embed_cache`.

Medido en producción (ago 2026): el primer build del índice tardó **~19 min**
para 2052 imágenes (~1026 productos × 2), a ~1.8 img/s — el cuello es la
descarga, no la inferencia. Los rebuilds posteriores salen del cache. Mientras
construye, `/app/lookup` devuelve `status:"indexing"`; seguilo con
`GET /app/index-status`.

> **Memoria**: el modelo suma ~500 MB de RSS. El límite del container pasó de
> 512M a **2G** — en Coolify hay que subirlo en el panel del servicio.

### 5. Acción

- **Duplicados: solo flaguea.** Nada se deshabilita en Vendure sin que alguien
  apriete "Confirmar duplicado" en el dashboard (o `bulk-confirm`). Y confirmar
  solo apaga algo cuando el flag vino de la **auditoría de catálogo** (los dos
  productos están en Vendure: se apaga el más nuevo y se conserva el canónico).
  Un flag de `/verify` no tiene nada que apagar — el candidato de Luis/Cloud
  nunca entró a Vendure y el único id de la fila es el del producto **original**
  — así que confirmar solo registra y archiva. Cada fila dice explícitamente qué
  se apagaría (`disable_target_id`) y cuál es el original (`canonical_product_id`).
- Precios: snapshot + alerta. No modifica Vendure.
- Loguea TODO en `AuditLog` (Postgres en Supabase; SQLite en local).
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
│   │   ├── image_hash.py     # pHash (píxeles)
│   │   ├── image_embed.py    # CLIP ONNX (semántico)
│   │   ├── catalog_index.py  # índice vectorial del catálogo
│   │   ├── fuzzy_text.py
│   │   └── orchestrator.py
│   ├── ingest/
│   │   └── image_from_url.py # saca la foto de una URL de marketplace
│   ├── pricing/
│   │   ├── source_check.py   # precio del proveedor (OTAPI) + budget diario
│   │   ├── competitor_check.py  # SIN USO: no tiene llamador
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
   - `HUGO_ENV=production` (activa los chequeos estrictos de seguridad)
   - `DATABASE_URL` (Supabase Session Pooler, ver más abajo)
   - `VENDURE_API_URL`, `VENDURE_BEARER`, `VENDURE_CHANNEL_TOKEN`
   - `HUGO_API_KEYS` y `SUPABASE_ALLOWED_EMAILS` (ver *Seguridad*)
   - `RAPIDAPI_KEY`
   - `ALERT_SMTP_*` y `ALERT_EMAIL_TO`

   **Usuario de Vendure.** Lo único que Hugo escribe en Vendure es el flag
   `enabled` de un producto (`updateProduct`); el resto es lectura del catálogo.
   `VENDURE_BEARER` / `VENDURE_USER` deberían ser de un administrador con un
   **rol acotado al catálogo** (permisos `ReadCatalog` + `UpdateCatalog` del
   canal), no un SuperAdmin: si la credencial se filtra, el daño queda en
   productos y no llega a pedidos, clientes ni configuración. Esto es
   configuración en el admin de Vendure (Settings → Roles), no código de Hugo.
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
- `POST /app/lookup` — el b2box app manda una URL: PA + comprar ahora, o pedido a Cloud
- `GET  /app/index-status` — si el índice de imágenes ya está listo
- `POST /app/index-rebuild` — fuerza la reconstrucción del índice

Los tres `/app/*` se autentican con `X-API-Key` (igual que `/verify`). Hay una
key **por cliente** en `HUGO_API_KEYS="luis:xxx,cloud:yyy,b2box-app:zzz"`: Hugo
loguea el nombre del cliente en cada request autenticado y cada key se rota por
separado. `HUGO_API_KEY` (una sola key compartida) sigue valiendo como cliente
`legacy`; `GET /api/debug-config` lista los nombres configurados (nunca las keys).

### `POST /app/lookup`

```jsonc
// request — alcanza con `url`; `image_url` es para cuando el app ya subió la foto.
// `client` solo se usa si NO lo tenemos (es lo que pide el formulario de Cloud).
{
  "url": "https://articulo.mercadolibre.com.ar/MLA-123-lampara-led",
  "image_url": null,
  "note": "lo quiero en negro",
  "client": {
    "name": "Juan Pérez", "email": "juan@ejemplo.com", "phone": "+5491155551234",
    "country": "Argentina", "quantity": "200 u"
  }
}
```

```jsonc
// response — lo tenemos
{
  "status": "found", "found": true,
  "confidence": 0.94, "matched_by": ["image_embed"],
  "image_url": "https://http2.mlstatic.com/…jpg",
  "product": {
    "product_id": "42", "name": "Lámpara LED táctil",
    "product_code": "BX-1001",          // código del producto
    "pa": "PA-1001-BL",                 // PA = código de la 1ra variante
    "price_cents": 189900, "currency": "ARS",
    "variants": [{ "id": "101", "name": "Blanco", "pa": "PA-1001-BL", "price_cents": 189900 }],
    "buy_now_url": "https://b2box.app/ar/products/lampara-led-tactil"
  }
}
```

```jsonc
// response — no lo tenemos: se abrió la consulta en Cloud
{ "status": "not_found", "found": false,
  "cloud_request": { "sent": true, "request_id": "<uuid de form_app_consultations>" },
  "suggestion": { "product_id": "42", "score": 0.82 } }  // casi-match, si lo hubo

// response — no lo tenemos pero faltan datos del cliente: el app los pide y reintenta
{ "status": "not_found",
  "cloud_request": { "sent": false, "missing_fields": ["client.email", "client.phone"] } }
```

### Integración del app: mirá `action`, no `status`

La respuesta trae `action` y `message`. El app decide la pantalla con `action`
solo — no hace falta interpretar combinaciones de `status` + `cloud_request` +
`product`. `message` ya viene redactado para mostrárselo al cliente.

| `action` | Qué hace el app |
|---|---|
| `show_product` | Muestra el producto: PA, precio y botón "comprar ahora" |
| `confirm_product` | Muestra `suggestion` y pregunta "¿es este?". Si el cliente dice que no, repetir el lookup con `reject_suggestion: true` |
| `ask_photo` | Pide una foto del producto y reintenta el lookup con `image_url` |
| `ask_client_data` | Pide nombre, email y teléfono, y reintenta con `client` |
| `retry_later` | Avisa que reintente en unos minutos |
| `none` | Muestra `message` y listo |

**Por qué se pregunta en vez de afirmar.** Medido con productos reales del
catálogo: los aciertos caen entre 0.84 y 0.87 y el ruido en 0.80. Cuatro
centésimas de margen no alcanzan para decirle a un cliente "sí, lo tenemos" —
equivocarse ahí es peor que no encontrarlo. Por eso arriba de
`EMBED_MATCH_THRESHOLD` (0.88) se afirma, y entre `EMBED_SUGGEST_THRESHOLD`
(0.82) y ese valor se pregunta. El cliente resuelve en un toque lo que el modelo
no puede decidir solo.

Mientras hay una pregunta abierta **no se abre la consulta en Cloud**: si el
candidato resulta ser el correcto, ese pedido nacería muerto y alguien tendría
que descartarlo a mano. Cuando el cliente dice que no, el app repite el lookup
con `reject_suggestion: true` y ahí sí se abre — con el candidato descartado
anotado, para que nadie vuelva a proponer lo mismo.

**MercadoLibre necesita el paso de la foto.** ML no le contesta a un servidor y su
API no ofrece leer publicaciones de otros vendedores — no es falta de permisos ni
de certificación: ese scope no existe. Ver `ingest/meli.py`. Los links de ML
devuelven `action: "ask_photo"`, y con la foto del cliente el flujo sigue normal.
Las fichas de catálogo de ML (`/p/MLA…`) sí se leen por la API oficial.

El flujo completo del caso ML queda así:

```
cliente pega link de ML  → action: "ask_photo"
cliente sube una foto    → mismo lookup + image_url → action: "show_product"
```

Otros `status`: `"indexing"` (el índice se está construyendo — reintentar, **no**
se abre pedido), `"no_image"` (no se pudo sacar ninguna foto) y `"site_blocked"`
(el sitio bloquea a los servidores).

## Seguridad

- **`HUGO_ENV=production` es el interruptor.** Activa todo lo que sigue:
  secretos obligatorios al arranque (login del dashboard + al menos una API key
  que parsee), allowlist fail-closed, `503` en `/verify` y `/app/*` si ninguna
  key es válida, y descarte de keys placeholder o de menos de 24 caracteres.
  Con `development` (el default del código) todo eso es solo un warning. Por
  eso `docker-compose.yml` —lo que deploya Coolify— lo define con default
  `production`; en local el `.env` lo pisa con `development`.
- **Login del dashboard**: Supabase Auth de Cloud_B2BOX (mismos usuarios que
  Paco). `SUPABASE_ALLOWED_EMAILS` es la allowlist de quién entra y es
  **fail-closed en producción**: con `HUGO_ENV=production` y la variable vacía,
  todo login responde `403 "SUPABASE_ALLOWED_EMAILS no configurado"`. Hugo
  arranca igual (no es un restart loop) y `/verify` y `/app/*` siguen andando
  con su API key. Para abrir a todos los usuarios de Cloud_B2BOX hay que
  decirlo a propósito: `SUPABASE_ALLOWED_EMAILS=*`. En development, vacía =
  abierta, con warning.
- **Clientes máquina** (`/verify`, `/app/*`): una API key por cliente en
  `HUGO_API_KEYS` (ver arriba). Comparación en tiempo constante; el nombre del
  cliente queda en el log de cada request.
- **IP del cliente** (rate limit de `/verify`, lockout del login): se toma del
  último hop confiable de `X-Forwarded-For` según `TRUSTED_PROXY_HOPS`
  (1 = Traefik de Coolify, 0 = sin proxy). El primer valor del header lo
  escribe el cliente y no se le cree; si la cadena viene más corta que lo
  configurado se usa la IP del socket. `2` (Cloudflare delante de Traefik)
  **solo** si Traefik acepta tráfico únicamente desde los rangos de Cloudflare;
  si no, quien le pegue directo con un header armado elige su propia IP. No
  subirlo "por las dudas".

## Variables de entorno

Ver `.env.example`. Las críticas:

- `VENDURE_API_URL`, `VENDURE_BEARER`, `VENDURE_CHANNEL_TOKEN` — Vendure Admin API.
- `RAPIDAPI_KEY` — proxy a 1688 vía OTAPI (sin esto Hugo no puede consultar precios fuente).
- `ALERT_SMTP_*`, `ALERT_EMAIL_TO` — notificaciones por email.
- `ALERT_WEBHOOK_URL` — opcional, Slack/Discord/n8n/CallMeBot.
- `HUGO_ENV=production` — activa los chequeos estrictos (secretos obligatorios, allowlist fail-closed).
- `HUGO_API_KEYS` — una API key por cliente (`luis:…,cloud:…,b2box-app:…`).
- `SUPABASE_ALLOWED_EMAILS` — quién entra al dashboard; `*` para todos. Vacía en producción = nadie.
- `TRUSTED_PROXY_HOPS` — proxies confiables delante de Hugo (default 1).
- `DEDUP_*_THRESHOLD` — umbrales de confianza de cada estrategia (0-1).
- `PRICE_DRIFT_THRESHOLD` — % mínimo de variación que dispara alerta.
- `AUDIT_INTERVAL_HOURS` — cada cuánto corre la auditoría completa.
