"""Búsqueda en 1688 por foto con Apify (app.ingest.apify_1688).

Sin red: la API de Apify se reemplaza con un MockTransport de httpx que hace
de corridas (lanzar, poll, dataset, abort). Lo que se cubre es lo que puede
salir caro o inseguro: que el cupo corte antes de llamar, que la caché no
vuelva a pagar, que la corrida de cobertura se lance y aborte a tiempo, que lo
que vuelve pase por la lista blanca y que el token no aparezca nunca en un log.
Los tiempos del hedge son chicos e inyectados (décimas de segundo, no 20 s).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx
import pytest
from sqlmodel import Session, SQLModel, select

from app.config import get_settings
from app.db.models import Setting
from app.db.session import engine
from app.ingest import apify_1688

TOKEN = "apify_api_SECRETO_1688_no_tiene_que_salir_9876"
ACTOR = "piotrv1001/1688-image-search-scraper"
PHOTO = "https://storage.example-bucket.com/fotos/cliente.jpg?X-Amz-Signature=abc123"
PHOTO_HASH = "a" * 64

ITEM_A = {
    "offerId": "675101017401",
    "title": "宠物漏食玩具 不倒翁",
    "price": 11.7,
    "currency": "CNY",
    "imageUrl": "https://cbu01.alicdn.com/img/ibank/O1CN01abc.jpg",
    "similarityScore": 0.115,
    "soldQuantity": 320,
    "companyName": "义乌市某某宠物用品厂",
    "sellerMemberId": "b2b-123",
    "url": "https://evil.example.com/no-se-usa",
}
ITEM_B = {
    "offerId": "abb-666233922092",
    "title": "MICRO-200 电池检测仪",
    "price": "86.58",
    "imageUrl": "https://img.alicdn.com/imgextra/i1/abc.jpg",
    "similarityScore": 1.058,
    "ordersCount": "15",
    "companyName": "深圳某某电子",
}


@pytest.fixture
def apify_on(monkeypatch):
    """Apify prendido con cupo, contador del día en cero, tiempos chicos."""
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        s.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "apify_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "apify_1688_actor", ACTOR, raising=False)
    monkeypatch.setattr(settings, "apify_1688_build", "", raising=False)
    monkeypatch.setattr(settings, "apify_1688_hedge_after_s", 0.2, raising=False)
    monkeypatch.setattr(apify_1688, "_daily_budget", lambda: 100)
    monkeypatch.setattr(apify_1688, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(apify_1688, "_MIN_RUN_WINDOW_S", 0.0)
    apify_1688._reset_for_tests()
    yield settings
    apify_1688._reset_for_tests()


class FakeApify:
    """Hace de la API de Apify. `behaviors` dice qué hace cada corrida lanzada, en
    orden (la última se repite): ok, empty, hang (RUNNING para siempre), fail,
    late (RUNNING hasta `late_s` después de lanzada, después ok)."""

    def __init__(self, *behaviors: str, items: list | None = None, raw_items: bytes | None = None,
                 start_status: int = 201, usage: float = 0.021, exc: Exception | None = None,
                 late_s: float = 0.0):
        self.behaviors = list(behaviors) or ["ok"]
        self.items = [ITEM_A, ITEM_B] if items is None else items
        self.raw_items = raw_items
        self.start_status = start_status
        self.usage = usage
        self.exc = exc
        self.late_s = late_s
        self.requests: list[httpx.Request] = []
        self.starts: list[float] = []
        self.aborted: list[str] = []

    def _behavior(self, n: int) -> str:
        return self.behaviors[min(n, len(self.behaviors)) - 1]

    def _run(self, n: int) -> dict:
        behavior = self._behavior(n)
        rid = f"run{n}"
        if rid in self.aborted:
            status, usage = "ABORTED", 0.0
        else:
            if behavior == "late":
                # Termina bien recién `late_s` después de lanzada.
                late = time.monotonic() - self.starts[n - 1] < self.late_s
                behavior = "hang" if late else "ok"
            status = {"ok": "SUCCEEDED", "empty": "SUCCEEDED", "hang": "RUNNING",
                      "fail": "FAILED"}[behavior]
            usage = self.usage if behavior == "ok" else 0.0
        return {"data": {"id": rid, "status": status, "defaultDatasetId": f"ds{n}",
                         "usageTotalUsd": usage}}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            self.starts.append(time.monotonic())
            if self.start_status not in (200, 201):
                # Un body con datos que NO pueden terminar en el log.
                return httpx.Response(self.start_status,
                                      json={"error": {"message": f"token {TOKEN}"}})
            return httpx.Response(201, json={"data": {"id": f"run{len(self.starts)}",
                                                      "status": "READY",
                                                      "defaultDatasetId": f"ds{len(self.starts)}"}})
        if m := re.fullmatch(r"/v2/actor-runs/run(\d+)/abort", path):
            self.aborted.append(f"run{m.group(1)}")
            return httpx.Response(200, json=self._run(int(m.group(1))))
        if m := re.fullmatch(r"/v2/actor-runs/run(\d+)", path):
            return httpx.Response(200, json=self._run(int(m.group(1))))
        if m := re.fullmatch(r"/v2/datasets/ds(\d+)/items", path):
            if self.raw_items is not None:
                return httpx.Response(200, content=self.raw_items)
            items = self.items if self._behavior(int(m.group(1))) in ("ok", "late") else []
            return httpx.Response(200, json=items)
        return httpx.Response(404)

    def install(self, monkeypatch) -> FakeApify:
        def factory(total_s):
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

        monkeypatch.setattr(apify_1688, "_new_client", factory)
        return self


def _used() -> int:
    return apify_1688.daily_budget.used_today(apify_1688.COUNTER_KEY)


# ─── Camino feliz ──────────────────────────────────────────────────


async def test_ok_trae_candidatos_validados(apify_on, monkeypatch):
    fake = FakeApify("ok").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)
    await apify_1688.wait_pending_aborts()

    assert result.status == "ok"
    assert result.hedged is False and result.cached is False
    assert result.run_ids == ["run1"]
    assert result.usd_imputado == pytest.approx(0.021)
    assert [c.offer_id for c in result.candidates] == ["675101017401", "666233922092"]
    a, b = result.candidates
    assert a.price_cny == 11.7 and b.price_cny == 86.58
    assert a.title == "宠物漏食玩具 不倒翁"
    assert (a.similarity, a.sold_quantity, a.company) == (0.115, 320, "义乌市某某宠物用品厂")
    assert b.sold_quantity == 15
    # El link se arma desde el id, nunca con el `url` del actor.
    assert a.detail_url == "https://detail.1688.com/offer/675101017401.html"
    assert fake.aborted == []
    assert _used() == 1

    start = fake.requests[0]
    assert start.method == "POST"
    assert start.url.host == "api.apify.com"
    assert start.url.path == "/v2/acts/piotrv1001~1688-image-search-scraper/runs"
    assert dict(start.url.params) == {"timeout": "120", "memory": "1024", "maxItems": "20",
                                      "maxTotalChargeUsd": "0.05"}
    assert json.loads(start.content) == {
        "imageUrls": [PHOTO], "maxResultsPerImage": 20, "excludeSponsored": True,
        "proxyConfiguration": {"useApifyProxy": True}}
    for req in fake.requests:
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(req.url)
    polls = [r for r in fake.requests if r.url.path == "/v2/actor-runs/run1"]
    assert polls and polls[0].url.params["waitForFinish"] == "5"
    assert any(r.url.path == "/v2/datasets/ds1/items" for r in fake.requests)


async def test_orden_del_actor_se_respeta(apify_on, monkeypatch):
    """No se reordena por similarityScore (medido: es ruido); lo hace CLIP después."""
    FakeApify("ok").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert [c.similarity for c in result.candidates] == [0.115, 1.058]


async def test_usd_imputado_tiene_piso_por_corrida(apify_on, monkeypatch):
    FakeApify("ok", usage=0.001).install(monkeypatch)
    assert (await apify_1688.search_by_image(PHOTO)).usd_imputado == pytest.approx(0.02)


async def test_build_fijado_viaja_en_el_query(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_build", "1.0.12", raising=False)
    fake = FakeApify("ok").install(monkeypatch)

    await apify_1688.search_by_image(PHOTO)

    assert fake.requests[0].url.params["build"] == "1.0.12"


async def test_sin_candidatos_no_paga_cobertura(apify_on, monkeypatch):
    fake = FakeApify("empty").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "no_candidates" and result.candidates == []
    assert len(fake.starts) == 1 and result.hedged is False


# ─── Apagado, lista blanca y cupo ──────────────────────────────────


async def test_sin_token_es_disabled(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_token", "  ", raising=False)
    fake = FakeApify().install(monkeypatch)

    assert (await apify_1688.search_by_image(PHOTO)).status == "disabled"
    assert apify_1688.enabled() is False
    assert fake.requests == []


@pytest.mark.parametrize("actor", [
    "khadinakbar/mercadolibre-all-in-one-scraper",
    "otro/1688-image-search-scraper",
    "../../users/me",
    "",
])
async def test_actor_fuera_de_lista_blanca_no_se_llama(apify_on, monkeypatch, actor):
    monkeypatch.setattr(apify_on, "apify_1688_actor", actor, raising=False)
    fake = FakeApify().install(monkeypatch)

    assert (await apify_1688.search_by_image(PHOTO)).status == "disabled"
    assert fake.requests == []
    assert _used() == 0


async def test_build_con_forma_rara_no_se_llama(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_build", "latest&token=x", raising=False)
    fake = FakeApify().install(monkeypatch)

    assert (await apify_1688.search_by_image(PHOTO)).status == "disabled"
    assert fake.requests == []


async def test_cupo_cero_es_over_budget_sin_llamadas(apify_on, monkeypatch):
    monkeypatch.setattr(apify_1688, "_daily_budget", lambda: 0)
    fake = FakeApify().install(monkeypatch)

    assert (await apify_1688.search_by_image(PHOTO)).status == "over_budget"
    assert fake.requests == []


async def test_cupo_agotado_es_over_budget_sin_llamadas(apify_on, monkeypatch, caplog):
    monkeypatch.setattr(apify_1688, "_daily_budget", lambda: 3)
    with Session(engine) as s:
        s.add(Setting(key=apify_1688.COUNTER_KEY,
                      value=f"{apify_1688.daily_budget.today_utc()}:3"))
        s.commit()
    fake = FakeApify().install(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.ingest.apify_1688"):
        result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "over_budget"
    assert fake.requests == []
    assert "cupo diario agotado" in caplog.text


def test_default_del_cupo_es_apagado():
    from app.config import Settings

    assert Settings(vendure_api_url="https://example.invalid/x").apify_1688_daily_budget == 0


def test_defaults_de_tiempo_simulados_por_qa():
    """12/38: ~70 % con resultado en la simulación con los 15 tiempos medidos."""
    import inspect

    from app.config import Settings

    assert Settings(vendure_api_url="https://example.invalid/x").apify_1688_hedge_after_s == 12.0
    default = inspect.signature(apify_1688.search_by_image).parameters["deadline_s"].default
    assert default == apify_1688.DEFAULT_DEADLINE_S == 38.0


# ─── Caché ─────────────────────────────────────────────────────────


async def test_misma_foto_sale_de_la_cache_sin_gastar(apify_on, monkeypatch):
    fake = FakeApify("ok").install(monkeypatch)

    first = await apify_1688.search_by_image(PHOTO)
    # Mismo URL con otro fragmento y el host en mayúsculas: misma foto.
    again = await apify_1688.search_by_image(
        PHOTO.replace("storage.example-bucket.com", "STORAGE.example-bucket.com") + "#x")

    assert first.status == again.status == "ok"
    assert again.cached is True and again.usd_imputado == 0.0
    assert [c.offer_id for c in again.candidates] == [c.offer_id for c in first.candidates]
    assert len(fake.starts) == 1
    assert _used() == 1


async def test_cache_por_hash_de_bytes_aunque_cambie_la_url(apify_on, monkeypatch):
    """La misma foto subida dos veces trae otra URL firmada: la clave son los bytes."""
    fake = FakeApify("ok").install(monkeypatch)

    await apify_1688.search_by_image(PHOTO, trusted_bytes_sha256=PHOTO_HASH)
    again = await apify_1688.search_by_image(PHOTO.replace("abc123", "zzz999"),
                                             trusted_bytes_sha256=PHOTO_HASH.upper())

    assert again.cached is True
    assert len(fake.starts) == 1


async def test_hash_con_forma_rara_cae_a_la_url(apify_on, monkeypatch):
    fake = FakeApify("ok").install(monkeypatch)

    await apify_1688.search_by_image(PHOTO, trusted_bytes_sha256="no-es-un-sha256")
    await apify_1688.search_by_image(PHOTO.replace("abc123", "zzz999"),
                                     trusted_bytes_sha256="no-es-un-sha256")

    assert len(fake.starts) == 2


async def test_error_se_cachea_una_hora(apify_on, monkeypatch):
    fake = FakeApify(start_status=402).install(monkeypatch)

    assert (await apify_1688.search_by_image(PHOTO)).status == "error"
    second = await apify_1688.search_by_image(PHOTO)
    assert second.status == "error" and second.cached is True
    assert len(fake.starts) == 1

    # Vencido el TTL corto (p. ej. después de cargar saldo), se reintenta.
    monkeypatch.setattr(apify_1688, "_CACHE_ERROR_TTL_S", 0)
    apify_1688._reset_for_tests()
    await apify_1688.search_by_image(PHOTO)
    await apify_1688.search_by_image(PHOTO)
    assert len(fake.starts) == 3


def test_cache_acotada_lru(monkeypatch):
    apify_1688._reset_for_tests()
    monkeypatch.setattr(apify_1688, "_CACHE_MAX", 2)
    hit = apify_1688.Search1688Result(status="ok")
    apify_1688._cache_put("a", hit, 60)
    apify_1688._cache_put("b", hit, 60)
    assert apify_1688._cache_get("a") is hit       # "a" pasa a ser el más reciente
    apify_1688._cache_put("c", hit, 60)
    assert apify_1688._cache_get("b") is None      # se fue el menos usado
    assert apify_1688._cache_get("a") is hit and apify_1688._cache_get("c") is hit
    apify_1688._reset_for_tests()


# ─── Corrida de cobertura (hedge) ──────────────────────────────────


async def test_corrida_colgada_se_cubre_gana_la_segunda_y_se_aborta_la_primera(
        apify_on, monkeypatch):
    fake = FakeApify("hang", "ok").install(monkeypatch)
    t0 = time.monotonic()

    result = await apify_1688.search_by_image(PHOTO, deadline_s=3.0)
    await apify_1688.wait_pending_aborts()

    assert result.status == "ok"
    assert result.hedged is True
    assert result.run_ids == ["run1", "run2"]
    assert len(result.candidates) == 2
    # La 2 sale recién al cumplirse el hedge (0,2 s acá; 20 s en prod).
    assert len(fake.starts) == 2
    assert fake.starts[1] - t0 >= 0.18
    assert result.elapsed_s < 2.0
    # Se aborta la perdedora, no la ganadora.
    assert fake.aborted == ["run1"]
    # Cada corrida lanzada reserva cupo; se imputa el piso a la abortada.
    assert _used() == 2
    assert result.usd_imputado == pytest.approx(0.02 + 0.021)


async def test_ambas_colgadas_timeout_al_deadline_y_abort_de_las_dos(apify_on, monkeypatch):
    fake = FakeApify("hang").install(monkeypatch)

    result = await asyncio.wait_for(apify_1688.search_by_image(PHOTO, deadline_s=0.6), timeout=3)
    await apify_1688.wait_pending_aborts()

    assert result.status == "timeout"
    assert result.candidates == []
    assert result.hedged is True and result.run_ids == ["run1", "run2"]
    # El abort corre en segundo plano: no le suma al deadline.
    assert 0.55 <= result.elapsed_s < 1.0
    assert sorted(fake.aborted) == ["run1", "run2"]
    assert result.usd_imputado == pytest.approx(0.04)


async def test_sin_cupo_para_la_cobertura_espera_solo_a_la_primera(apify_on, monkeypatch):
    monkeypatch.setattr(apify_1688, "_daily_budget", lambda: 1)
    fake = FakeApify("hang").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=0.5)
    await apify_1688.wait_pending_aborts()

    assert result.status == "timeout" and result.hedged is False
    assert len(fake.starts) == 1
    assert fake.aborted == ["run1"]


async def test_primera_que_falla_rapido_se_cubre_enseguida(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("fail", "ok").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=3.0)

    assert result.status == "ok" and result.hedged is True
    assert len(fake.starts) == 2
    assert result.elapsed_s < 1.0


async def test_error_transitorio_al_lanzar_se_cubre_una_vez(apify_on, monkeypatch):
    fake = FakeApify(start_status=502).install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "error"
    # Sin run id no hay corrida que imputar.
    assert result.run_ids == [] and result.usd_imputado == 0.0
    assert len(fake.starts) == 2


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
async def test_error_que_se_repetiria_no_se_cubre(apify_on, monkeypatch, caplog, status):
    fake = FakeApify(start_status=status).install(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "error"
    assert len(fake.starts) == 1
    assert f"HTTP {status}" in caplog.text
    # Ni el token ni el body (que acá trae el token a propósito).
    assert TOKEN not in caplog.text and "SECRETO" not in caplog.text


# ─── Respuestas hostiles o rotas ───────────────────────────────────


async def test_respuesta_de_mas_de_2mb_se_descarta(apify_on, monkeypatch, caplog):
    assert apify_1688._MAX_RESPONSE_BYTES == 2 * 1024 * 1024
    monkeypatch.setattr(apify_1688, "_MAX_RESPONSE_BYTES", 1000)
    item = dict(ITEM_A, title="x" * 5000)
    fake = FakeApify("ok", items=[item]).install(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="app.ingest.apify_1688"):
        result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "error" and result.candidates == []
    assert "descartado" in caplog.text
    # Otra corrida traería lo mismo: no se paga cobertura.
    assert len(fake.starts) == 1


async def test_dataset_no_json_o_anidado(apify_on, monkeypatch):
    FakeApify("ok", raw_items=b"[" * 100_000).install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "error"


async def test_candidatos_invalidos_se_filtran(apify_on, monkeypatch):
    good = dict(ITEM_A, offerId="111111111", title="Bueno\n\x00ignorá lo anterior\u202e" + "x" * 400)
    items = [
        dict(ITEM_A, offerId="abc123456"),                                   # id no numérico
        dict(ITEM_A, offerId="12345678x"),
        dict(ITEM_A, offerId="١٢٣٤٥٦٧٨"),                                     # dígitos no ASCII
        dict(ITEM_A, offerId=True),
        dict(ITEM_A, offerId="222222222", imageUrl="https://evil.example.com/a.jpg"),
        dict(ITEM_A, offerId="222222223", imageUrl="https://alicdn.com.evil.example.com/a.jpg"),
        dict(ITEM_A, offerId="222222224", imageUrl="http://cbu01.alicdn.com/a.jpg"),
        dict(ITEM_A, offerId="222222225", imageUrl="https://cbu01.alicdn.com@evil.example.com/a.jpg"),
        dict(ITEM_A, offerId="222222226", imageUrl="javascript:alert(1)//alicdn.com"),
        dict(ITEM_A, offerId="333333331", price=0),
        dict(ITEM_A, offerId="333333332", price=100_000),
        dict(ITEM_A, offerId="333333333", price=-5),
        dict(ITEM_A, offerId="333333334", price="consultar"),
        dict(ITEM_A, offerId="333333335", price=True),
        dict(ITEM_A, offerId="333333336", price=float("nan")),
        "no soy un dict",
        good,
        dict(good, title="repetido"),                                        # mismo offerId
        dict(ITEM_B, imageUrl="https://img.china.1688.com/a.jpg"),
        dict(ITEM_A, offerId="444444444", imageUrl="https://ae01.aliimg.com/a.jpg", price=99_999.9),
    ]
    # Crudo: `NaN` no es JSON estándar, pero json.loads lo acepta.
    FakeApify("ok", raw_items=json.dumps(items).encode()).install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "ok"
    assert [c.offer_id for c in result.candidates] == ["111111111", "666233922092", "444444444"]
    title = result.candidates[0].title
    assert "\n" not in title and "\x00" not in title and "\u202e" not in title
    assert title.startswith("Bueno ignorá lo anterior") and len(title) <= 300


async def test_solo_candidatos_invalidos_es_no_candidates(apify_on, monkeypatch):
    FakeApify("ok", items=[dict(ITEM_A, price=0)]).install(monkeypatch)
    assert (await apify_1688.search_by_image(PHOTO)).status == "no_candidates"


@pytest.mark.parametrize("score, expected", [
    (0.115, 0.115), (0, 0.0), (1.058, 1.058), (100, 100.0), ("0.5", 0.5),
    (-0.1, None), (100.01, None), (1e308, None), (float("inf"), None), (float("nan"), None),
    (True, None), ("alto", None), (None, None), (10**400, None),
])
def test_similarity_fuera_de_rango_es_none(score, expected):
    (cand,) = apify_1688.normalize([dict(ITEM_A, similarityScore=score)])
    assert cand.similarity == expected


def test_como_mucho_20_candidatos():
    items = [dict(ITEM_A, offerId=str(10_000_000 + n)) for n in range(50)]
    assert len(apify_1688.normalize(items)) == 20


# ─── Token y fotos que no se mandan ────────────────────────────────


async def test_token_nunca_en_logs(apify_on, monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        FakeApify("ok").install(monkeypatch)
        assert (await apify_1688.search_by_image(PHOTO)).status == "ok"
        apify_1688._reset_for_tests()
        FakeApify("hang").install(monkeypatch)
        await apify_1688.search_by_image(PHOTO, deadline_s=0.4)
        await apify_1688.wait_pending_aborts()
        apify_1688._reset_for_tests()
        FakeApify(exc=httpx.ReadTimeout(f"timeout leyendo con {TOKEN}")).install(monkeypatch)
        assert (await apify_1688.search_by_image(PHOTO)).status == "error"

    assert "ReadTimeout" in caplog.text
    assert TOKEN not in caplog.text
    # Tampoco la URL firmada de la foto: solo su host.
    assert "X-Amz-Signature" not in caplog.text


async def test_cualquier_sorpresa_termina_en_error(apify_on, monkeypatch, caplog):
    def roto():
        raise RuntimeError(f"la DB se cayó con {TOKEN}")

    monkeypatch.setattr(apify_1688, "_daily_budget", roto)
    fake = FakeApify().install(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "error"
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text
    assert fake.requests == []


@pytest.mark.parametrize("url", [
    "http://storage.example-bucket.com/a.jpg",
    "https://127.0.0.1/a.jpg",
    "https://10.0.0.5/a.jpg",
    "https://192.168.1.10/a.jpg",
    "https://169.254.169.254/latest/meta-data",
    "https://100.100.100.200/a.jpg",
    "https://[::1]/a.jpg",
    "https://[::ffff:10.0.0.1]/a.jpg",
    "https://localhost/a.jpg",
    "https://api.localhost/a.jpg",
    "https://nas.local/a.jpg",
    "https://db.internal/a.jpg",
    "https://intranet/a.jpg",
    "https://2130706433/a.jpg",
    "https://0x7f.1/a.jpg",
    "https://user:pass@storage.example-bucket.com/a.jpg",
    "https://storage.example-bucket.com:8443/a.jpg",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "",
    None,
])
async def test_foto_que_no_es_https_publica_no_va_a_apify(apify_on, monkeypatch, url):
    fake = FakeApify().install(monkeypatch)

    result = await apify_1688.search_by_image(url)

    assert result.status == "error"
    assert fake.requests == []
    assert _used() == 0


# ─── Concurrencia ──────────────────────────────────────────────────


async def test_cuarta_busqueda_en_paralelo_es_busy(apify_on, monkeypatch):
    monkeypatch.setattr(apify_1688, "_SLOT_WAIT_S", 0.05)
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("hang").install(monkeypatch)

    results = await asyncio.gather(*(
        apify_1688.search_by_image(f"https://storage.example-bucket.com/{n}.jpg", deadline_s=0.4)
        for n in range(4)))
    await apify_1688.wait_pending_aborts()

    assert sorted(r.status for r in results) == ["busy", "timeout", "timeout", "timeout"]
    assert len(fake.starts) == 3
    # La que se salteó no reservó cupo.
    assert _used() == 3


# ─── Regresiones (QA, 20-oct) ──────────────────────────────────────


async def test_entero_gigante_no_tira_todos_los_candidatos(apify_on, monkeypatch):
    """`float(10**400)` lanza OverflowError: tiraba la corrida entera (y pagaba
    una de cobertura que traía lo mismo) por un campo roto de una sola oferta."""
    raw = json.dumps([dict(ITEM_A, soldQuantity=10**400, similarityScore=-10**400),
                      dict(ITEM_A, offerId="999999999", price=10**400),
                      ITEM_B]).encode()
    fake = FakeApify("ok", raw_items=raw).install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "ok"
    assert [c.offer_id for c in result.candidates] == ["675101017401", "666233922092"]
    a = result.candidates[0]
    assert a.sold_quantity is None and a.similarity is None
    assert len(fake.starts) == 1 and _used() == 1


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -1.0, 0, "35", True, None])
async def test_deadline_invalido_no_llama_ni_gasta_cupo(apify_on, monkeypatch, deadline):
    """NaN dejaba el loop sin corte (colgado hasta que Apify cortara); <= 0 o un
    tipo raro reservaban cupo sin lanzar nada."""
    fake = FakeApify("hang").install(monkeypatch)

    result = await asyncio.wait_for(apify_1688.search_by_image(PHOTO, deadline_s=deadline),
                                    timeout=2)

    assert result.status == "error"
    assert fake.requests == [] and _used() == 0


@pytest.mark.parametrize("digest", [123, ["a" * 64], b"a" * 64])
async def test_hash_que_no_es_str_cae_a_la_url(apify_on, monkeypatch, digest):
    fake = FakeApify("ok").install(monkeypatch)

    first = await apify_1688.search_by_image(PHOTO, trusted_bytes_sha256=digest)
    again = await apify_1688.search_by_image(PHOTO, trusted_bytes_sha256=digest)

    assert first.status == "ok" and again.cached is True
    assert len(fake.starts) == 1


def _slow_reserve(monkeypatch, *, slow_call: int, seconds: float):
    """La reserva número `slow_call` tarda `seconds` (una base trabada)."""
    real = apify_1688.daily_budget.reserve_async
    calls = []

    async def reserve(*args, **kwargs):
        calls.append(1)
        if len(calls) == slow_call:
            await asyncio.sleep(seconds)
        return await real(*args, **kwargs)

    monkeypatch.setattr(apify_1688.daily_budget, "reserve_async", reserve)


async def test_reserva_lenta_de_la_cobertura_no_estira_el_deadline(apify_on, monkeypatch):
    _slow_reserve(monkeypatch, slow_call=2, seconds=3.0)
    fake = FakeApify("hang").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=0.6)
    await apify_1688.wait_pending_aborts()

    assert result.status == "timeout"
    assert result.elapsed_s < 0.9
    assert len(fake.starts) == 1 and fake.aborted == ["run1"]


async def test_reserva_lenta_de_la_primera_no_estira_el_deadline(apify_on, monkeypatch):
    _slow_reserve(monkeypatch, slow_call=1, seconds=3.0)
    fake = FakeApify("ok").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=0.4)

    assert result.status == "timeout" and result.elapsed_s < 0.7
    assert fake.requests == []


# ─── Huecos de cobertura (QA, 20-oct) ──────────────────────────────


class _Clock:
    """time.monotonic desplazable, solo para el módulo (asyncio sigue con el real)."""

    def __init__(self):
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset


async def test_ttl_ok_24_horas(apify_on, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(apify_1688, "time", clock)
    fake = FakeApify("ok").install(monkeypatch)

    await apify_1688.search_by_image(PHOTO)
    clock.offset = 24 * 3600 - 60
    assert (await apify_1688.search_by_image(PHOTO)).cached is True
    assert len(fake.starts) == 1
    clock.offset = 24 * 3600 + 1
    assert (await apify_1688.search_by_image(PHOTO)).cached is False
    assert len(fake.starts) == 2


async def test_timeout_no_se_cachea(apify_on, monkeypatch):
    """Los cuelgues son pasajeros (medido): el siguiente pedido vuelve a buscar."""
    fake = FakeApify("hang", "hang", "ok").install(monkeypatch)

    assert (await apify_1688.search_by_image(PHOTO, deadline_s=0.4)).status == "timeout"
    again = await apify_1688.search_by_image(PHOTO, deadline_s=0.4)
    await apify_1688.wait_pending_aborts()

    assert again.status == "ok" and again.cached is False
    assert len(fake.starts) == 3


async def test_over_budget_y_busy_no_se_cachean(apify_on, monkeypatch):
    monkeypatch.setattr(apify_1688, "_daily_budget", lambda: 0)
    fake = FakeApify("ok").install(monkeypatch)
    assert (await apify_1688.search_by_image(PHOTO)).status == "over_budget"

    # Se carga cupo: la misma foto ya se busca, sin esperar ningún TTL.
    monkeypatch.setattr(apify_1688, "_daily_budget", lambda: 5)
    assert (await apify_1688.search_by_image(PHOTO)).status == "ok"
    assert len(fake.starts) == 1


async def test_api_que_no_contesta_vuelve_al_deadline(apify_on, monkeypatch):
    """Ni siquiera el POST de lanzar contesta: igual se vuelve al deadline."""
    fake = FakeApify()

    async def never(request):
        fake.requests.append(request)
        await asyncio.sleep(60)

    fake.handler = never
    fake.install(monkeypatch)

    result = await asyncio.wait_for(apify_1688.search_by_image(PHOTO, deadline_s=0.7), timeout=3)

    assert result.status == "timeout"
    assert 0.65 <= result.elapsed_s < 1.0
    # Se lanzó la cobertura igual, y no hay run id que imputar ni abortar.
    assert result.run_ids == [] and result.usd_imputado == 0.0
    assert _used() == 2


async def test_gana_la_primera_aunque_termine_despues_de_la_cobertura(apify_on, monkeypatch):
    # 0,6 s de margen entre la cobertura (0,2 s) y el fin de la 1: con la máquina
    # cargada (xdist -n 16) 0,25 s no alcanzaba para que la 2 tuviera run id.
    fake = FakeApify("late", "hang", late_s=0.8).install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=5.0)
    await apify_1688.wait_pending_aborts()

    assert result.status == "ok" and result.hedged is True
    assert result.run_ids == ["run1", "run2"]
    assert len(result.candidates) == 2
    # La perdedora es la de cobertura.
    assert fake.aborted == ["run2"]
    assert 0.75 <= result.elapsed_s < 3.0


async def test_nunca_mas_de_dos_corridas(apify_on, monkeypatch):
    fake = FakeApify("fail").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=2.0)

    assert result.status == "error"
    assert len(fake.starts) == 2 and _used() == 2


async def test_cobertura_sin_ventana_no_se_lanza(apify_on, monkeypatch):
    """Si al hedge le quedan menos de _MIN_RUN_WINDOW_S hasta el deadline, no se paga."""
    monkeypatch.setattr(apify_1688, "_MIN_RUN_WINDOW_S", 8.0)
    fake = FakeApify("hang").install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=0.5)
    await apify_1688.wait_pending_aborts()

    assert result.status == "timeout" and result.hedged is False
    assert len(fake.starts) == 1 and _used() == 1


async def test_dataset_de_2mb_justos_se_descarta_con_el_tope_real(apify_on, monkeypatch):
    raw = b"[" + b" " * (2 * 1024 * 1024) + b"]"
    fake = FakeApify("ok", raw_items=raw).install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "error" and len(fake.starts) == 1


async def test_respuesta_gigante_al_lanzar_no_rompe(apify_on, monkeypatch):
    monkeypatch.setattr(apify_1688, "_MAX_RESPONSE_BYTES", 100)
    fake = FakeApify()

    async def big(request):
        fake.requests.append(request)
        if request.url.path.endswith("/runs"):
            fake.starts.append(time.monotonic())
        return httpx.Response(201, content=b'{"data": {"id": "' + b"x" * 500 + b'"}}')

    fake.handler = big
    fake.install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO)

    assert result.status == "error" and result.run_ids == []


async def test_token_no_sale_en_logs_si_el_abort_falla(apify_on, monkeypatch, caplog):
    fake = FakeApify("hang")
    real = fake.handler

    async def abort_roto(request):
        if request.url.path.endswith("/abort"):
            raise httpx.ConnectError(f"no pude conectar con {TOKEN}")
        return await real(request)

    fake.handler = abort_roto
    fake.install(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        result = await apify_1688.search_by_image(PHOTO, deadline_s=0.4)
        await apify_1688.wait_pending_aborts()

    assert result.status == "timeout"
    assert "no se pudo abortar" in caplog.text and "ConnectError" in caplog.text
    assert TOKEN not in caplog.text


async def test_misma_foto_en_paralelo_paga_una_sola_corrida(apify_on, monkeypatch):
    """Cliente impaciente que reenvía mientras espera ~30 s: los reenvíos se suman
    a la búsqueda del primero en vez de reservar cupo y lanzar otra corrida."""
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("late", late_s=0.2).install(monkeypatch)

    results = await asyncio.gather(*(apify_1688.search_by_image(PHOTO, deadline_s=3.0)
                                     for _ in range(3)))

    assert [r.status for r in results] == ["ok"] * 3
    assert len(fake.starts) == 1 and _used() == 1


# ─── Observaciones de QA (20-oct) ──────────────────────────────────


async def test_puerto_443_explicito_es_la_misma_clave(apify_on, monkeypatch):
    fake = FakeApify("ok").install(monkeypatch)

    await apify_1688.search_by_image("https://Storage.example-bucket.com:443/fotos/a.jpg")
    again = await apify_1688.search_by_image("https://storage.example-bucket.com/fotos/a.jpg")

    assert again.cached is True and len(fake.starts) == 1
    # A Apify va la forma normalizada.
    assert json.loads(fake.requests[0].content)["imageUrls"] == [
        "https://storage.example-bucket.com/fotos/a.jpg"]


async def test_no_se_cubre_mientras_se_lee_el_dataset(apify_on, monkeypatch):
    """La 1 ya terminó SUCCEEDED pero el dataset tarda: pasado el hedge no se paga otra."""
    fake = FakeApify("ok")
    real = fake.handler

    async def dataset_lento(request):
        if "/datasets/" in request.url.path:
            await asyncio.sleep(0.4)
        return await real(request)

    fake.handler = dataset_lento
    fake.install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=3.0)

    assert result.status == "ok" and result.hedged is False
    assert len(fake.starts) == 1 and _used() == 1
    assert result.elapsed_s >= 0.35


# ─── Revisión de seguridad B3 ──────────────────────────────────────


async def test_post_de_lanzamiento_con_timeout_de_la_ventana(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("ok").install(monkeypatch)

    await apify_1688.search_by_image(PHOTO, deadline_s=0.5)

    start = fake.requests[0]
    assert start.url.path.endswith("/runs")
    timeouts = start.extensions["timeout"]
    assert all(0 < v <= 0.5 for v in timeouts.values())
    # El poll y el dataset siguen con el timeout del cliente.
    assert "/runs" not in fake.requests[1].url.path


async def test_post_de_lanzamiento_colgado_corta_en_la_ventana(apify_on, monkeypatch):
    """Aunque el transporte ignore el timeout de httpx, la ventana corta igual."""
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify()

    async def never(request):
        fake.requests.append(request)
        await asyncio.sleep(60)

    fake.handler = never
    fake.install(monkeypatch)

    run = apify_1688._Run(n=1)
    http = apify_1688._new_client(5)
    try:
        t0 = time.monotonic()
        kind, _ = await apify_1688._drive(http, run, ACTOR, "", PHOTO, {},
                                          time.monotonic() + 0.3)
    finally:
        await http.aclose()

    # "late", no "failed": al orquestador le dice que fue el deadline (timeout).
    assert kind == "late" and run.run_id == ""
    assert time.monotonic() - t0 < 0.8


async def test_shutdown_espera_los_aborts(apify_on, monkeypatch):
    from app import main as main_mod

    fake = FakeApify("hang")
    real = fake.handler

    async def abort_lento(request):
        if request.url.path.endswith("/abort"):
            await asyncio.sleep(0.2)
        return await real(request)

    fake.handler = abort_lento
    fake.install(monkeypatch)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=0.3)
    assert result.status == "timeout" and fake.aborted == []

    await main_mod._drain_apify_aborts()

    assert sorted(fake.aborted) == ["run1", "run2"]
    assert not apify_1688._background


async def test_shutdown_no_revienta_si_falla_la_espera(monkeypatch, caplog):
    from app import main as main_mod

    async def roto():
        raise RuntimeError("x")

    monkeypatch.setattr(apify_1688, "wait_pending_aborts", roto)
    with caplog.at_level(logging.WARNING):
        await main_mod._drain_apify_aborts()
    assert "RuntimeError" in caplog.text


async def test_lifespan_drena_los_aborts_al_apagar(monkeypatch):
    from fastapi import FastAPI

    from app import leader
    from app import main as main_mod

    calls: list[str] = []

    async def fake_drain():
        calls.append("drain")

    for name in ("_configure_logging", "_enforce_prod_secrets", "init_db", "_seed_stores"):
        monkeypatch.setattr(main_mod, name, lambda: None)
    monkeypatch.setattr(main_mod, "_drain_apify_aborts", fake_drain)
    monkeypatch.setattr(leader, "try_become_leader", lambda: False)
    # Los warm-ups corren en tareas aparte: que no hagan nada.
    from app.dedup import image_embed
    from app.ingest import browser_fetch
    from app.vendure import catalog as vendure_catalog

    async def no_catalog():
        return None

    monkeypatch.setattr(vendure_catalog, "get_catalog", no_catalog)
    monkeypatch.setattr(image_embed, "available", lambda: False)
    monkeypatch.setattr(browser_fetch, "available", lambda: False)

    async with main_mod.lifespan(FastAPI()):
        assert calls == []
    assert calls == ["drain"]


# ─── Pedidos en vuelo (single-flight) ──────────────────────────────


async def test_los_que_se_suman_no_pagan_y_tienen_copia_propia(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("late", late_s=0.2).install(monkeypatch)

    first, second = await asyncio.gather(
        apify_1688.search_by_image(PHOTO, deadline_s=3.0),
        apify_1688.search_by_image(PHOTO, deadline_s=3.0))

    assert first.shared is False and first.usd_imputado == pytest.approx(0.021)
    assert second.shared is True and second.usd_imputado == 0.0
    assert second.run_ids == first.run_ids == ["run1"]
    assert second.candidates is not first.candidates
    assert len(fake.starts) == 1
    assert apify_1688._inflight == {}


async def test_el_que_se_suma_vence_en_su_deadline_sin_cancelar_la_compartida(
        apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("late", late_s=0.5).install(monkeypatch)

    leader = asyncio.create_task(apify_1688.search_by_image(PHOTO, deadline_s=3.0))
    await asyncio.sleep(0.05)
    joiner = await apify_1688.search_by_image(PHOTO, deadline_s=0.2)

    assert joiner.status == "timeout" and joiner.shared is True
    assert joiner.elapsed_s < 0.4
    result = await leader
    assert result.status == "ok"
    assert len(fake.starts) == 1 and fake.aborted == [] and _used() == 1


async def test_primer_llamador_cancelado_no_corta_la_compartida(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("late", late_s=0.3).install(monkeypatch)

    leader = asyncio.create_task(apify_1688.search_by_image(PHOTO, deadline_s=3.0))
    await asyncio.sleep(0.05)
    joiner = asyncio.create_task(apify_1688.search_by_image(PHOTO, deadline_s=3.0))
    await asyncio.sleep(0.05)
    leader.cancel()

    result = await joiner
    assert result.status == "ok" and result.shared is True
    assert len(fake.starts) == 1
    # Lo que se pagó quedó en la caché.
    assert (await apify_1688.search_by_image(PHOTO)).cached is True


async def test_otra_foto_en_paralelo_no_se_suma(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    fake = FakeApify("late", late_s=0.1).install(monkeypatch)

    a, b = await asyncio.gather(
        apify_1688.search_by_image(PHOTO, deadline_s=3.0),
        apify_1688.search_by_image(PHOTO.replace("cliente", "otra"), deadline_s=3.0))

    assert a.shared is False and b.shared is False
    assert len(fake.starts) == 2


async def test_lanzamiento_que_vence_antes_que_el_loop_igual_es_timeout(apify_on, monkeypatch):
    """Carrera: el POST de lanzamiento vence en el deadline un pelo antes de que
    el loop lo note. Tiene que salir timeout, no error."""
    async def late(*args, **kwargs):
        return "late", []

    monkeypatch.setattr(apify_1688, "_drive", late)
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)

    result = await apify_1688.search_by_image(PHOTO, deadline_s=1.0)

    assert result.status == "timeout"


async def test_deadline_corto_no_envenena_al_que_se_suma_ni_a_la_cache(apify_on, monkeypatch):
    """Un pedido con poco tiempo (p. ej. el resto del presupuesto de un request)
    abre la búsqueda; otro con la misma foto y 5 s se suma. Cuando la compartida
    vence, el segundo busca de nuevo con lo que le queda, y el timeout no queda
    en la caché."""
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    FakeApify("late", late_s=0.6).install(monkeypatch)

    leader = asyncio.create_task(apify_1688.search_by_image(PHOTO, deadline_s=0.3))
    await asyncio.sleep(0.02)
    joiner = await apify_1688.search_by_image(PHOTO, deadline_s=5.0)
    await leader
    await apify_1688.wait_pending_aborts()
    later = await apify_1688.search_by_image(PHOTO, deadline_s=5.0)

    assert joiner.status == "ok"
    assert later.status == "ok"


async def test_al_que_se_suma_sin_ventana_le_queda_el_timeout(apify_on, monkeypatch):
    monkeypatch.setattr(apify_on, "apify_1688_hedge_after_s", 10.0, raising=False)
    monkeypatch.setattr(apify_1688, "_MIN_RUN_WINDOW_S", 8.0)
    fake = FakeApify("hang").install(monkeypatch)

    leader = asyncio.create_task(apify_1688.search_by_image(PHOTO, deadline_s=0.3))
    await asyncio.sleep(0.02)
    joiner = await apify_1688.search_by_image(PHOTO, deadline_s=1.0)
    await leader
    await apify_1688.wait_pending_aborts()

    assert joiner.status == "timeout" and joiner.shared is True
    assert len(fake.starts) == 1 and _used() == 1


async def test_si_la_compartida_revienta_el_que_se_suma_sale_shared(apify_on, monkeypatch):
    gate = asyncio.Event()

    async def roto(*args, **kwargs):
        await gate.wait()
        raise RuntimeError("x")

    monkeypatch.setattr(apify_1688, "_search_paid", roto)

    leader = asyncio.create_task(apify_1688.search_by_image(PHOTO, deadline_s=2.0))
    await asyncio.sleep(0.02)
    joiner = asyncio.create_task(apify_1688.search_by_image(PHOTO, deadline_s=2.0))
    await asyncio.sleep(0.02)
    gate.set()

    assert (await joiner).status == "error" and (await joiner).shared is True
    lead = await leader
    assert lead.status == "error" and lead.shared is False
