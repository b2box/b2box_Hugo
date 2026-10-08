# ────────────────────────────────────────────────────────────────
# Hugo — B2Box Catalog QC Agent
#
# Imagen ÚNICA que empaqueta frontend (React/Vite) + backend (FastAPI).
# El contexto de build es la RAÍZ del repo (necesitamos frontend/ y backend/).
#
# Stages:
#   1. frontend-builder → compila el React a estáticos (dist/)
#   2. builder          → instala las deps de Python EXACTAMENTE como dice
#                         backend/uv.lock (Pillow, lxml, …)
#   3. runtime          → imagen final: Python + deps + código + dist/ de React
#
# Las deps de Python salen de backend/uv.lock, nunca de un `pip install .` suelto:
# el 08-oct-2026 un build sin lock trajo sqlmodel 0.0.48 y gql 4.4 + httpx2 y
# rompió producción dos veces. Para cambiar una versión: ver README, sección
# "Actualizar dependencias".
# ────────────────────────────────────────────────────────────────

# ─── Stage 1: frontend-builder ──────────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /fe

# Instalamos deps primero (cache de capa mientras no cambie package.json)
COPY frontend/package*.json ./
RUN npm install

# Código del front y build. Vite emite a /fe/dist con base '/static/'.
COPY frontend/ ./
RUN npm run build

# ─── Stage 2a: uv (solo el binario, para exportar el lock) ──────
# Versión Y digest fijos: el tag solo puede reapuntarse. Para subirla: cambiar
# los dos juntos (`docker buildx imagetools inspect ghcr.io/astral-sh/uv:X.Y.Z`
# da el digest). uv NO queda en la imagen final.
FROM ghcr.io/astral-sh/uv:0.11.8@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 AS uv

# ─── Stage 2: builder (Python wheels) ───────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_PROGRESS=1

# Dependencias de sistema necesarias para compilar Pillow + lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        zlib1g-dev \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /build

# Solo pyproject + lock: la capa de deps queda cacheada mientras no cambien
# (un cambio de código ya no reinstala nada).
COPY backend/pyproject.toml backend/uv.lock ./

# `browser` = Camoufox, para renderizar las fichas que bloquean el fetch plano
# (ver app/ingest/browser_fetch.py). Off por defecto: suma Playwright acá y
# ~150 MB de Firefox en el runtime. Build con:
#     docker build --build-arg INSTALL_BROWSER=true ...
ARG INSTALL_BROWSER=false

# 1) uv.lock → requirements.lock (versiones + hashes, sin resolver nada).
#    --locked hace FALLAR el build si uv.lock no coincide con pyproject.toml
#    (alguien tocó las deps y no regeneró el lock): mejor sin imagen que con
#    versiones que nadie probó. Se arregla con `uv lock` (ver README).
RUN set -eu; \
    extra=""; \
    if [ "$INSTALL_BROWSER" = "true" ]; then extra="--extra browser"; fi; \
    uv export --locked --no-emit-project --format requirements-txt $extra \
        -o requirements.lock

# 2) Deps exactas, en un prefix aislado que después copiamos al stage final.
#    --no-deps: el lock ya trae todo el árbol, pip no resuelve nada.
#    --require-hashes: cada archivo tiene que coincidir con el sha256 del lock.
RUN pip install --prefix=/install --no-deps --require-hashes -r requirements.lock

# 3) El proyecto en sí (sin deps: ya están). Va después para que un cambio de
#    código no invalide la capa de arriba.
COPY backend/app ./app
RUN pip install --prefix=/install --no-deps .

# Si algo quedó incoherente (una dep que pide otra versión de la que fija el
# lock), que reviente acá y no en runtime.
RUN PYTHONPATH=/install/lib/python3.11/site-packages pip check

# ─── Stage 2b: modelo CLIP (torre visual, ONNX) ─────────────────
# Lo bajamos en build y lo bakeamos en la imagen: sin volumen que montar y sin
# descargar 350 MB en cada arranque del container. Stage aparte para que la capa
# quede cacheada y no se rebaje en cada cambio de código.
FROM debian:bookworm-slim AS model

