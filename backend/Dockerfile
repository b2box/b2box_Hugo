# ────────────────────────────────────────────────────────────────
# Hugo — B2Box Catalog QC Agent
# Multi-stage build: el stage `builder` compila las wheels (Pillow,
# lxml, etc.) y el stage final solo lleva las wheels instaladas.
# Imagen final ~150-200 MB en lugar de 600+.
# ────────────────────────────────────────────────────────────────

# ─── Stage 1: builder ───────────────────────────────────────────
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
COPY pyproject.toml ./
COPY app ./app

# Instalamos en un prefix aislado que después copiamos al stage final
RUN pip install --prefix=/install .

# ─── Stage 2: runtime ───────────────────────────────────────────
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

# Código del agente (incluye app/static/ con el dashboard)
WORKDIR /app
COPY --chown=hugo:hugo app ./app

# DATABASE_URL viene del .env (Postgres/Supabase en prod, SQLite en local).
# Si no está definida, en config.py default es sqlite:///./hugo.db.

USER hugo

EXPOSE 8000

# Healthcheck: el endpoint /health responde {"status":"ok"}
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fs http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
