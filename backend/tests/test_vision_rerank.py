"""Rerank por visión: lámina de contactos, veredictos y degradación."""

from io import BytesIO

import pytest
from PIL import Image

from app.dedup import vision_rerank
from app.vendure.client import VendureProduct


def _png(size=(800, 400), color=(200, 30, 40)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


_DEFAULT_IMAGE = object()


def _product(pid: str, image=_DEFAULT_IMAGE) -> VendureProduct:
    url = f"https://cdn.b2box.app/{pid}.jpg" if image is _DEFAULT_IMAGE else image
    return VendureProduct(
        id=pid,
        name=f"Organizador {pid}",
        slug=f"organizador-{pid}",
        description="",
        enabled=True,
        source_url="",
        image_urls=[url] if url else [],
        product_code=f"BX-{pid}",
        featured_image_url=url,
        first_variant_price_cents=1000,
        variant_count=1,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    vision_rerank._CACHE.clear()
    yield
    vision_rerank._CACHE.clear()


@pytest.fixture
def enabled(monkeypatch):
    s = vision_rerank.get_settings()
    monkeypatch.setattr(s, "vision_enabled", True, raising=False)
    monkeypatch.setattr(s, "anthropic_api_key", "sk-ant-test", raising=False)
    return s


# ─── Lámina de contactos ───────────────────────────────────────────


def test_contact_sheet_grid_dimensions():
    sheet = vision_rerank.build_contact_sheet([_png()] * 6, cell=100, columns=4)
    img = Image.open(BytesIO(sheet))
    # 6 fotos en grilla de 4 → 2 filas. La franja del número suma alto por fila.
    assert img.width == 4 * 100
    assert img.height > 2 * 100


def test_contact_sheet_letterboxes_instead_of_cropping():
    """Sin recorte: los bordes suelen ser lo que distingue dos productos parecidos."""
    sheet = vision_rerank.build_contact_sheet([_png((800, 200))], cell=100, columns=1)
    img = Image.open(BytesIO(sheet))
    assert img.width == 100  # celda cuadrada, la foto ancha entra entera


def test_contact_sheet_skips_corrupt_images():
    sheet = vision_rerank.build_contact_sheet([_png(), b"no soy una imagen"], cell=80)
    assert Image.open(BytesIO(sheet)).width > 0


def test_contact_sheet_raises_when_nothing_opens():
    with pytest.raises(ValueError):
        vision_rerank.build_contact_sheet([b"basura"], cell=80)


# ─── Disponibilidad y degradación ──────────────────────────────────


def test_unavailable_without_api_key(monkeypatch):
    s = vision_rerank.get_settings()
    monkeypatch.setattr(s, "vision_enabled", True, raising=False)
    monkeypatch.setattr(s, "anthropic_api_key", "", raising=False)
    assert vision_rerank.available(vision_rerank.ANTHROPIC) is False


def test_available_is_per_provider(monkeypatch, enabled):
    """Tener key de uno no habilita al otro."""
    monkeypatch.setattr(enabled, "openai_api_key", "", raising=False)
    assert vision_rerank.available(vision_rerank.ANTHROPIC) is True
    assert vision_rerank.available(vision_rerank.OPENAI) is False


def test_unavailable_when_disabled(monkeypatch, enabled):
    monkeypatch.setattr(enabled, "vision_enabled", False, raising=False)
    assert vision_rerank.available(vision_rerank.ANTHROPIC) is False
    assert vision_rerank.available(vision_rerank.OPENAI) is False


def test_unknown_provider_is_not_available(enabled):
    assert vision_rerank.available("gemini") is False


@pytest.mark.asyncio
async def test_pick_match_returns_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(vision_rerank, "available", lambda provider=None: False)
    assert await vision_rerank.pick_match(["http://x/a.jpg"], [_product("1")]) is None


@pytest.mark.asyncio
async def test_pick_match_returns_none_without_candidates(enabled):
    assert await vision_rerank.pick_match(["http://x/a.jpg"], []) is None


@pytest.mark.asyncio
async def test_pick_match_returns_none_when_download_fails(monkeypatch, enabled):
    async def boom(url):  # noqa: ARG001
        raise RuntimeError("SSRF blocked")

    monkeypatch.setattr(vision_rerank, "_fetch", boom)
    assert await vision_rerank.pick_match(["http://x/a.jpg"], [_product("1")]) is None


def test_shortlist_drops_candidates_without_photo():
    """Un candidato sin foto correría la numeración de la lámina."""
    out = vision_rerank._shortlist([_product("1", image=None), _product("2")], topk=5)
    assert [p.id for p in out] == ["2"]


def test_shortlist_filters_before_truncating():
    """Si recortara primero, un candidato sin foto ocuparía un lugar del top-K."""
    cands = [_product("1", image=None), _product("2"), _product("3")]
    assert [p.id for p in vision_rerank._shortlist(cands, topk=2)] == ["2", "3"]


# ─── Parseo del veredicto ──────────────────────────────────────────


def _parse(data, n=3):
    return vision_rerank._parse_verdict(
        data, [_product(str(i + 1)) for i in range(n)], "anthropic", "m", 10
    )


def test_parse_picks_candidate_by_one_based_index():
    verdict = _parse({"match": 2, "confidence": 0.9, "reason": "igual"})
    assert verdict.product.id == "2"
    assert verdict.confidence == 0.9
    assert verdict.provider == "anthropic"


def test_parse_returns_no_match_verdict():
    """`product=None` es una decisión, distinta de `None` (no pude decidir)."""
    verdict = _parse({"match": None, "confidence": 0.8, "reason": "ninguno"})
    assert verdict is not None
    assert verdict.product is None


def test_parse_ignores_out_of_range_index():
    """Índice alucinado: mejor caer a CLIP que agarrar otro producto."""
    assert _parse({"match": 7, "confidence": 0.9, "reason": "x"}) is None
    assert _parse({"match": 0, "confidence": 0.9, "reason": "x"}) is None


def test_parse_rejects_non_integer_match():
    assert _parse({"match": "2", "confidence": 0.9, "reason": "x"}) is None
    assert _parse({"match": True, "confidence": 0.9, "reason": "x"}) is None


def test_parse_returns_none_without_data():
    assert _parse(None) is None


def test_loads_rejects_non_json_and_non_objects():
    assert vision_rerank._loads("perdón, no puedo") is None
    assert vision_rerank._loads("[1, 2]") is None


# ─── Proveedores ───────────────────────────────────────────────────


def _payload(n=2):
    return vision_rerank._Payload(
        query_raws=[_png()],
        sheet=_png(),
        candidates=[_product(str(i + 1)) for i in range(n)],
        title="Organizador",
        parts=[("text", "hola"), ("jpeg", _png()), ("png", _png())],
    )


def _fake_anthropic(monkeypatch, response):
    async def fake_create(**kwargs):
        _fake_anthropic.seen = kwargs
        if isinstance(response, Exception):
            raise response
        return response

    class _Client:
        def __init__(self, **kwargs):  # noqa: ARG002
            self.messages = type("M", (), {"create": staticmethod(fake_create)})()

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)


def _fake_openai(monkeypatch, response):
    async def fake_create(**kwargs):
        _fake_openai.seen = kwargs
        if isinstance(response, Exception):
            raise response
        return response

    class _Client:
        def __init__(self, **kwargs):  # noqa: ARG002
            completions = type("C", (), {"create": staticmethod(fake_create)})()
            self.chat = type("Ch", (), {"completions": completions})()

    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _Client)


