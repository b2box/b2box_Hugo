"""Modelos SQLModel para Hugo. Tabla local con SQLite por defecto.

Dos tablas core:
  - PriceHistory: cada vez que vemos un precio (en Vendure, en fuente o en
    competidor), lo logueamos. Sirve para análisis y para detectar tendencias.
  - AuditLog: cada acción que toma Hugo (auto-update, disable duplicado,
    flag para revisión, etc.). Sirve para trazabilidad y reportes diarios.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy import BigInteger, Column, Index
from sqlmodel import Field, SQLModel

from app.clock import utcnow

PriceSource = Literal["vendure", "source", "competitor"]
ActionType = Literal[
    "price_updated",
    "duplicate_disabled",
    "duplicate_flagged",
    # Confirmado por el usuario sin tocar Vendure (el candidato nunca entró al catálogo)
    "duplicate_confirmed",
    "price_flagged",
    "no_change",
    "error",
    # /app/lookup (b2box app manda una URL)
    "app_lookup_match",
    "app_lookup_request_sent",
    "app_lookup_request_failed",
    "app_lookup_no_image",
]


class PriceHistory(SQLModel, table=True):
    __tablename__ = "price_history"
    # Índice compuesto: la query de snapshot-anterior y el budget-guard filtran
    # por (product_id, source, captured_at). Acelera ambos y baja costo Supabase.
    __table_args__ = (
        Index("ix_price_history_prod_source_time", "product_id", "source", "captured_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    product_id: str = Field(index=True)
    variant_id: str | None = Field(default=None, index=True)
    source: str = Field(index=True, description="vendure | source | competitor name")
    price_cents: int
    currency: str = Field(default="USD", max_length=8)
    captured_at: datetime = Field(default_factory=utcnow, index=True)
    extra: str | None = Field(default=None, description="JSON con info adicional")


class ImageHashCache(SQLModel, table=True):
    """pHash perceptual de una imagen, cacheado por URL.

    Antes el cache de hashes vivía SOLO en memoria (LRU) → al reiniciar el
    container se perdía y la próxima auditoría de duplicados re-descargaba TODAS
    las imágenes (lento + costo de red). Persistiéndolo, el hash sobrevive
    reinicios y se reusa entre corridas.
    """
    __tablename__ = "image_hash_cache"

    url: str = Field(primary_key=True, max_length=1024)
    phash: str = Field(description="pHash en hex (imagehash.ImageHash → str)")
    updated_at: datetime = Field(default_factory=utcnow)


class ImageEmbedCache(SQLModel, table=True):
    """Embedding CLIP de una imagen, cacheado por URL.

    Mismo rol que ImageHashCache pero para el vector semántico (512 floats).
    Embeder el catálogo entero es caro (descarga + inferencia en CPU): sin este
    cache, cada reinicio del container re-indexaría los ~1500 productos.

    El vector se guarda en base64 de float32 little-endian, ya L2-normalizado,
    así el score coseno es un producto punto directo.
    """
    __tablename__ = "image_embed_cache"

    url: str = Field(primary_key=True, max_length=1024)
    model: str = Field(default="clip-vit-b32", max_length=64)
    dim: int = Field(default=512)
    vector_b64: str = Field(description="float32 LE normalizado, en base64")
    updated_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    """Settings runtime editables desde el dashboard.

    Si una key no existe acá, se usa el default del .env (ver app/runtime.py).
    """
    __tablename__ = "settings"

    key: str = Field(primary_key=True)
    value: str  # se guarda siempre como string; el módulo runtime parsea según tipo
    updated_at: datetime = Field(default_factory=utcnow)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    # Índices compuestos para las queries del dashboard (que corren cada 15s):
    #  - listado/counts por sección: filtran dismissed + action|source y ordenan
    #    por created_at desc. Estos índices evitan full-scans en Supabase.
    __table_args__ = (
        Index("ix_audit_dismissed_action_time", "dismissed", "action", "created_at"),
        Index("ix_audit_dismissed_source_time", "dismissed", "source", "created_at"),
        Index("ix_audit_product_created", "product_id", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    action: str = Field(index=True, description=str(ActionType))
    # De dónde viene el evento — sirve para los tabs del dashboard
    # Valores: "luis" | "audit" | "orders" | "manual"
    source: str | None = Field(default=None, index=True)
    product_id: str = Field(index=True)
    related_product_id: str | None = Field(default=None)
    detail: str = Field(default="", description="Descripción humana de qué pasó")
    before: str | None = Field(default=None, description="JSON del estado previo")
    after: str | None = Field(default=None, description="JSON del estado nuevo")
    confidence: float | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    notified: bool = Field(default=False, description="Si ya se incluyó en algún email")

    # Datos denormalizados del producto (snapshot al momento del evento, para
    # que el dashboard pueda mostrar miniatura/nombre/código sin repreguntar a Vendure)
    product_name: str | None = Field(default=None)
    product_code: str | None = Field(default=None)        # b2boxProductCode (BX)
    product_image_url: str | None = Field(default=None)
    product_source_url: str | None = Field(default=None)  # link al proveedor
    related_product_name: str | None = Field(default=None)
    related_product_code: str | None = Field(default=None)
    # Acciones manuales del usuario sobre el evento
    dismissed: bool = Field(default=False, index=True, description="Descartado por el usuario")
    dismissed_at: datetime | None = Field(default=None)
    # Comentario libre del usuario (ej: "el link no anda", motivo real del flag).
    note: str | None = Field(default=None)

    # ── Semántica explícita de "Confirmar duplicado" ───────────────
    # Un `duplicate_flagged` nace en dos lugares con significados distintos:
    #   · /verify: el candidato (Luis/Cloud) NO está en Vendure. `product_id` es
    #     el producto que YA existe y matcheó. Confirmar NO apaga nada: solo se
    #     registra y se archiva.
    #   · auditoría de catálogo: los dos están en Vendure. `product_id` es el más
    #     nuevo (drop) y `related_product_id` el canónico (keep).
    # Antes el confirm hacía disable_product(product_id) sin distinguir, y en el
    # primer caso apagaba al ORIGINAL. Por eso ahora el id a apagar y el original
    # viajan explícitos en la fila:
    #   disable_target_id=None + canonical_product_id seteado → "no toca Vendure".
    #   Ambos None → fila anterior a estos campos (ver app/dedup/confirm_target.py).
    disable_target_id: str | None = Field(
        default=None,
        description="Producto que 'Confirmar duplicado' deshabilita en Vendure. None = ninguno",
    )
    canonical_product_id: str | None = Field(
        default=None,
        description="Producto original que se conserva. Nunca se deshabilita",
    )
    # JSON con el contexto del /verify original (source, callback_ctx, text_specs,
    # use_browser). retry-paco lo usa para reenviar al MISMO Paco (APP o PRO) con
    # el mismo callback; sin esto un reintento de un pedido PRO caía en Paco APP.
    verify_ctx: str | None = Field(default=None, description="JSON del contexto del /verify")


# ─── Semáforo de precios contra Mercado Libre ──────────────────────
# Tablas PROPIAS a propósito: `price_history` se poda a los 120 días
# (prune_price_history) y acá queremos la tendencia completa. El job de poda
# solo mira PriceHistory; estas no las toca.


class PriceMonitorRun(SQLModel, table=True):
    """Una corrida nocturna del monitor. Es también el cursor de reanudación:
    mientras `status == "running"` el job retoma esta misma corrida y procesa
    solo los productos que todavía no tienen snapshot con este `run_id`."""
    __tablename__ = "price_monitor_run"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = Field(default=None)
    # running | ok | degraded (>20 % failed) | failed (reventó antes de terminar)
    status: str = Field(default="running", index=True)
    # pm_mode al arrancar: 0 sombra, 1 activo (todavía no implementado).
    mode: int = Field(default=0)
    # cron | manual | resume
    trigger: str = Field(default="cron")
    total_products: int = Field(default=0)
    processed: int = Field(default=0)
    n_ok: int = Field(default=0)
    n_no_data: int = Field(default=0)
    n_failed: int = Field(default=0)
    n_skipped: int = Field(default=0)
    n_verde: int = Field(default=0)
    n_amarillo: int = Field(default=0)
    n_rojo: int = Field(default=0)
    n_sin_dato: int = Field(default=0)
    ml_requests_used: int = Field(default=0)
    # Juez LLM de la banda ambigua (apagado por default: pm_vision_max_calls=0).
    llm_calls: int = Field(default=0)
    llm_input_tokens: int = Field(default=0)
    llm_output_tokens: int = Field(default=0)
    llm_cost_usd: float = Field(default=0.0)
    # Cuántas veces un reinicio del proceso retomó esta corrida.
    resumed_count: int = Field(default=0)
    error: str | None = Field(default=None)
    # Fuente "ML web" (búsqueda en listado.mercadolibre.com.ar). `web_status`:
    # "ok", o por qué no corrió / se cortó ("apagada: falta BROWSER_PROXY…",
    # "cortada por esta noche: 5 fallos seguidos…"). Los bytes son lo que bajó
    # el navegador por el proxy (piso del consumo real: Request.sizes).
    web_status: str | None = Field(default=None)
    web_searches: int = Field(default=0)
    # 64 bits: una noche sin bloquear scripts son ~3,4 GB y un INTEGER de
    # Postgres aguanta 2,1 GB (el UPDATE reventaba con NumericValueOutOfRange).
    web_bytes: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, default=0))
    web_blocked: int = Field(default=0)
    # Productos cuyo precio salió de la web (no de una ficha de la API).
    n_web_ok: int = Field(default=0)
    # Productos con al menos una publicación SIMILAR guardada aparte.
    n_con_similares: int = Field(default=0)
    # Color ESTIMADO (por similares) de los productos sin IGUAL, aparte del real, y
    # productos donde ML solo devolvió DIFERENTES.
    n_est_verde: int = Field(default=0)
    n_est_amarillo: int = Field(default=0)
    n_est_rojo: int = Field(default=0)
    n_solo_diferentes: int = Field(default=0)


class MarketPriceSnapshot(SQLModel, table=True):
    """Lo que vimos en ML para UN producto en UNA corrida. Una fila por
    producto habilitado y corrida, siempre — aunque no haya dato (`ml_status`
    dice por qué). Es el historial para ver tendencias."""
    __tablename__ = "market_price_snapshot"
    # Tres índices compuestos cubren todas las consultas (historial por
    # producto, tabla por corrida y color, cursor de reanudación); índices
    # sueltos por columna solo encarecerían los ~1.500 inserts por noche.
    __table_args__ = (
        Index("ix_mps_product_time", "product_id", "captured_at"),
        Index("ix_mps_run_color", "run_id", "color"),
        # El cursor de reanudación se apoya en esto: un producto no puede tener
        # dos snapshots en la misma corrida.
        Index("ix_mps_run_product", "run_id", "product_id", unique=True),
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: int
    product_id: str = Field(max_length=64)
    variant_id: str | None = Field(default=None, max_length=64)
    captured_at: datetime = Field(default_factory=utcnow)
    # ok       → hubo matches y precio
    # no_data  → ML no tiene (o no reconocimos) el producto: NO es malo
    # failed   → ML falló (429, 5xx, red): el dato viejo sigue valiendo
    # skipped  → no se pudo evaluar (sin precio propio, sin foto, sin budget)
    ml_status: str = Field(default="no_data", max_length=16)
    ml_error: str | None = Field(default=None)
    ml_median_cents: int | None = Field(default=None)
    ml_min_cents: int | None = Field(default=None)
    ml_listing_count: int = Field(default=0)
    ml_seller_count: int = Field(default=0)
    ml_currency: str | None = Field(default=None, max_length=8)
    # JSON: [{ml_id, permalink, title, listings, min_cents, median_cents, source}]
    matched_listings: str | None = Field(default=None)
    # Cómo se decidió que es el mismo producto: clip | clip+nombre | llm
    match_source: str | None = Field(default=None)
    # Confianza del juez LLM cuando el match vino por ahí (0-1).
    match_confidence: float | None = Field(default=None)
    image_score_max: float | None = Field(default=None)
    name_score_max: float | None = Field(default=None)
    candidates_count: int = Field(default=0)
    # Cuántos candidatos cayeron en la banda ambigua (los que iría a mirar el juez).
    ambiguous_count: int = Field(default=0)
    our_price_cents: int | None = Field(default=None)
    # "tier:min_qty=12" | "priceWithTax" | "priceWithTax(fallback)"
    tier_used: str | None = Field(default=None)
    commission_pct: float | None = Field(default=None)
    shipping_cents: int | None = Field(default=None)
    est_margin_pct: float | None = Field(default=None)
    # verde | amarillo | rojo | sin_dato
    color: str = Field(default="sin_dato", max_length=16)
    prev_color: str | None = Field(default=None)
    # Denormalizado para que el dashboard no repregunte a Vendure.
    product_name: str | None = Field(default=None)
    product_code: str | None = Field(default=None)
    product_image_url: str | None = Field(default=None)
    product_slug: str | None = Field(default=None)
    # ¿El producto estaba habilitado en Vendure al medirlo? Los deshabilitados se
    # miden solo para mostrar (pm_include_disabled) y nunca llevan a escribir.
    product_enabled: bool = Field(default=True)
    # De dónde salió el precio del snapshot: api (ficha de catálogo) | web.
    match_origin: str | None = Field(default=None, max_length=8)
    # Publicaciones SIMILARES (no cuentan para mediana/mínimo/ganancia/color):
    # misma estructura que matched_listings + `differences`.
    similar_count: int = Field(default=0)
    similar_listings: str | None = Field(default=None)
    # Búsquedas web de este producto y bytes que bajaron por el proxy.
    web_searches: int = Field(default=0)
    web_bytes: int = Field(default=0)
    # Cómo le fue a la fuente web con este producto: ok | empty | blocked |
    # error | budget | off. None = no se usó.
    web_state: str | None = Field(default=None, max_length=8)
    # JSON: medidas nuestras de Vendure (length/width/height/weight y box*).
    our_specs: str | None = Field(default=None)
    # Publicaciones DIFERENTES (otro producto): se guardan igual, solo para
    # mostrar (nunca entran a ningún cálculo). Misma estructura que similar_listings.
    other_listings: str | None = Field(default=None)
    other_count: int = Field(default=0)
    # Publicaciones IGUAL a las que no se les pudo sacar un precio que cuente
    # (sin vendedores, pocas ventas, tope de fichas): se muestran con los idénticos
    # pero no suman a la mediana. Aparte de `matched_listings`, que son solo las
    # IGUAL con precio (el color real sale de ahí).
    unpriced_listings: str | None = Field(default=None)
    # Qué devolvió ML para este producto, en una palabra: igual | igual_sin_precio |
    # similar (solo similares) | diferente (solo diferentes) | ninguno (ML no
    # devolvió nada). None = falló o no se evaluó (o fila vieja).
    match_state: str | None = Field(default=None, max_length=16)
    # Color ESTIMADO por la mediana de los SIMILARES (misma fórmula de ganancia).
    # Solo cuando no hay IGUAL; `color` (el real) NO cambia nunca por esto.
    estimated_color: str | None = Field(default=None, max_length=16)
    estimated_margin_pct: float | None = Field(default=None)
    estimated_median_cents: int | None = Field(default=None)
    estimated_listing_count: int = Field(default=0)
    estimated_from: str | None = Field(default=None, max_length=8)   # 'similar'


class MarketMatchFeedback(SQLModel, table=True):
    """"No es el mismo": una persona dijo que esta publicación de ML NO es el
    producto. Excluye ese id de ML para ese producto en las próximas corridas y
    queda como etiqueta negativa para calibrar el filtro."""
    __tablename__ = "market_match_feedback"
    __table_args__ = (
        Index("ix_mmf_product_ml", "product_id", "ml_id", unique=True),
    )

    id: int | None = Field(default=None, primary_key=True)
    product_id: str = Field(max_length=64)
    ml_id: str = Field(max_length=64)
    created_at: datetime = Field(default_factory=utcnow)
    # Lo que Hugo había dicho de esa publicación y por qué (para calibrar).
    category: str | None = Field(default=None, max_length=12)     # igual | similar
    origin: str | None = Field(default=None, max_length=8)        # api | web
    source: str | None = Field(default=None, max_length=16)       # clip | clip+nombre | llm | specs
    image_score: float | None = Field(default=None)
    name_score: float | None = Field(default=None)
    confidence: float | None = Field(default=None)
    title: str | None = Field(default=None)
    permalink: str | None = Field(default=None)
    snapshot_id: int | None = Field(default=None)
    product_name: str | None = Field(default=None)
    # Quién lo marcó (el usuario de la sesión del dashboard, un email con Supabase).
    actor: str | None = Field(default=None, max_length=120)
    # 0 = "No es el mismo" (se excluye); 1 = "Es el mismo" (se promueve a IGUAL
    # para ese producto en las próximas corridas). Una fila por (producto, id).
    label: int = Field(default=0)
    # La marca anterior cuando una persona cambió de opinión (0 → 1 o 1 → 0): "Deshacer"
    # vuelve a ella en vez de borrar la fila. None = nunca cambió.
    previous_label: int | None = Field(default=None)


class MlSellerCache(SQLModel, table=True):
    """Ventas concretadas de un vendedor de ML (`/users/{id}`), cacheadas 30
    días. Los mismos vendedores aparecen en cientos de fichas: sin cache serían
    miles de requests por noche para el mismo dato."""
    __tablename__ = "ml_seller_cache"

    seller_id: str = Field(primary_key=True, max_length=32)
    completed_sales: int | None = Field(default=None)
    fetched_at: datetime = Field(default_factory=utcnow)
