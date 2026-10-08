"""Filtro "mismo producto" (CLIP + nombre) y juez LLM de la banda ambigua.

El juez nunca habla con un proveedor real: el cliente OpenAI-compatible es un
doble que devuelve el texto que el test quiere.
"""

from __future__ import annotations

import os
from io import BytesIO
from types import SimpleNamespace

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.models import Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app import net_guard  # noqa: E402
from app.pricing import daily_budget, judge_images, market_judge, market_match  # noqa: E402
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
    def __init__(self, text="", exc: Exception | None = None, usage=(1200, 80), reasoning=None):
        self.calls: list[dict] = []
        self.closed = 0
        self._text, self._exc, self._usage, self._reasoning = text, exc, usage, reasoning
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def close(self):
        self.closed += 1

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._text))],
            usage=SimpleNamespace(
                prompt_tokens=self._usage[0], completion_tokens=self._usage[1],
                completion_tokens_details=(None if self._reasoning is None
                                           else SimpleNamespace(reasoning_tokens=self._reasoning)),
            ),
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


# ─── cliente y transporte (security L3, L4) ────────────────────────────────


def test_client_never_retries_on_its_own(monkeypatch):
    _judge_settings(monkeypatch, pm_llm_timeout_s=12.0)
    client = market_judge.make_client()
    assert client.max_retries == 0
    assert client.timeout == 12.0


@pytest.mark.parametrize("url", ["http://llm.invalid/v1", "ftp://llm.invalid", "llm.invalid/v1", "https://"])
async def test_non_https_base_url_turns_the_judge_off(monkeypatch, caplog, url):
    _judge_settings(monkeypatch, pm_llm_base_url=url)
    monkeypatch.setattr(market_judge, "_warned_insecure_url", False)
    client = FakeClient(GOOD)
    assert market_judge.enabled() is False
    assert await market_judge.judge("x", [], CANDS, max_calls=5, client=client) is None
    assert client.calls == []
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0


def test_insecure_url_warns_once(monkeypatch, caplog):
    import logging

    _judge_settings(monkeypatch, pm_llm_base_url="http://llm.invalid/v1")
    monkeypatch.setattr(market_judge, "_warned_insecure_url", False)
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        for _ in range(3):
            market_judge.enabled()
    assert caplog.text.count("no es una URL https") == 1


async def test_a_client_created_by_the_judge_is_closed(monkeypatch):
    _judge_settings(monkeypatch)
    created: list[FakeClient] = []

    def factory():
        created.append(FakeClient(exc=TimeoutError("30s")))
        return created[-1]

    monkeypatch.setattr(market_judge, "make_client", factory)
    assert await market_judge.judge("x", [], CANDS, max_calls=5) is None
    assert len(created) == 1 and created[0].closed == 1


async def test_a_failed_call_still_counts_against_the_cap_and_runs_the_hook(monkeypatch):
    _judge_settings(monkeypatch)
    hooked = []
    client = FakeClient(exc=TimeoutError("30s"))
    assert await market_judge.judge("x", [], CANDS, max_calls=5, client=client,
                                    on_reserve=lambda session: hooked.append(1)) is None
    assert hooked == [1] and daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 1
    assert client.closed == 0  # el cliente es del llamador: no lo cierra el juez


# ─── proveedor: pensamiento apagado por host (extra_body) ─────────────────


@pytest.fixture
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(market_judge, "_warned_once", set())


MIMO_URL = "https://api.xiaomimimo.com/v1"


@pytest.mark.parametrize("base_url,expected", [
    (MIMO_URL, {"thinking": {"type": "disabled"}}),
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", {"enable_thinking": False}),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", {"enable_thinking": False}),
    ("https://ws-123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", {"enable_thinking": False}),
    ("https://api.qwencloudapi.com/v1", {"enable_thinking": False}),
    ("https://openrouter.ai/api/v1", {}),
    ("https://llm.invalid/v1", {}),
    ("https://evil-dashscope.example.com/v1", {}),       # "dashscope" en otro dominio no cuenta
    ("https://api.xiaomimimo.com.evil.com/v1", {}),
])
def test_extra_body_by_provider_host(monkeypatch, base_url, expected):
    _judge_settings(monkeypatch, pm_llm_base_url=base_url)
    assert market_judge.extra_body() == expected