class _ABlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _AResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_ABlock(text)]
        self.stop_reason = stop_reason


class _OResponse:
    def __init__(self, text, refusal=None):
        message = type("Msg", (), {"content": text, "refusal": refusal})()
        self.choices = [type("Ch", (), {"message": message})()]


@pytest.mark.asyncio
async def test_anthropic_returns_verdict(monkeypatch, enabled):
    _fake_anthropic(monkeypatch, _AResponse('{"match": 1, "confidence": 0.9, "reason": "ok"}'))
    verdict = await vision_rerank._ask(vision_rerank.ANTHROPIC, _payload())
    assert verdict.product.id == "1"
    assert verdict.provider == "anthropic"


@pytest.mark.asyncio
async def test_anthropic_handles_refusal(monkeypatch, enabled):
    _fake_anthropic(monkeypatch, _AResponse("", stop_reason="refusal"))
    assert await vision_rerank._ask(vision_rerank.ANTHROPIC, _payload()) is None


@pytest.mark.asyncio
async def test_anthropic_survives_api_error(monkeypatch, enabled):
    _fake_anthropic(monkeypatch, RuntimeError("503"))
    assert await vision_rerank._ask(vision_rerank.ANTHROPIC, _payload()) is None


@pytest.mark.asyncio
async def test_openai_returns_verdict(monkeypatch, enabled):
    _fake_openai(monkeypatch, _OResponse('{"match": 2, "confidence": 0.7, "reason": "ok"}'))
    verdict = await vision_rerank._ask(vision_rerank.OPENAI, _payload())
    assert verdict.product.id == "2"
    assert verdict.provider == "openai"


