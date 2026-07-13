# ────────────────────────────────────────────────────────────────
# Hugo — B2Box Catalog QC Agent
#
# Imagen ÚNICA que empaqueta frontend (React/Vite) + backend (FastAPI).
# El contexto de build es la RAÍZ del repo (necesitamos frontend/ y backend/).
#
# Stages:
#   1. frontend-builder → compila el React a estáticos (dist/)
#   2. builder          → compila las wheels de Python (Pillow, lxml, …)
#   3. runtime          → imagen final: Python + deps + código + dist/ de React
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

# ─── Stage 2: builder (Python wheels) ───────────────────────────
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependencias de sistema necesarias para compilar Pillow + lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        zlib1g-dev \
        libxml2-dev \
        libxslt1-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY backend/pyproject.toml ./
COPY backend/app ./app

# Instalamos en un prefix aislado que después copiamos al stage final
RUN pip install --prefix=/install .

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

# Código del agente (backend)
WORKDIR /app
COPY --chown=hugo:hugo backend/app ./app

# Build de React → app/static/ (FastAPI lo sirve: '/' y '/static/*')
COPY --from=frontend-builder --chown=hugo:hugo /fe/dist ./app/static

# DATABASE_URL viene del .env (Postgres/Supabase en prod, SQLite en local).
# Si no está definida, en config.py default es sqlite:///./hugo.db.

USER hugo

EXPOSE 8000

# Healthcheck: el endpoint /health responde {"status":"ok"}
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fs http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
