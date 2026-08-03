"""POST /app/lookup — el b2box app manda una URL y pregunta "¿tenemos esto?".

Flujo:

    URL del cliente
        │  (MercadoLibre / Alibaba / 1688 / AliExpress / foto propia)
        ▼
    ingest.image_from_url.extract()   → saca la(s) foto(s) del producto
        ▼
    match contra el catálogo Vendure
        ├── source URL exacta          (barato, seguro)
        ├── embeddings CLIP            (misma cosa aunque sea otra foto)
        └── pHash                      (fallback si CLIP no está disponible)
        ▼
    ┌────────────────────────┬──────────────────────────────────────┐
    │ match ≥ threshold      │ sin match                            │
    │ → PA (SKU de variante) │ → formulario a Cloud_B2BOX para que  │
    │ + link "comprar ahora" │   el equipo decida si lo buscamos    │
    └────────────────────────┴──────────────────────────────────────┘

Por qué el endpoint es nuevo y no un `source` más de /verify: /verify entra con
nombre+descripción+imágenes y su salida manda a Paco. Acá entra una URL sola,
no hay texto para el fuzzy match, y la salida es precio o formulario. Mezclarlos
rompería el contrato que ya consumen Luis y el admin.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app import runtime
from app.config import get_settings
from app.db.models import AuditLog
from app.db.session import engine
from app.dedup import catalog_index, image_embed
from app.dedup.image_hash import image_similarity
from app.dedup.url_match import url_similarity
from app.ingest import image_from_url
from app.integrations import cloud as cloud_integration
from app.security import verify_api_key, verify_rate_limit
from app.vendure import catalog as vendure_catalog
from app.vendure.client import VendureClient, VendureProduct

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["b2box-app"])

# Cuántas comparaciones de pHash corren a la vez en el fallback. El cuello de
# botella es la descarga de imágenes, no la CPU.
_PHASH_CONCURRENCY = 8

# Cuántas fotos de la publicación se comparan contra el catálogo. Las páginas de
# producto traen varias (galería) y la principal no siempre es la que mejor
# matchea; nos quedamos con el mejor score entre todas.
_MAX_QUERY_IMAGES = 4


# ─── Modelos ───────────────────────────────────────────────────────


class AppLookupClient(BaseModel):
    """Datos del cliente. El formulario de Cloud exige nombre, email y teléfono.

    Si el producto SÍ está en el catálogo no se usan para nada; solo hacen falta
    para abrir la consulta cuando no lo tenemos.
    """
    name: str = ""
    email: str = ""
    phone: str = ""
    country: str | None = None
    # Cuánto quiere comprar. Texto libre en el form ("200 u", "1 contenedor").
    quantity: str = ""


class AppLookupRequest(BaseModel):
    # La URL que pegó el cliente. Puede ser una publicación o un link directo
    # a una foto suya.
    url: str = ""
    # Si el app ya subió la foto a su propio storage, puede mandarla derecho
    # y saltear el scraping.
    image_url: str | None = None
    # Comentario libre del cliente ("busco 200 unidades", "en color negro"…).
    # Va como `description` del producto en el formulario.
    note: str | None = None
    client: AppLookupClient = Field(default_factory=AppLookupClient)
    source: str = "b2box-app"
    # El cliente ya vio la sugerencia y dijo que NO era ese producto. Hugo deja
    # de sugerir y abre la consulta en Cloud. Sin esto, un "no" del cliente
    # volvería a mostrarle el mismo candidato para siempre.
    reject_suggestion: bool = False


class LookupVariant(BaseModel):
    id: str
    name: str = ""
    # "PA" = el código de la variante (SKU en Vendure).
    pa: str = ""
    price_cents: int | None = None
    currency: str | None = None
    stock: str | None = None


class LookupProduct(BaseModel):
    product_id: str
    name: str = ""
    slug: str = ""
    description: str = ""
    # Código BX del producto (b2boxProductCode). Distinto del PA de la variante.
    product_code: str | None = None
    # PA de la primera variante — el que el app muestra si no elige variante.
    pa: str | None = None
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    price_cents: int | None = None
    currency: str | None = None
    variants: list[LookupVariant] = Field(default_factory=list)
    # Link del botón "comprar ahora". None si STOREFRONT_URL no está configurado.
    buy_now_url: str | None = None


class LookupSuggestion(BaseModel):
    """Casi-match: no alcanza para afirmar, sí para preguntar "¿es este?".

    Medido con productos reales: los aciertos caen entre 0.84 y 0.87 y el ruido
    en 0.80. Cuatro centésimas de margen es poco para afirmarle a un cliente que
    lo tenemos, y equivocarse ahí es peor que no encontrarlo. Así que en esa
    franja preguntamos en vez de afirmar: el cliente resuelve en un toque lo que
    la máquina no puede decidir sola.
    """
    product_id: str
    name: str = ""
    product_code: str | None = None
    image_url: str | None = None
    score: float = 0.0
    # Data completa, para que el app pueda mostrar PA y precio apenas el cliente
    # confirme, sin una segunda vuelta al servidor.
    product: LookupProduct | None = None


class CloudRequestInfo(BaseModel):
    sent: bool = False
    # id de la fila en form_app_consultations.
    request_id: str | None = None
    error: str | None = None
    # Campos obligatorios del formulario que el app no mandó. Si viene con algo,
    # el app tiene que pedirle esos datos al cliente y reintentar el lookup.
    missing_fields: list[str] = Field(default_factory=list)


class AppLookupResponse(BaseModel):
    # found        → está en el catálogo, viene `product` con PA y comprar ahora
    # not_found    → no está; se abrió el pedido en Cloud (ver `cloud_request`)
    # indexing     → el índice de imágenes todavía se está construyendo, reintentar
    # no_image     → no se pudo sacar ninguna foto de la URL
    # site_blocked → el sitio no le contesta a un servidor (anti-bot). El app
    #                tiene que mandar la foto en `image_url`: es el único caso
    #                donde reintentar con la misma URL nunca va a funcionar.
    status: str
    found: bool = False
    confidence: float = 0.0
    matched_by: list[str] = Field(default_factory=list)
    # Qué extrajo Hugo de la URL (útil para que el app muestre la foto detectada).
    image_url: str | None = None
    image_urls: list[str] = Field(default_factory=list)
    title: str = ""
    marketplace: str = "other"
    canonical_url: str = ""
    product: LookupProduct | None = None
    suggestion: LookupSuggestion | None = None
    cloud_request: CloudRequestInfo | None = None
    detail: str | None = None

    # Qué tiene que hacer el app ahora. Es lo único que necesita mirar para
    # decidir la pantalla siguiente; `status` queda para diagnóstico.
    #   show_product     → mostrar el producto con el PA y el botón de comprar
    #   confirm_product  → mostrar la sugerencia y preguntar "¿es este?"
    #   ask_photo        → pedirle una foto al cliente y reintentar con image_url
    #   ask_client_data  → pedir nombre/email/teléfono y reintentar
    #   retry_later      → volver a probar en unos minutos
    #   none             → no hay nada más que hacer, mostrar el mensaje y listo
    action: str = "none"
    # Texto listo para mostrarle al cliente, sin jerga. El app puede usarlo tal
    # cual o escribir el suyo.
    message: str = ""


def _decide_action(resp: AppLookupResponse) -> AppLookupResponse:
    """Traduce el estado interno a "qué hace el app ahora" + texto para el cliente.

    Vive acá y no en el app para que el mensaje sea uno solo: si mañana ML
    desbloquea la lectura, o sumamos otro marketplace, el app no cambia.
    """
    if resp.status == "found" and resp.product is not None:
        resp.action = "show_product"
        resp.message = f"Lo tenemos: {resp.product.name}"
        return resp

    if resp.status == "site_blocked":
        resp.action = "ask_photo"
        resp.message = (
            f"{resp.marketplace.replace('-', ' ').title()} no nos deja leer el link "
            "desde acá. Mandanos una foto del producto y lo buscamos igual."
        )
        return resp

    if resp.status == "no_image":
        resp.action = "ask_photo"
        resp.message = (
            "No pudimos sacar la foto de ese link. Mandanos una foto del producto "
            "y lo buscamos."
        )
        return resp

    if resp.status == "indexing":
        resp.action = "retry_later"
        resp.message = "Estamos actualizando el catálogo. Probá de nuevo en un rato."
        return resp

    # not_found con casi-match: preguntar antes de dar por perdido. Va primero
    # que pedir los datos del cliente — si el producto es ese, no hace falta
    # pedirle nada.
    if resp.suggestion is not None:
        resp.action = "confirm_product"
        resp.message = f"¿Es este? {resp.suggestion.name}"
        return resp

    # not_found
    if resp.cloud_request is not None and resp.cloud_request.missing_fields:
        resp.action = "ask_client_data"
        resp.message = (
            "Todavía no lo tenemos. Dejanos tu nombre, email y teléfono y lo "
            "buscamos para vos."
        )
        return resp

    if resp.cloud_request is not None and resp.cloud_request.sent:
        resp.action = "none"
        resp.message = (
            "Todavía no lo tenemos. Registramos tu pedido y te contactamos con "
            "una cotización."
        )
        return resp

    resp.action = "none"
    resp.message = (
        "Todavía no lo tenemos y no pudimos registrar tu pedido. Probá de nuevo "
        "en unos minutos."
    )
    return resp


# ─── Helpers ───────────────────────────────────────────────────────


def _buy_now_url(slug: str, product_id: str, product_code: str | None) -> str | None:
    s = get_settings()
    base = (s.storefront_url or "").rstrip("/")
    if not base:
        return None
    try:
        path = s.storefront_product_path.format(
            slug=slug or "", id=product_id or "", code=product_code or ""
        )
    except (KeyError, IndexError):
        log.warning("STOREFRONT_PRODUCT_PATH tiene un placeholder desconocido: %s",
                    s.storefront_product_path)
        path = f"/product/{slug}"
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def _product_from_full(full: dict[str, Any]) -> LookupProduct:
    """Arma la respuesta a partir del dict de VendureClient.get_product_full()."""
    variants = [
        LookupVariant(
            id=str(v.get("id", "")),
            name=v.get("name", "") or "",
            pa=v.get("sku", "") or "",
            price_cents=v.get("price_cents"),
            currency=v.get("currency"),
            stock=str(v["stock"]) if v.get("stock") is not None else None,
        )
        for v in (full.get("variants") or [])
    ]
    first = variants[0] if variants else None
    return LookupProduct(
        product_id=str(full.get("id", "")),
        name=full.get("name", "") or "",
        slug=full.get("slug", "") or "",
        description=full.get("description", "") or "",
        product_code=full.get("product_code"),
        pa=first.pa if first else None,
        image_url=full.get("featured_image_url"),
        image_urls=list(full.get("image_urls") or []),
        price_cents=first.price_cents if first else full.get("first_variant_price_cents"),
        currency=first.currency if first else None,
        variants=variants,
        buy_now_url=_buy_now_url(
            full.get("slug", "") or "",
            str(full.get("id", "")),
            full.get("product_code"),
        ),
    )


def _product_from_cached(p: VendureProduct) -> LookupProduct:
    """Fallback cuando get_product_full falla: usamos lo que hay en el cache.

    Sin variantes no hay PA, pero el app igual puede mostrar el producto y el
    link de compra en vez de decirle al cliente que no lo tenemos.
    """
    return LookupProduct(
        product_id=p.id,
        name=p.name,
        slug=p.slug,
        description=p.description,
        product_code=p.product_code,
        pa=None,
        image_url=p.featured_image_url,
        image_urls=list(p.image_urls or []),
        price_cents=p.first_variant_price_cents,
        buy_now_url=_buy_now_url(p.slug, p.id, p.product_code),
    )


def _record(
    *,
    action: str,
    detail: str,
    source: str,
    product_id: str,
    confidence: float | None = None,
    product_name: str | None = None,
    image_url: str | None = None,
    source_url: str | None = None,
) -> None:
    """Fila de AuditLog para que el dashboard muestre qué pidió el app."""
    try:
        with Session(engine) as session:
            session.add(AuditLog(
                action=action,
                source=source or "b2box-app",
                product_id=product_id,
                detail=detail[:500],
                confidence=confidence,
                product_name=(product_name or "")[:200] or None,
                product_image_url=image_url,
                product_source_url=source_url,
            ))
            session.commit()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo registrar el AuditLog de /app/lookup")


def _url_match(source_url: str, products: list[VendureProduct]) -> VendureProduct | None:
    """Match exacto por URL de proveedor. Es el más confiable y no cuesta red."""
    if not source_url:
        return None
    for p in products:
        if p.enabled and url_similarity(source_url, p.source_url) >= 1.0:
            return p
    return None


async def _phash_scan(
    image_urls: list[str], products: list[VendureProduct], threshold: float
) -> tuple[VendureProduct | None, float]:
    """Fallback sin CLIP: pHash de la foto contra el catálogo.

    Solo pesca fotos prácticamente idénticas (típico de una publicación que reusa
    la imagen del proveedor). La primera corrida descarga muchas imágenes; a
    partir de ahí el cache (memoria + tabla image_hash_cache) las reusa.
    """
    sem = asyncio.Semaphore(_PHASH_CONCURRENCY)
    best: tuple[VendureProduct | None, float] = (None, 0.0)
    found = asyncio.Event()

    async def _one(p: VendureProduct) -> None:
        nonlocal best
        if found.is_set():
            return
        async with sem:
            if found.is_set():
                return
            score = await image_similarity(image_urls, (p.image_urls or [])[:1])
        if score > best[1]:
            best = (p, score)
        if score >= threshold:
            found.set()

    await asyncio.gather(*(_one(p) for p in products if p.enabled))
    return best


# ─── Endpoint ──────────────────────────────────────────────────────


@router.post(
    "/lookup",
    response_model=AppLookupResponse,
    dependencies=[Depends(verify_api_key), Depends(verify_rate_limit)],
)
async def app_lookup(payload: AppLookupRequest) -> AppLookupResponse:
    """El b2box app manda una URL; Hugo responde PA + comprar ahora, o abre pedido.

    El `action` de la respuesta ya dice qué pantalla mostrar: el app no tiene que
    interpretar combinaciones de status + cloud_request + product.
    """
    return _decide_action(await _lookup(payload))


async def _lookup(payload: AppLookupRequest) -> AppLookupResponse:
    raw_url = (payload.url or "").strip()
    direct_image = (payload.image_url or "").strip()
    if not raw_url and not direct_image:
        raise HTTPException(422, "Mandá `url` (publicación o foto) o `image_url`.")

    # ── 1. Sacar la foto ───────────────────────────────────────────
    title = ""
    marketplace = "other"
    canonical = raw_url or direct_image
    image_urls: list[str] = []

    if direct_image:
        image_urls = [direct_image]
        marketplace = "upload"
    if raw_url:
        try:
            extracted = await image_from_url.extract(raw_url)
            image_urls = image_urls + [u for u in extracted.image_urls if u not in image_urls]
            title = extracted.title
            marketplace = extracted.marketplace if not direct_image else marketplace
            canonical = extracted.canonical_url
        except image_from_url.ExtractError as exc:
            if not image_urls:
                blocked = isinstance(exc, image_from_url.BlockedByCaptcha)
                _record(
                    action="app_lookup_no_image",
                    detail=f"No se pudo sacar la foto de {raw_url[:200]} — {exc}",
                    source=payload.source,
                    product_id="(sin foto)",
                    source_url=raw_url,
                )
                return AppLookupResponse(
                    status="site_blocked" if blocked else "no_image",
                    marketplace=image_from_url.detect_marketplace(raw_url),
                    canonical_url=raw_url,
                    detail=str(exc),
                )
            log.info("extract falló para %s, sigo con la imagen directa: %s", raw_url, exc)

    if not image_urls:
        return AppLookupResponse(status="no_image", canonical_url=canonical,
                                 detail="No hay ninguna imagen para comparar.")

    base = AppLookupResponse(
        status="not_found",
        image_url=image_urls[0],
        image_urls=image_urls,
        title=title,
        marketplace=marketplace,
        canonical_url=canonical,
    )

    # ── 2. Match contra el catálogo ────────────────────────────────
    try:
        products = await vendure_catalog.get_catalog()
    except Exception as exc:  # noqa: BLE001
        log.warning("app_lookup: no se pudo traer el catálogo: %s", exc)
        raise HTTPException(502, f"No se pudo consultar Vendure: {type(exc).__name__}")

    matched: VendureProduct | None = None
    score = 0.0
    matched_by: list[str] = []
    suggestion: LookupSuggestion | None = None
    # Candidato que el cliente ya descartó: no se le muestra de nuevo, pero
    # viaja en la consulta a Cloud para que nadie vuelva a proponerlo.
    rejected: LookupSuggestion | None = None

    # 2a. URL exacta del proveedor — gratis y seguro.
    if raw_url:
        matched = _url_match(raw_url, products) or _url_match(canonical, products)
        if matched is not None:
            score, matched_by = 1.0, ["url"]

    # 2b. Embeddings CLIP.
    if matched is None and image_embed.available():
        await catalog_index.ensure_index()
        if not catalog_index.is_ready():
            # Índice a medias diría "no lo tenemos" sobre cosas que sí tenemos, y
            # abriría un pedido en Cloud al pedo. Mejor pedirle al app que reintente.
            status = catalog_index.status()
            base.status = "indexing"
            base.detail = (
                "El índice de imágenes se está construyendo "
                f"({status['progress_done']}/{status['progress_total']}). Reintentá en un rato."
            )
            return base

        # Comparamos TODAS las fotos de la publicación, no solo la primera: la
        # foto principal suele ser la de marketing (packaging, con modelo, de
        # perfil) y la que se parece a la nuestra puede ser la tercera. Cada
        # imagen extra cuesta ~25 ms de inferencia — barato al lado de perder
        # un match que sí teníamos.
        query_vecs = await image_embed.embed_urls(image_urls[:_MAX_QUERY_IMAGES])
        if query_vecs:
            best_hit: tuple[VendureProduct, float] | None = None
            for vec in query_vecs:
                hits = catalog_index.search(vec, top_k=1)
                if hits and (best_hit is None or hits[0][1] > best_hit[1]):
                    best_hit = (hits[0][0], hits[0][1])
            if best_hit is not None:
                product, hit_score = best_hit
                # Thresholds por runtime: se ajustan desde el dashboard sin redeploy.
                threshold = float(runtime.get("embed_match_threshold"))
                if hit_score >= threshold:
                    matched, score, matched_by = product, hit_score, ["image_embed"]
                elif hit_score >= float(runtime.get("embed_suggest_threshold")):
                    candidate = LookupSuggestion(
                        product_id=product.id,
                        name=product.name,
                        product_code=product.product_code,
                        image_url=product.featured_image_url,
                        score=round(hit_score, 4),
                    )
                    score = hit_score
                    if payload.reject_suggestion:
                        # El cliente ya lo vio y dijo que no. No se lo volvemos a
                        # mostrar, pero viaja a Cloud: quien revise la consulta
                        # tiene que saber que ese candidato ya se descartó, para
                        # no volver a proponerlo.
                        rejected = candidate
                    else:
                        suggestion = candidate
                else:
                    score = max(score, hit_score)

    # 2c. Fallback pHash (CLIP no disponible).
    if matched is None and not image_embed.available():
        threshold = float(runtime.get("dedup_image_threshold"))
        candidate, phash_score = await _phash_scan(image_urls, products, threshold)
        if candidate is not None and phash_score >= threshold:
            matched, score, matched_by = candidate, phash_score, ["image_phash"]
        elif candidate is not None and phash_score > score:
            score = phash_score

    # ── 3a. Lo tenemos → PA + comprar ahora ────────────────────────
    if matched is not None:
        product_payload: LookupProduct
        try:
            full = await VendureClient().get_product_full(matched.id)
            product_payload = _product_from_full(full) if full else _product_from_cached(matched)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_product_full(%s) falló: %s", matched.id, exc)
            product_payload = _product_from_cached(matched)

        base.status = "found"
        base.found = True
        base.confidence = round(score, 4)
        base.matched_by = matched_by
        base.product = product_payload
        _record(
            action="app_lookup_match",
            detail=(
                f"El app buscó {canonical[:150]} y lo encontramos: "
                f"'{matched.name[:60]}' (PA {product_payload.pa or '—'}, "
                f"BX {product_payload.product_code or '—'}) por "
                f"{','.join(matched_by)} — confianza {score:.0%}"
            ),
            source=payload.source,
            product_id=matched.id,
            confidence=score,
            product_name=matched.name,
            image_url=image_urls[0],
            source_url=canonical,
        )
        return base

    # ── 3a-bis. Casi-match → preguntarle al cliente antes de nada ──
    # No abrimos la consulta en Cloud todavía: si el candidato es el correcto,
    # ese pedido nace muerto y alguien lo tiene que descartar a mano. Esperamos
    # la respuesta; si dice que no, el app vuelve con reject_suggestion=true.
    if suggestion is not None:
        try:
            full = await VendureClient().get_product_full(suggestion.product_id)
            if full:
                suggestion.product = _product_from_full(full)
        except Exception as exc:  # noqa: BLE001
            log.warning("get_product_full(%s) falló para la sugerencia: %s",
                        suggestion.product_id, exc)
        base.confidence = round(score, 4)
        base.suggestion = suggestion
        _record(
            action="app_lookup_suggested",
            detail=(
                f"El app buscó {canonical[:130]} y Hugo propuso "
                f"'{suggestion.name[:50]}' (BX {suggestion.product_code or '—'}) "
                f"con {score:.0%} — esperando que el cliente confirme"
            ),
            source=payload.source,
            product_id=suggestion.product_id,
            confidence=score,
            product_name=suggestion.name,
            image_url=image_urls[0],
            source_url=canonical,
        )
        return base

    # ── 3b. No lo tenemos → formulario a Cloud ─────────────────────
    base.confidence = round(score, 4)
    base.suggestion = suggestion

    if not cloud_integration.enabled():
        base.cloud_request = CloudRequestInfo(sent=False, error="CLOUD_URL no configurado")
        _record(
            action="app_lookup_request_failed",
            detail=f"'{(title or canonical)[:120]}' no está en el catálogo, "
                   "pero CLOUD_URL no está configurado y el pedido no se pudo abrir",
            source=payload.source,
            product_id="(nuevo)",
            confidence=score,
            product_name=title,
            image_url=image_urls[0],
            source_url=canonical,
        )
        return base

    # El formulario del app exige nombre + email + teléfono. Si el app no los
    # mandó, no llamamos: sería un 400 seguro y encima gastaría una de las 5
    # submissions que Cloud permite por ventana de 10 minutos.
    missing = cloud_integration.missing_client_fields(payload.client)
    if missing:
        base.cloud_request = CloudRequestInfo(
            sent=False,
            missing_fields=missing,
            error=(
                "Faltan datos del cliente para abrir la consulta: "
                f"{', '.join(missing)}. Pedíselos y reintentá el lookup."
            ),
        )
        _record(
            action="app_lookup_request_failed",
            detail=f"'{(title or canonical)[:120]}' no está en el catálogo, pero el app "
                   f"no mandó {', '.join(missing)} y el formulario no se pudo abrir",
            source=payload.source,
            product_id="(nuevo)",
            confidence=score,
            product_name=title,
            image_url=image_urls[0],
            source_url=canonical,
        )
        return base

    cloud_payload = cloud_integration.build_payload(
        client=payload.client,
        input_url=canonical or raw_url,
        image_urls=image_urls,
        title=title,
        marketplace=marketplace,
        note=payload.note,
        score=score,
        best_match=(rejected.model_dump(exclude={'product'}) if rejected else None),
        source=payload.source,
    )

    try:
        result = await cloud_integration.submit_request(cloud_payload)
        base.cloud_request = CloudRequestInfo(sent=True, request_id=result.request_id)
        _record(
            action="app_lookup_request_sent",
            detail=(
                f"'{(title or canonical)[:120]}' no está en el catálogo. Hugo abrió "
                f"la consulta del app en Cloud para {payload.client.name or 'el cliente'} "
                f"(consulta {result.request_id or 's/id'})"
            ),
            source=payload.source,
            product_id="(nuevo)",
            confidence=score,
            product_name=title,
            image_url=image_urls[0],
            source_url=canonical,
        )
    except cloud_integration.CloudError as exc:
        base.cloud_request = CloudRequestInfo(sent=False, error=str(exc))
        log.warning("Cloud request falló: %s", exc)
        _record(
            action="app_lookup_request_failed",
            detail=f"'{(title or canonical)[:120]}' no está en el catálogo y Cloud "
                   f"no aceptó el pedido: {str(exc)[:150]}",
            source=payload.source,
            product_id="(nuevo)",
            confidence=score,
            product_name=title,
            image_url=image_urls[0],
            source_url=canonical,
        )
    return base


@router.get("/index-status", dependencies=[Depends(verify_api_key)])
async def index_status() -> dict[str, Any]:
    """Estado del índice vectorial. Sirve para saber si /app/lookup ya puede responder."""
    return catalog_index.status()


@router.post("/index-rebuild", dependencies=[Depends(verify_api_key)])
async def index_rebuild() -> dict[str, Any]:
    """Fuerza la reconstrucción del índice (en background)."""
    catalog_index.invalidate()
    asyncio.create_task(catalog_index.build(force=True))
    return {"ok": True, "status": catalog_index.status()}
