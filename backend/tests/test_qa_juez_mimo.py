"""QA de fix/juez-mimo: juez LLM con MiMo/Qwen (pensamiento apagado, fotos en
base64) visto desde afuera, con el SDK de OpenAI REAL y el semáforo entero.

Sin red: el SDK habla con un `httpx.MockTransport`, las fotos salen de otro
MockTransport y el chequeo DNS de net_guard se reemplaza. La config entra por
variables de entorno (PM_LLM_*), igual que en producción.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import httpx  # noqa: E402
import openai  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app import net_guard  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db.models import Setting  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, judge_images, market_judge, price_monitor  # noqa: E402
from app.pricing.market_judge import JudgeCandidate  # noqa: E402
from tests import test_price_monitor as tpm  # noqa: E402
from tests.test_price_monitor import world  # noqa: E402,F401  (fixture)

MIMO = "https://api.xiaomimimo.com/v1"
QWEN = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
OUR_HOST = "https://example.invalid"          # host de VENDURE_API_URL
GOOD = '{"results":[{"ml_id":"MLA8","same_product":true,"confidence":0.9,"reason":"igual"}]}'
BASE_KEYS = {"model", "messages", "temperature", "max_tokens"}


# ─── dobles ─────────────────────────────────────────────────────────────────


@pytest.fixture
def llm_env(monkeypatch):
    """PM_LLM_* por entorno (como en prod) y avisos "una vez" en cero."""
    for k in [k for k in os.environ if k.startswith("PM_LLM_")]:
        monkeypatch.delenv(k)
    monkeypatch.setattr(market_judge, "_warned_once", set())
    monkeypatch.setattr(market_judge, "_warned_insecure_url", False)
    get_settings.cache_clear()

    def set_env(**kv):
        for k, v in kv.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()

    yield set_env
    get_settings.cache_clear()


@pytest.fixture
def clean_budget():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.exec(select(Setting)).all():
            s.delete(row)
        s.commit()


def _completion(content, prompt=900, completion=80, reasoning=None) -> dict:
    usage = {"prompt_tokens": prompt, "completion_tokens": completion,
             "total_tokens": prompt + completion}
    if reasoning is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": usage}


class FakeProvider:
    """El proveedor del otro lado del SDK real: guarda el JSON de cada request."""

    def __init__(self, answer: dict | None = None):
        self.bodies: list[dict] = []
        self.answer = answer or _completion(GOOD)
        self.clients = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=self.answer)

    def client(self) -> openai.AsyncOpenAI:
        self.clients += 1
        return openai.AsyncOpenAI(
            base_url=get_settings().pm_llm_base_url or "https://llm.invalid/v1", api_key="k",
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


def _jpeg(size=(900, 600)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def photos(monkeypatch):
    """Fotos por MockTransport: todo https de mlstatic o del host de Vendure
    existe, salvo lo que esté en `missing`."""
    state = SimpleNamespace(requested=[], missing=set())
    body = _jpeg()

    def handler(request):
        url = str(request.url)
        state.requested.append(url)
        if url in state.missing:
            return httpx.Response(404, request=request)
        return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"}, request=request)

    monkeypatch.setattr(net_guard, "assert_public_url", lambda url: None)

    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)
    monkeypatch.setattr(judge_images, "make_http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return state


def _sent_images(body: dict) -> list[str]:
    return [p["image_url"]["url"] for p in body["messages"][1]["content"] if p["type"] == "image_url"]


def _ambiguous(world, pids=("8",), our_host="https://cdn.b2box"):  # noqa: F811
    """Productos en banda ambigua contra MLA8. `our_host` = de dónde salen
    nuestras fotos (en base64 solo se bajan las del host de Vendure)."""
    tpm._ambiguous_world(world)
    tpm.FakeVendure.products = [
        replace(tpm._product(pid, "Soporte celular auto"),
                featured_image_url=f"{our_host}/assets/preview/{pid}.jpg",
                image_urls=[f"{our_host}/assets/source/{pid}.jpg"])
        for pid in pids
    ]


# ─── 1. juez apagado o sin cupo = idéntico a antes ─────────────────────────


@pytest.mark.parametrize("env,cap", [
    ({}, None),                                                        # nada configurado
    ({}, 5),                                                           # cupo pero sin credenciales
    ({"PM_LLM_BASE_URL": MIMO, "PM_LLM_API_KEY": "k"}, 0),             # MiMo configurado, cupo 0
    ({"PM_LLM_BASE_URL": QWEN, "PM_LLM_API_KEY": "k", "PM_LLM_IMAGE_MODE": "base64",
      "PM_LLM_EXTRA_BODY": '{"x": 1}'}, 0),
    ({"PM_LLM_BASE_URL": MIMO}, 5),                                    # sin key
    ({"PM_LLM_BASE_URL": "http://api.xiaomimimo.com/v1", "PM_LLM_API_KEY": "k"}, 5),  # no https
])
async def test_c1_judge_off_or_without_quota_downloads_and_calls_nothing(world, monkeypatch, llm_env,  # noqa: F811
                                                                        env, cap):
    llm_env(**env)
    tpm.FakeVendure.products.append(tpm._product("8", "Soporte celular auto"))
    world.ml.search["Soporte celular auto"] = [tpm._candidate("MLA8", "Soporte celular para auto")]
    world.ml.items["MLA8"] = [tpm._listing("I8", "101", 300.0)]
    world.image_scores[tpm.ML_IMG.format("MLA8")] = 0.62
    if cap is not None:
        tpm._set("pm_vision_max_calls", cap)
    touched: list[str] = []

    async def _no_inline(urls):
        touched.append("inline_images")
        return {}

    def _no_client(*a, **kw):
        touched.append("AsyncOpenAI")
        raise AssertionError("cliente LLM creado con el juez apagado")

    monkeypatch.setattr(judge_images, "inline_images", _no_inline)
    monkeypatch.setattr(judge_images, "make_http_client", lambda: touched.append("http") or None)
    monkeypatch.setattr(openai, "AsyncOpenAI", _no_client)

    result = await price_monitor.run_price_monitor()

    assert touched == []
    assert result["counts"] == {"ok": 1, "no_data": 3, "failed": 1, "skipped": 1}
    [run] = tpm._runs()
    assert (run.llm_calls, run.llm_input_tokens, run.llm_output_tokens, run.llm_cost_usd) == (0, 0, 0, 0)
    s8 = tpm._snaps()["8"]
    assert (s8.ml_status, s8.ambiguous_count, s8.match_source) == ("no_data", 1, None)
    assert "items:MLA8" not in world.ml.calls
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0


# ─── 2. extra_body por proveedor, con el SDK real ──────────────────────────


@pytest.mark.parametrize("base_url,extra", [
    (MIMO, {"thinking": {"type": "disabled"}}),
    ("https://API.XiaomiMimo.com:443/v1", {"thinking": {"type": "disabled"}}),
    (QWEN, {"enable_thinking": False}),
    ("https://dashscope-us.aliyuncs.com/compatible-mode/v1", {"enable_thinking": False}),
    ("https://ws-ab12.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", {"enable_thinking": False}),
    ("https://qwencloudapi.com/v1", {"enable_thinking": False}),
    ("https://api.qwencloudapi.com/compatible-mode/v1", {"enable_thinking": False}),
    ("https://openrouter.ai/api/v1", {}),
    ("https://api.openai.com/v1", {}),
    ("https://oss.aliyuncs.com/v1", {}),                 # Alibaba pero no Model Studio
    ("https://maas.aliyuncs.com.evil.com/v1", {}),
    ("https://xqwencloudapi.com/v1", {}),
])
async def test_c2_the_wire_body_carries_the_provider_field(llm_env, clean_budget, base_url, extra):
    llm_env(PM_LLM_BASE_URL=base_url, PM_LLM_API_KEY="k", PM_LLM_IMAGE_MODE="url")
    provider = FakeProvider()
    res = await market_judge.judge("x", ["https://cdn/1.jpg"], [JudgeCandidate("MLA8", "t", None)],
                                   max_calls=5, client=provider.client())
    assert res is not None and set(res.verdicts) == {"MLA8"}
    [body] = provider.bodies
    assert set(body) == BASE_KEYS | set(extra)
    assert {k: body[k] for k in extra} == extra
    assert body["max_tokens"] == market_judge._MAX_TOKENS and body["temperature"] == 0


async def test_c2_env_override_replaces_the_default_but_not_the_protected_fields(llm_env, clean_budget, caplog):
    llm_env(PM_LLM_BASE_URL=MIMO, PM_LLM_API_KEY="k", PM_LLM_MODEL="mimo-v2.6-flash", PM_LLM_IMAGE_MODE="url",
            PM_LLM_EXTRA_BODY=json.dumps({
                "model": "otro", "messages": [], "max_tokens": 99999, "max_completion_tokens": 99999,
                "temperature": 1.5, "stream": True, "n": 4, "enable_thinking": True, "top_p": 0.5}))
    provider = FakeProvider()
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        for _ in range(2):
            await market_judge.judge("x", [], [JudgeCandidate("MLA8", "t", None)], max_calls=5,
                                     client=provider.client())
    for body in provider.bodies:
        assert set(body) == BASE_KEYS | {"enable_thinking", "top_p"}   # sin "thinking": el default se reemplaza
        assert (body["model"], body["max_tokens"], body["temperature"]) == ("mimo-v2.6-flash", 700, 0)
        assert body["enable_thinking"] is True and len(body["messages"]) == 2
    assert caplog.text.count("claves que maneja el juez") == 1


@pytest.mark.parametrize("raw", ["{roto", "[]", "true", '{"a": 1', "  "])
async def test_c2_invalid_override_warns_once_and_sends_the_default(llm_env, clean_budget, caplog, raw):
    llm_env(PM_LLM_BASE_URL=QWEN, PM_LLM_API_KEY="k", PM_LLM_EXTRA_BODY=raw)
    provider = FakeProvider()
    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        for _ in range(3):
            await market_judge.judge("x", [], [JudgeCandidate("MLA8", "t", None)], max_calls=5,
                                     client=provider.client())
    assert [b.get("enable_thinking") for b in provider.bodies] == [False] * 3
    expected_warnings = 0 if not raw.strip() else 1        # vacío/espacios = default, sin aviso
    assert caplog.text.count("PM_LLM_EXTRA_BODY no es un objeto JSON") == expected_warnings


# ─── 3. modelo pensando = sin veredicto, tokens a la corrida, un aviso ─────


@pytest.mark.parametrize("content,reasoning", [
    ("", 700),        # MiMo con thinking prendido
    (None, 700),      # content null (MiMo manda reasoning_content aparte)
    (GOOD, 350),      # pensó y contestó: igual sin veredicto
    ("", None),       # vacío sin detalle de tokens
])
async def test_c3_thinking_answers_count_tokens_in_the_run_and_warn_once(world, monkeypatch, llm_env, caplog,  # noqa: F811
                                                                        content, reasoning):
    llm_env(PM_LLM_BASE_URL=QWEN, PM_LLM_API_KEY="k", PM_LLM_PRICE_IN_PER_M="0.14",
            PM_LLM_PRICE_OUT_PER_M="0.28")
    _ambiguous(world, pids=("8", "10"))
    tpm._set("pm_vision_max_calls", 5)
    provider = FakeProvider(_completion(content, prompt=900, completion=700, reasoning=reasoning))
    monkeypatch.setattr(market_judge, "make_client", provider.client)

    with caplog.at_level(logging.WARNING, logger="app.pricing.market_judge"):
        await price_monitor.run_price_monitor()

    assert len(provider.bodies) == 2 and all(b["enable_thinking"] is False for b in provider.bodies)
    snaps = tpm._snaps()
    assert {pid: (s.ml_status, s.match_source) for pid, s in snaps.items()} == {
        "8": ("no_data", None), "10": ("no_data", None)}
    [run] = tpm._runs()
    assert (run.llm_calls, run.llm_input_tokens, run.llm_output_tokens) == (2, 1_800, 1_400)
    assert run.llm_cost_usd == pytest.approx(2 * (900 * 0.14 + 700 * 0.28) / 1e6, abs=1e-9)
    assert caplog.text.count("el modelo está pensando, revisá PM_LLM_EXTRA_BODY") == 1
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 2


# ─── 4. base64: fotos antes del cupo, una llamada = 1 de cupo ──────────────


B64_CANDS = [JudgeCandidate("MLA8", "t", "https://http2.mlstatic.com/D_8.jpg"),
             JudgeCandidate("MLA9", "t", "https://http2.mlstatic.com/D_9.jpg")]
OURS = [f"{OUR_HOST}/assets/preview/1.jpg", f"{OUR_HOST}/assets/source/1.jpg"]


async def test_c4_photos_are_downloaded_before_the_quota_is_reserved(llm_env, clean_budget, photos, monkeypatch):
    llm_env(PM_LLM_BASE_URL=MIMO, PM_LLM_API_KEY="k")
    events: list[str] = []
    real_inline, real_reserve = judge_images.inline_images, daily_budget.reserve_async

    async def inline(urls):
        events.append("download")
        return await real_inline(urls)

    async def reserve(*a, **kw):
        events.append("reserve")
        return await real_reserve(*a, **kw)

    monkeypatch.setattr(judge_images, "inline_images", inline)
    monkeypatch.setattr(daily_budget, "reserve_async", reserve)
    provider = FakeProvider()
    await market_judge.judge("x", OURS, B64_CANDS, max_calls=5, client=provider.client())
    assert events == ["download", "reserve"]


async def test_c4_one_call_with_photos_spends_exactly_one_unit(llm_env, clean_budget, photos):
    llm_env(PM_LLM_BASE_URL=MIMO, PM_LLM_API_KEY="k")
    photos.missing.add(OURS[0])                       # la primera nuestra falla; la segunda alcanza
    photos.missing.add(B64_CANDS[1].image_url)        # y una de ML también
    provider, hooked = FakeProvider(), []
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0
    res = await market_judge.judge("x", OURS, B64_CANDS, max_calls=5, client=provider.client(),
                                   on_reserve=lambda s: hooked.append(1))
    assert res is not None and set(res.verdicts) == {"MLA8"}
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 1 and hooked == [1]
    [body] = provider.bodies
    assert body["thinking"] == {"type": "disabled"}
    sent = _sent_images(body)
    assert len(sent) == 2 and all(u.startswith("data:image/jpeg;base64,") for u in sent)
    assert "example.invalid" not in json.dumps(body) and "mlstatic" not in json.dumps(body)
    assert sorted(photos.requested) == sorted(OURS + [c.image_url for c in B64_CANDS])


async def test_c4_quota_exactly_at_the_cap_downloads_nothing(llm_env, clean_budget, photos):
    llm_env(PM_LLM_BASE_URL=MIMO, PM_LLM_API_KEY="k")
    provider = FakeProvider()
    for _ in range(3):
        await market_judge.judge("x", OURS, B64_CANDS, max_calls=3, client=provider.client())
    photos.requested.clear()
    assert await market_judge.judge("x", OURS, B64_CANDS, max_calls=3, client=provider.client()) is None
    assert photos.requested == [] and len(provider.bodies) == 3
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 3


async def test_c4_base64_through_the_run_spends_one_call_per_product(world, monkeypatch, llm_env, photos):  # noqa: F811
    llm_env(PM_LLM_BASE_URL=MIMO, PM_LLM_API_KEY="k", PM_LLM_MODEL="mimo-v2.6-flash")
    _ambiguous(world, our_host=OUR_HOST)
    tpm._set("pm_vision_max_calls", 5)
    provider = FakeProvider(_completion(GOOD, prompt=1_000, completion=60, reasoning=0))
    monkeypatch.setattr(market_judge, "make_client", provider.client)

    await price_monitor.run_price_monitor()

    s8 = tpm._snaps()["8"]
    assert (s8.ml_status, s8.match_source) == ("ok", "llm")
    [run] = tpm._runs()
    assert (run.llm_calls, run.llm_input_tokens, run.llm_output_tokens) == (1, 1_000, 60)
    assert daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 1
    [body] = provider.bodies
    assert len(_sent_images(body)) == 2            # la preview nuestra (una sola versión) + la de MLA8
    assert all(u.startswith("data:image/jpeg;base64,") for u in _sent_images(body))
    assert f"{OUR_HOST}/assets/preview/8.jpg" in photos.requested
    assert f"{OUR_HOST}/assets/source/8.jpg" not in photos.requested     # el source es la misma foto


async def test_c4_base64_with_our_photos_off_the_vendure_host_spends_nothing(world, monkeypatch, llm_env,  # noqa: F811
                                                                            photos):
    """Si las fotos del catálogo no salen del host de VENDURE_API_URL (CDN,
    bucket), en base64 ningún producto tiene veredicto. Queda fijado acá."""
    llm_env(PM_LLM_BASE_URL=MIMO, PM_LLM_API_KEY="k")
    _ambiguous(world, our_host="https://cdn.b2box")
    tpm._set("pm_vision_max_calls", 5)
    provider = FakeProvider()
    monkeypatch.setattr(market_judge, "make_client", provider.client)
    await price_monitor.run_price_monitor()
    assert provider.bodies == []
    assert not any("cdn.b2box" in u for u in photos.requested)
    [run] = tpm._runs()
    assert run.llm_calls == 0 and daily_budget.used_today(market_judge.LLM_COUNTER_KEY) == 0
    assert tpm._snaps()["8"].ml_status == "no_data"


async def test_c4_url_mode_sends_the_same_request_as_before(llm_env, clean_budget, photos):
    llm_env(PM_LLM_BASE_URL="https://openrouter.ai/api/v1", PM_LLM_API_KEY="k")
    provider = FakeProvider()
    await market_judge.judge("Org", OURS, B64_CANDS, max_calls=5, client=provider.client())
    [body] = provider.bodies
    assert set(body) == BASE_KEYS
    assert _sent_images(body) == [*OURS, *(c.image_url for c in B64_CANDS)]
    assert photos.requested == []


# ─── 5. tope de tiempo de las 8 fotos ──────────────────────────────────────


async def test_c5_eight_hanging_photos_finish_within_the_deadline(monkeypatch):
    deadline = 0.3
    monkeypatch.setattr(judge_images, "DEADLINE_S", deadline)
    monkeypatch.setattr(net_guard, "assert_public_url", lambda url: None)
    monkeypatch.setattr(net_guard, "assert_peer_public", lambda resp: None)

    async def drip():
        yield b"\xff\xd8"
        while True:
            await asyncio.sleep(0.05)
            yield b"\x00"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=drip(), request=request)

    monkeypatch.setattr(judge_images, "make_http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    urls = [f"{OUR_HOST}/assets/preview/{i}.jpg" for i in range(2)] + \
           [f"https://http2.mlstatic.com/D_{i}.jpg" for i in range(6)]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await judge_images.inline_images(urls) == {}
    assert loop.time() - t0 <= deadline * 1.5


# ─── 6. costo con el precio de MiMo v2.6 Flash ─────────────────────────────


async def test_c6_cost_uses_the_env_prices(world, monkeypatch, llm_env):  # noqa: F811
    llm_env(PM_LLM_BASE_URL=QWEN, PM_LLM_API_KEY="k", PM_LLM_PRICE_IN_PER_M="0.14",
            PM_LLM_PRICE_OUT_PER_M="0.28")
    assert (get_settings().pm_llm_price_in_per_m, get_settings().pm_llm_price_out_per_m) == (0.14, 0.28)
    _ambiguous(world)
    tpm._set("pm_vision_max_calls", 5)
    provider = FakeProvider(_completion(GOOD, prompt=12_345, completion=678, reasoning=0))
    monkeypatch.setattr(market_judge, "make_client", provider.client)
    await price_monitor.run_price_monitor()
    [run] = tpm._runs()
    assert (run.llm_input_tokens, run.llm_output_tokens) == (12_345, 678)
    exact = 12_345 * 0.14 / 1e6 + 678 * 0.28 / 1e6          # 0.00191814
    assert run.llm_cost_usd == pytest.approx(exact, abs=1e-6)  # se guarda redondeado a 6 decimales
    assert run.llm_cost_usd == 0.001918


# ─── 7. lo documentado = lo que lee config.py ──────────────────────────────


def test_c7_docs_list_exactly_the_pm_llm_variables_of_the_settings():
    root = Path(__file__).resolve().parents[2]
    config = {name.upper() for name in Settings.model_fields if name.startswith("pm_llm_")}
    env_example = set(re.findall(r"^(PM_LLM_[A-Z_]+)=", (root / ".env.example").read_text(), re.M))
    readme = set(re.findall(r"\bPM_LLM_[A-Z_]*[A-Z]\b", (root / "README.md").read_text()))
    assert env_example == config
    assert readme == config