@pytest.mark.asyncio
async def test_openai_sends_images_at_high_detail(monkeypatch, enabled):
    """En "low" redimensionan la lámina y se pierde el detalle que la justifica."""
    _fake_openai(monkeypatch, _OResponse('{"match": null, "confidence": 0.5, "reason": "-"}'))
    await vision_rerank._ask(vision_rerank.OPENAI, _payload())
    parts = _fake_openai.seen["messages"][1]["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert images and all(p["image_url"]["detail"] == "high" for p in images)


@pytest.mark.asyncio
async def test_openai_handles_refusal(monkeypatch, enabled):
    _fake_openai(monkeypatch, _OResponse(None, refusal="no puedo"))
    assert await vision_rerank._ask(vision_rerank.OPENAI, _payload()) is None


@pytest.mark.asyncio
async def test_openai_survives_api_error(monkeypatch, enabled):
    _fake_openai(monkeypatch, RuntimeError("429"))
    assert await vision_rerank._ask(vision_rerank.OPENAI, _payload()) is None


@pytest.mark.asyncio
async def test_unknown_provider_returns_none(enabled):
    assert await vision_rerank._ask("gemini", _payload()) is None


# ─── compare() ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_runs_both_on_the_same_payload(monkeypatch, enabled):
    """Misma lámina para los dos: si no, los resultados no son comparables."""
    monkeypatch.setattr(enabled, "openai_api_key", "sk-openai-test", raising=False)
    payloads = []

    async def fake_fetch(url):  # noqa: ARG001
        return _png()

    async def fake_ask(provider, payload):
        payloads.append(payload)
        return vision_rerank.Verdict(
            payload.candidates[0], 0.9, "ok", provider, "m", 1
        )

    monkeypatch.setattr(vision_rerank, "_fetch", fake_fetch)
    monkeypatch.setattr(vision_rerank, "_ask", fake_ask)

    out = await vision_rerank.compare(["http://x/a.jpg"], [_product("1")])

    assert set(out) == {"anthropic", "openai"}
    assert payloads[0] is payloads[1]


@pytest.mark.asyncio
async def test_compare_isolates_a_provider_that_blows_up(monkeypatch, enabled):
    monkeypatch.setattr(enabled, "openai_api_key", "sk-openai-test", raising=False)

    async def fake_fetch(url):  # noqa: ARG001
        return _png()

    async def fake_ask(provider, payload):
        if provider == vision_rerank.OPENAI:
            raise RuntimeError("boom")
        return vision_rerank.Verdict(payload.candidates[0], 0.9, "ok", provider, "m", 1)

    monkeypatch.setattr(vision_rerank, "_fetch", fake_fetch)
    monkeypatch.setattr(vision_rerank, "_ask", fake_ask)

    out = await vision_rerank.compare(["http://x/a.jpg"], [_product("1")])

    assert out["openai"] is None
    assert out["anthropic"].product.id == "1"


@pytest.mark.asyncio
async def test_compare_skips_providers_without_key(monkeypatch, enabled):
    monkeypatch.setattr(enabled, "openai_api_key", "", raising=False)

    async def fake_fetch(url):  # noqa: ARG001
        return _png()

    async def fake_ask(provider, payload):
        return vision_rerank.Verdict(payload.candidates[0], 0.9, "ok", provider, "m", 1)

    monkeypatch.setattr(vision_rerank, "_fetch", fake_fetch)
    monkeypatch.setattr(vision_rerank, "_ask", fake_ask)

    out = await vision_rerank.compare(["http://x/a.jpg"], [_product("1")])
    assert set(out) == {"anthropic"}


# ─── Cache ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_is_cached_per_query_candidates_and_provider(monkeypatch, enabled):
    calls = []

    async def fake_fetch(url):  # noqa: ARG001
        return _png()

    async def fake_ask(provider, payload):
        calls.append(provider)
        return vision_rerank.Verdict(payload.candidates[0], 0.9, "ok", provider, "m", 1)

    monkeypatch.setattr(vision_rerank, "_fetch", fake_fetch)
    monkeypatch.setattr(vision_rerank, "_ask", fake_ask)

    cands = [_product("1"), _product("2")]
    first = await vision_rerank.pick_match(["http://x/a.jpg"], cands)
    second = await vision_rerank.pick_match(["http://x/a.jpg"], cands)
    assert first is second
    assert len(calls) == 1

    await vision_rerank.pick_match(["http://x/b.jpg"], cands)
    assert len(calls) == 2

    # Mismo pedido, otro proveedor: no puede devolver el veredicto del primero.
    monkeypatch.setattr(enabled, "openai_api_key", "sk-openai-test", raising=False)
    await vision_rerank.pick_match(["http://x/a.jpg"], cands, provider="openai")
    assert calls[-1] == "openai"


# ─── Endpoint de comparación ───────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_endpoint_devuelve_los_dos_veredictos(monkeypatch):
    """Un link, los dos proveedores, la misma lista corta."""
    import numpy as np

    from app.api import app_routes
    from app.api.app_routes import AppLookupRequest
    from app.ingest.image_from_url import ExtractedProduct

    async def fake_extract(url):  # noqa: ARG001
        return ExtractedProduct(
            image_urls=["https://cdn.ml/1.jpg"], title="Organizador",
            marketplace="mercadolibre", canonical_url=url, kind="page",
        )

    async def fake_embed(urls, concurrency=4):  # noqa: ARG001
        return [np.ones(4, dtype=np.float32) for _ in urls]

    async def noop():
        return None

    async def fake_compare(query_urls, candidates, *, title=""):  # noqa: ARG001
        return {
            "anthropic": vision_rerank.Verdict(
                candidates[0], 0.91, "es el mismo", "anthropic", "claude", 8000
            ),
            "openai": vision_rerank.Verdict(None, 0.62, "ninguno", "openai", "gpt", 5000),
        }

    monkeypatch.setattr(app_routes.image_from_url, "extract", fake_extract)
    monkeypatch.setattr(app_routes.image_embed, "available", lambda: True)
    monkeypatch.setattr(app_routes.image_embed, "embed_urls_aligned", fake_embed)
    monkeypatch.setattr(app_routes.catalog_index, "ensure_index", noop)
    monkeypatch.setattr(app_routes.catalog_index, "is_ready", lambda: True)
    monkeypatch.setattr(
        app_routes.catalog_index, "search",
        lambda v, top_k=1: [(_product("1"), 0.71, "x"), (_product("2"), 0.64, "x")],
    )
    monkeypatch.setattr(app_routes.vision_rerank, "compare", fake_compare)

    out = await app_routes.run_vision_compare(
        AppLookupRequest(url="https://articulo.mercadolibre.com.ar/MLA-1")
    )

    assert out["status"] == "ok"
    assert out["verdicts"]["anthropic"] == {
        "answered": True, "found": True, "product_id": "1",
        "product_name": "Organizador 1", "product_code": "BX-1",
        "image_url": "https://cdn.b2box.app/1.jpg", "confidence": 0.91,
        "reason": "es el mismo", "model": "claude", "elapsed_ms": 8000,
    }
    # "miró y dijo que ninguno" ≠ "no pudo contestar".
    assert out["verdicts"]["openai"]["answered"] is True
    assert out["verdicts"]["openai"]["found"] is False
    # Los candidatos que vieron, ordenados por score de CLIP.
    assert [c["id"] for c in out["candidates"]] == ["1", "2"]


@pytest.mark.asyncio
async def test_compare_endpoint_marca_al_proveedor_que_no_contesto(monkeypatch):
    import numpy as np

    from app.api import app_routes
    from app.api.app_routes import AppLookupRequest
    from app.ingest.image_from_url import ExtractedProduct

    async def fake_extract(url):  # noqa: ARG001
        return ExtractedProduct(
            image_urls=["https://cdn.ml/1.jpg"], title="x",
            marketplace="mercadolibre", canonical_url=url, kind="page",
        )

    async def fake_embed(urls, concurrency=4):  # noqa: ARG001
        return [np.ones(4, dtype=np.float32) for _ in urls]

    async def noop():
        return None

    async def fake_compare(query_urls, candidates, *, title=""):  # noqa: ARG001
        return {"openai": None}

    monkeypatch.setattr(app_routes.image_from_url, "extract", fake_extract)
    monkeypatch.setattr(app_routes.image_embed, "available", lambda: True)
    monkeypatch.setattr(app_routes.image_embed, "embed_urls_aligned", fake_embed)
    monkeypatch.setattr(app_routes.catalog_index, "ensure_index", noop)
    monkeypatch.setattr(app_routes.catalog_index, "is_ready", lambda: True)
    monkeypatch.setattr(
        app_routes.catalog_index, "search", lambda v, top_k=1: [(_product("1"), 0.7, "x")]
    )
    monkeypatch.setattr(app_routes.vision_rerank, "compare", fake_compare)

    out = await app_routes.run_vision_compare(AppLookupRequest(url="https://x/y"))

    assert out["verdicts"]["openai"] == {"answered": False, "found": False}