@pytest.mark.parametrize("raw,expected", [
    ("{}", {}),                                                    # apagar el default
    ('{"reasoning": {"enabled": false}}', {"reasoning": {"enabled": False}}),
    ('  {"thinking": {"type": "enabled"}}  ', {"thinking": {"type": "enabled"}}),
])
def test_extra_body_env_override_replaces_the_default(monkeypatch, raw, expected):
    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL, pm_llm_extra_body=raw)
    assert market_judge.extra_body() == expected


@pytest.mark.parametrize("raw", ["{roto", "[1, 2]", '"texto"', "42", "null"])
def test_invalid_extra_body_warns_once_and_falls_back_to_the_default(monkeypatch, caplog, fresh_warnings, raw):
    import logging

    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL, pm_llm_extra_body=raw)
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        for _ in range(3):
            assert market_judge.extra_body() == {"thinking": {"type": "disabled"}}
    assert caplog.text.count("PM_LLM_EXTRA_BODY no es un objeto JSON") == 1


ALLOWED_EXTRA = {
    "thinking": {"type": "disabled"}, "enable_thinking": False, "thinking_budget": 0,
    "reasoning": {"enabled": False}, "reasoning_effort": "none", "top_p": 0.5, "seed": 7,
    "response_format": {"type": "json_object"},
}


def test_extra_body_allowlist_is_exactly_the_documented_one():
    assert market_judge._ALLOWED_BODY_KEYS == set(ALLOWED_EXTRA)


def test_extra_body_lets_every_allowed_key_through(monkeypatch):
    import json

    _judge_settings(monkeypatch, pm_llm_extra_body=json.dumps(ALLOWED_EXTRA))
    assert market_judge.extra_body() == ALLOWED_EXTRA


DROPPED = ["model", "messages", "max_tokens", "max_completion_tokens", "temperature", "stream", "n",
           "tools", "tool_choice", "user", "stop", "logit_bias", "metadata", "store", "base_url",
           "extra_headers", "api_key"]


def test_extra_body_drops_everything_that_is_not_allowed_and_warns_once_per_key(monkeypatch, caplog,
                                                                              fresh_warnings):
    """Lista blanca: el aviso nombra la clave y nunca el valor."""
    import json
    import logging

    secret = "valor-que-no-debe-loguearse"
    raw = json.dumps({**{k: secret for k in DROPPED}, "top_p": 0.5, "seed": 3})
    _judge_settings(monkeypatch, pm_llm_extra_body=raw)
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        for _ in range(3):
            assert market_judge.extra_body() == {"top_p": 0.5, "seed": 3}
    for key in DROPPED:
        assert caplog.text.count(f"la clave '{key}' no está permitida") == 1, key
    assert secret not in caplog.text and "0.5" not in caplog.text
    assert len(caplog.records) == len(DROPPED)


def test_extra_body_warns_about_a_dropped_key_only_once_across_changes(monkeypatch, caplog, fresh_warnings):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        _judge_settings(monkeypatch, pm_llm_extra_body='{"max_tokens": 1}')
        market_judge.extra_body()
        _judge_settings(monkeypatch, pm_llm_extra_body='{"max_tokens": 2, "stream": true}')
        market_judge.extra_body()
    assert caplog.text.count("'max_tokens'") == 1 and caplog.text.count("'stream'") == 1


def test_a_very_long_dropped_key_is_truncated_in_the_log(monkeypatch, caplog, fresh_warnings):
    import json
    import logging

    _judge_settings(monkeypatch, pm_llm_extra_body=json.dumps({"k" * 500: 1}))
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        assert market_judge.extra_body() == {}
    assert "k" * 41 not in caplog.text and "k" * 40 in caplog.text


def test_the_readme_documents_the_allowed_extra_body_keys():
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    for key in ALLOWED_EXTRA:
        assert f"`{key}`" in readme, key


async def test_judge_sends_the_provider_extra_body(monkeypatch):
    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL, pm_llm_model="mimo-v2.6-flash",
                    pm_llm_image_mode="url")
    client = FakeClient(GOOD)
    await market_judge.judge("x", [], CANDS, max_calls=5, client=client)
    assert client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert client.calls[0]["max_tokens"] == market_judge._MAX_TOKENS