# Revisión (commit del repo de Hugging Face) y sha256 del archivo, FIJADOS.
# `resolve/main` puede cambiar debajo nuestro: un rebuild traería otro modelo
# con otros embeddings, el índice cacheado en image_embed_cache quedaría
# mezclado y los thresholds calibrados (config.embed_*_threshold) dejarían de
# valer. Para subir de versión: cambiar REVISION y SHA256 juntos, recalibrar
# con `python -m app.dedup.calibrate_embed` y vaciar image_embed_cache.
ARG CLIP_ONNX_REPO=https://huggingface.co/Qdrant/clip-ViT-B-32-vision
ARG CLIP_ONNX_REVISION=e0c24ed0fa57fa3e4f97f30de74c51d944036ace
ARG CLIP_ONNX_SHA256=c68d3d9a200ddd2a8c8a5510b576d4c94d1ae383bf8b36dd8c084f94e1fb4d63
ARG CLIP_ONNX_URL=${CLIP_ONNX_REPO}/resolve/${CLIP_ONNX_REVISION}/model.onnx

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# El build FALLA si el hash no coincide: mejor no tener imagen que tener una
# con un modelo que no es el que se calibró.
RUN mkdir -p /opt/models \
    && curl -fsSL --retry 3 -o /opt/models/clip-vit-b32-vision.onnx "${CLIP_ONNX_URL}" \
    && echo "${CLIP_ONNX_SHA256}  /opt/models/clip-vit-b32-vision.onnx" | sha256sum -c - \
    && test -s /opt/models/clip-vit-b32-vision.onnx

# ─── Stage 3: runtime ───────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Runtime libs (sin headers/compiladores)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        libxml2 \
        libxslt1.1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Usuario no-root
RUN useradd --create-home --shell /bin/bash hugo

# Copiamos las deps instaladas por el builder
COPY --from=builder /install /usr/local

# ─── Camoufox (opcional, ver INSTALL_BROWSER en el stage builder) ──
# Firefox necesita libs gráficas aunque corra headless, y el binario se baja
# como el usuario que después lo ejecuta (Camoufox lo cachea en su $HOME).
ARG INSTALL_BROWSER=false
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 libasound2 libxtst6 \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Modelo CLIP (ver EMBED_MODEL_PATH en config.py). Si este COPY se saca, Hugo
# sigue arrancando: /app/lookup cae a pHash y loguea un warning.
COPY --from=model /opt/models /opt/models

# Código del agente (backend)
WORKDIR /app
COPY --chown=hugo:hugo backend/app ./app

# Build de React → app/static/ (FastAPI lo sirve: '/' y '/static/*')
COPY --from=frontend-builder --chown=hugo:hugo /fe/dist ./app/static

# DATABASE_URL viene del .env (Postgres/Supabase en prod, SQLite en local).
# Si no está definida, en config.py default es sqlite:///./hugo.db.

USER hugo

# El binario de Firefox se baja como `hugo`: Camoufox lo busca en el cache del
# usuario que corre el proceso, así que hacerlo como root no serviría.
# La descarga pasa por la API de GitHub, que limita por IP: el deploy del
# 07-oct-2026 murió acá con exit 1. Se reintenta y, si igual falla, el build
# SIGUE: app/main.py completa la descarga al arrancar (fail-soft). Con
# GITHUB_TOKEN en el entorno de build, camoufox autentica y el límite sube.
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
        ok=0; \
        for i in 1 2 3; do \
            if python -m camoufox fetch; then ok=1; break; fi; \
            echo "camoufox fetch: intento $i falló, reintento en 20 s"; sleep 20; \
        done; \
        if [ "$ok" = "1" ]; then echo "camoufox fetch OK"; \
        else echo "WARN: camoufox fetch falló 3 veces; Firefox se baja al arrancar"; fi; \
    fi

EXPOSE 8000

# Healthcheck: el endpoint /health responde {"status":"ok"}
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fs http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
