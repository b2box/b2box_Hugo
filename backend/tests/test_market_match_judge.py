"""Filtro "mismo producto" (CLIP + nombre) y juez LLM de la banda ambigua.

El juez nunca habla con un proveedor real: el cliente OpenAI-compatible es un
doble que devuelve el texto que el test quiere.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".sqlite3")
os.close(_DB_FD)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_PATH}")

import pytest  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.models import Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import market_judge, market_match  # noqa: E402
from app.pricing.market_judge import JudgeCandidate  # noqa: E402
from app.pricing.market_match import AMBIGUOUS, MATCH, NO, Thresholds  # noqa: E402
from app.pricing.market_ml import MlCandidate  # noqa: E402
from app.vendure.client import VendureProduct  # noqa: E402

THR = Thresholds(image=0.65, name=0.60, image_strong=0.80, image_veto=0.40, name_veto=0.30)


@pytest.fixture(autouse=True)
def _clean_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        s.commit()
    yield


def _product(name="Organizador de cocina 3 niveles") -> VendureProduct:
    return VendureProduct(
        id="P1", name=name, slug="p1", description="", enabled=True, source_url=None,
        image_urls=["https://cdn/p1.jpg"], product_code="BX001",
        featured_image_url="https://cdn/p1.jpg", first_variant_price_cents=1000, variant_count=1,
    )


# ─── classify: las bandas ───────────────────────────────────────────────────


@pytest.mark.parametrize("image,name,expected", [
    (None, 1.0, (NO, None)),                     # sin imagen no hay match
    (0.39, 1.0, (NO, None)),                     # veto de imagen
    (0.95, 0.29, (NO, None)),                    # veto de nombre gana a la imagen fuerte
    (0.80, 0.30, (MATCH, "clip")),               # imagen fuerte, nombre no vetado
    (0.65, 0.60, (MATCH, "clip+nombre")),        # justo en los dos umbrales
    (0.64, 0.99, (AMBIGUOUS, None)),             # imagen apenas abajo
    (0.70, 0.59, (AMBIGUOUS, None)),             # nombre apenas abajo
    (0.40, 0.30, (AMBIGUOUS, None)),             # justo en los vetos: no se veta
])
def test_classify_bands(image, name, expected):
    assert market_match.classify(image, name, THR) == expected


def test_search_query_drops_internal_codes():
    assert market_match.search_query("BX0123  Organizador   PA-45 cocina pa12b") == "Organizador cocina"


def test_fallback_query_only_when_there_is_something_shorter():
    assert market_match.fallback_query("Organizador cocina") is None
    assert market_match.fallback_query(
        "BX9 Organizador Doble Ajustable 3 Niveles 40x30 Blanco") == "Organizador Doble Ajustable 3"


async def test_score_candidates_ranks_by_name_and_only_scores_the_top():
    scored: list[str] = []

    async def scorer(our, urls):
        scored.append(urls[0])
        return {"a": 0.9, "b": 0.5, "c": 0.7}[urls[0]]

    cands = [
        MlCandidate(id="MLA-c", name="Organizador cocina", image_urls=["c"]),
        MlCandidate(id="MLA-x", name="Zapatillas running", image_urls=["x"]),
        MlCandidate(id="MLA-a", name="Organizador de cocina 3 niveles", image_urls=["a"]),
        MlCandidate(id="MLA-b", name="Organizador de cocina", image_urls=["b"]),
    ]
    decisions = await market_match.score_candidates(_product(), cands, THR, scorer=scorer, max_candidates=3)
    # token_set_ratio da 100 a los tres "Organizador…" (son subconjuntos del
    # nuestro); el de zapatillas queda último y no llega a gastar una descarga.
    assert {d.candidate.id for d in decisions} == {"MLA-a", "MLA-b", "MLA-c"}
    assert "x" not in scored
    by_id = {d.candidate.id: d for d in decisions}
    assert (by_id["MLA-a"].verdict, by_id["MLA-a"].source) == (MATCH, "clip")
    assert by_id["MLA-b"].verdict == AMBIGUOUS
    assert (by_id["MLA-c"].verdict, by_id["MLA-c"].source) == (MATCH, "clip+nombre")


async def test_candidate_without_photo_is_not_scored():
    async def scorer(our, urls):  # pragma: no cover - no debería llamarse
        raise AssertionError("no debería puntuar sin foto")

    decisions = await market_match.score_candidates(
        _product(), [MlCandidate(id="MLA1", name="Organizador de cocina")], THR, scorer=scorer)
    assert decisions[0].image_score is None and decisions[0].verdict == NO


async def test_default_scorer_returns_none_without_clip(monkeypatch):
    from app.dedup import image_embed

    monkeypatch.setattr(image_embed, "available", lambda: False)
    assert await market_match.clip_index_scorer(_product(), ["https://x/a.jpg"]) is None
    assert market_match.indexed(_product()) is False


# ─── juez: parseo tolerante ────────────────────────────────────────────────


def test_parse_strict_json():
    text = '{"results":[{"ml_id":"MLA1","same_product":true,"confidence":0.91,"reason":"igual"}]}'
    [v] = market_judge.parse_verdicts(text)
    assert (v.ml_id, v.same_product, v.confidence, v.reason) == ("MLA1", True, 0.91, "igual")


def test_parse_fenced_list_with_spanish_keys_and_percent_confidence():
    text = '```json\n[{"id":"MLA2","mismo":"sí","confianza":85,"motivo":"misma forma"}]\n```'
    [v] = market_judge.parse_verdicts(text)
    assert (v.ml_id, v.same_product, v.confidence) == ("MLA2", True, 0.85)


def test_parse_dict_keyed_by_ml_id_with_text_around():
    text = 'Claro: {"MLA3": {"same_product": "false", "confidence": "0.2"}} listo.'
    [v] = market_judge.parse_verdicts(text)
    assert (v.ml_id, v.same_product, v.confidence) == ("MLA3", False, 0.2)


@pytest.mark.parametrize("text", [
    "", "no sé", "{roto", '{"results": [{"ml_id": "MLA1"}]}', '{"results": "x"}', "[1, 2]",
])
def test_invalid_output_means_no_verdict(text):
    assert market_judge.parse_verdicts(text) is None


def test_cost_estimate():
    # 10.000 tokens de entrada a 0,20/M + 500 de salida a 1,60/M
    assert market_judge.estimate_cost(10_000, 500, 0.20, 1.60) == pytest.approx(0.0028)


def test_messages_carry_our_photos_and_each_candidate():
    msgs = market_judge.build_messages(
        "Organizador", ["https://cdn/1.jpg", "https://cdn/2.jpg", "https://cdn/3.jpg"],
        [JudgeCandidate("MLA1", "Org A", "https://ml/a.jpg", 150000),
         JudgeCandidate("MLA2", "Org B", None)],
    )
    parts = msgs[1]["content"]
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert urls == ["https://cdn/1.jpg", "https://cdn/2.jpg", "https://ml/a.jpg"]  # máx 2 nuestras
    assert any("MLA1" in p.get("text", "") and "1500" in p.get("text", "") for p in parts)


# ─── juez: cuándo llama y qué devuelve ─────────────────────────────────────


class FakeClient:
    def __init__(self, text="", exc: Exception | None = None, usage=(1200, 80)):
        self.calls: list[dict] = []
        self._text, self._exc, self._usage = text, exc, usage
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._text))],
            usage=SimpleNamespace(prompt_tokens=self._usage[0], completion_tokens=self._usage[1]),
        )


def _judge_settings(monkeypatch, **over):
    base = dict(vendure_api_url="https://example.invalid/admin-api",
                pm_llm_base_url="https://llm.invalid/v1", pm_llm_api_key="k",
                pm_llm_model="qwen3-vl-plus", pm_llm_price_in_per_m=0.20, pm_llm_price_out_per_m=1.60)
    base.update(over)
    monkeypatch.setattr(market_judge, "get_settings", lambda: Settings(**base))


CANDS = [JudgeCandidate("MLA1", "Org A", "https://ml/a.jpg"), JudgeCandidate("MLA2", "Org B", None)]
GOOD = ('{"results":[{"ml_id":"MLA1","same_product":true,"confidence":0.9,"reason":"igual"},'
        '{"ml_id":"MLA9","same_product":true,"confidence":0.9,"reason":"no lo pedimos"}]}')


async def test_judge_off_by_default_never_calls(monkeypatch):
    _judge_settings(monkeypatch)
    client = FakeClient(GOOD)
    assert await market_judge.judge("x", [], CANDS, max_calls=0, client=client) is None
    assert client.calls == []


async def test_judge_without_credentials_never_calls(monkeypatch):
    _judge_settings(monkeypatch, pm_llm_api_key="")
    client = FakeClient(GOOD)
    assert await market_judge.judge("x", [], CANDS, max_calls=10, client=client) is None
    assert client.calls == []


async def test_judge_returns_verdicts_tokens_and_cost(monkeypatch):
    _judge_settings(monkeypatch)
    client = FakeClient(GOOD, usage=(10_000, 500))
    res = await market_judge.judge("Organizador", ["https://cdn/1.jpg"], CANDS, max_calls=5, client=client)
    assert set(res.verdicts) == {"MLA1"}  # el MLA9 inventado se descarta
    assert (res.input_tokens, res.output_tokens) == (10_000, 500)
    assert res.cost_usd == pytest.approx(0.0028)
    assert client.calls[0]["model"] == "qwen3-vl-plus" and client.calls[0]["temperature"] == 0


async def test_judge_daily_cap_is_atomic(monkeypatch):
    _judge_settings(monkeypatch)
    client = FakeClient(GOOD)
    results = [await market_judge.judge("x", [], CANDS, max_calls=2, client=client) for _ in range(4)]
    assert [r is not None for r in results] == [True, True, False, False]
    assert len(client.calls) == 2


async def test_judge_failure_is_no_verdict_not_an_exception(monkeypatch):
    _judge_settings(monkeypatch)
    client = FakeClient(exc=TimeoutError("30s"))
    assert await market_judge.judge("x", [], CANDS, max_calls=5, client=client) is None


async def test_illegible_answer_keeps_the_tokens_but_has_no_verdicts(monkeypatch):
    _judge_settings(monkeypatch)
    client = FakeClient("no puedo ver las imágenes")
    res = await market_judge.judge("x", [], CANDS, max_calls=5, client=client)
    assert res.verdicts == {} and res.input_tokens == 1200


@pytest.mark.parametrize("over", [{"pm_llm_base_url": ""}, {"pm_llm_api_key": ""},
                                  {"pm_llm_base_url": "", "pm_llm_api_key": ""}])
async def test_judge_without_base_url_or_key_sends_nothing_and_spends_no_quota(monkeypatch, over):
    _judge_settings(monkeypatch, **over)
    client = FakeClient(GOOD)
    assert market_judge.enabled() is False
    assert await market_judge.judge("x", [], CANDS, max_calls=10, client=client) is None
    assert client.calls == []
    assert market_judge.daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0  # ni reserva cupo