async def test_judge_sends_no_extra_body_when_there_is_nothing_to_send(monkeypatch):
    _judge_settings(monkeypatch, pm_llm_base_url="https://openrouter.ai/api/v1")
    client = FakeClient(GOOD)
    await market_judge.judge("x", [], CANDS, max_calls=5, client=client)
    assert "extra_body" not in client.calls[0]


# ─── respuesta de un modelo que piensa = sin veredicto ────────────────────


@pytest.mark.parametrize("text,reasoning", [
    ("", 700),            # MiMo con el pensamiento prendido: todo max_tokens en reasoning
    ("", None),           # content vacío sin detalle de tokens
    ("   \n ", 0),       # solo espacios
    (GOOD, 350),          # razonó y encima contestó: igual no es lo que costeamos
])
async def test_thinking_or_empty_answer_is_no_verdict_but_counts_tokens(
        monkeypatch, caplog, fresh_warnings, text, reasoning):
    import logging

    _judge_settings(monkeypatch)
    client = FakeClient(text, usage=(900, 700), reasoning=reasoning)
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        res = await market_judge.judge("x", [], CANDS, max_calls=5, client=client)
    assert res is not None and res.verdicts == {}
    assert (res.input_tokens, res.output_tokens) == (900, 700)
    assert "el modelo está pensando, revisá PM_LLM_EXTRA_BODY" in caplog.text


async def test_thinking_warning_is_logged_once(monkeypatch, caplog, fresh_warnings):
    import logging

    _judge_settings(monkeypatch)
    client = FakeClient("", reasoning=700)
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        for _ in range(3):
            await market_judge.judge("x", [], CANDS, max_calls=5, client=client)
    assert caplog.text.count("el modelo está pensando") == 1


async def test_zero_reasoning_tokens_keeps_the_verdicts(monkeypatch):
    _judge_settings(monkeypatch)
    client = FakeClient(GOOD, reasoning=0)
    res = await market_judge.judge("x", [], CANDS, max_calls=5, client=client)
    assert set(res.verdicts) == {"MLA1"}


@pytest.mark.parametrize("usage,expected", [
    (None, 0),
    (SimpleNamespace(), 0),
    (SimpleNamespace(completion_tokens_details=None), 0),
    (SimpleNamespace(completion_tokens_details={"reasoning_tokens": 12}), 12),
    (SimpleNamespace(completion_tokens_details=SimpleNamespace(reasoning_tokens="7")), 7),
    (SimpleNamespace(completion_tokens_details=SimpleNamespace(reasoning_tokens="x")), 0),
])
def test_reasoning_tokens_reader(usage, expected):
    assert market_judge._reasoning_tokens(usage) == expected


# ─── modo de imágenes: url vs base64 ───────────────────────────────────────


@pytest.mark.parametrize("base_url,raw,expected", [
    (MIMO_URL, "", "base64"),                          # MiMo no baja URLs remotas
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "", "url"),
    ("https://openrouter.ai/api/v1", "", "url"),
    (MIMO_URL, "url", "url"),                          # el env manda
    ("https://openrouter.ai/api/v1", " BASE64 ", "base64"),
])
def test_image_mode(monkeypatch, base_url, raw, expected):
    _judge_settings(monkeypatch, pm_llm_base_url=base_url, pm_llm_image_mode=raw)
    assert market_judge.image_mode() == expected


def test_invalid_image_mode_warns_once_and_uses_the_default(monkeypatch, caplog, fresh_warnings):
    import logging

    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL, pm_llm_image_mode="inline")
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        assert [market_judge.image_mode() for _ in range(3)] == ["base64"] * 3
    assert caplog.text.count("PM_LLM_IMAGE_MODE") == 1


@pytest.mark.parametrize("raw", ["inline", "sk-1234567890abcdef-esto-es-una-key", "Base 64"])
def test_invalid_image_mode_warning_does_not_repeat_the_value(monkeypatch, caplog, fresh_warnings, raw):
    """Una variable mal pegada puede traer una credencial: el aviso dice a lo
    sumo el largo."""
    import logging

    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL, pm_llm_image_mode=raw)
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        market_judge.image_mode()
    assert raw.strip().lower()[:6] not in caplog.text.lower().replace("pm_llm_image_mode", "")
    assert f"({len(raw.strip())} caracteres)" in caplog.text


OUR_PHOTO = "https://example.invalid/assets/preview/p1__preview.jpg"   # host de VENDURE_API_URL
ML_PHOTO = "https://http2.mlstatic.com/D_1.jpg"
B64_CANDS = [JudgeCandidate("MLA1", "Org A", ML_PHOTO),
             JudgeCandidate("MLA2", "Org B", "https://http2.mlstatic.com/D_rota.jpg")]


@pytest.fixture
def photos(monkeypatch):
    """Fotos servidas por un MockTransport: {url: bytes}. Las que no están dan 404."""
    buf = BytesIO()
    Image.new("RGB", (900, 600), (10, 120, 200)).save(buf, format="JPEG")
    served = {OUR_PHOTO: buf.getvalue(), ML_PHOTO: buf.getvalue()}
    requested: list[str] = []

    def handler(request):
        requested.append(str(request.url))
        body = served.get(str(request.url))
        if body is None:
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"}, request=request)

    monkeypatch.setattr(net_guard, "assert_public_url", lambda url: None)

    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)
    monkeypatch.setattr(judge_images, "make_http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return SimpleNamespace(served=served, requested=requested)


def _sent_images(call) -> list[str]:
    return [p["image_url"]["url"] for p in call["messages"][1]["content"] if p["type"] == "image_url"]


async def test_base64_mode_sends_inline_photos_and_skips_the_broken_one(monkeypatch, photos):
    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL, pm_llm_model="mimo-v2.6-flash")
    client = FakeClient(GOOD)
    res = await market_judge.judge("Organizador", [OUR_PHOTO], B64_CANDS, max_calls=5, client=client)
    assert set(res.verdicts) == {"MLA1"}
    sent = _sent_images(client.calls[0])
    assert len(sent) == 2 and all(u.startswith("data:image/jpeg;base64,") for u in sent)
    texts = " ".join(p.get("text", "") for p in client.calls[0]["messages"][1]["content"])
    assert "MLA2" in texts                         # la ficha va igual, sin su foto
    assert OUR_PHOTO not in str(client.calls[0])   # ninguna URL viaja al proveedor


async def test_base64_mode_without_any_of_our_photos_is_no_verdict_and_spends_nothing(monkeypatch, photos):
    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL)
    photos.served.pop(OUR_PHOTO)
    client = FakeClient(GOOD)
    hooked = []
    assert await market_judge.judge("x", [OUR_PHOTO], B64_CANDS, max_calls=5, client=client,
                                    on_reserve=lambda s: hooked.append(1)) is None
    assert client.calls == [] and hooked == []
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0


async def test_base64_mode_our_photo_from_a_foreign_host_is_not_downloaded(monkeypatch, photos):
    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL)
    client = FakeClient(GOOD)
    assert await market_judge.judge("x", ["https://cdn.evil.com/p1.jpg"], B64_CANDS,
                                    max_calls=5, client=client) is None
    assert "https://cdn.evil.com/p1.jpg" not in photos.requested and client.calls == []


async def test_base64_mode_without_quota_downloads_nothing(monkeypatch, photos):
    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL)
    client = FakeClient(GOOD)
    assert await market_judge.judge("x", [OUR_PHOTO], B64_CANDS, max_calls=1, client=client) is not None
    photos.requested.clear()
    assert await market_judge.judge("x", [OUR_PHOTO], B64_CANDS, max_calls=1, client=client) is None
    assert photos.requested == [] and len(client.calls) == 1


async def test_url_mode_never_downloads(monkeypatch, photos):
    _judge_settings(monkeypatch, pm_llm_base_url=MIMO_URL, pm_llm_image_mode="url")
    client = FakeClient(GOOD)
    await market_judge.judge("x", [OUR_PHOTO], B64_CANDS, max_calls=5, client=client)
    assert photos.requested == []
    assert _sent_images(client.calls[0])[0] == OUR_PHOTO
