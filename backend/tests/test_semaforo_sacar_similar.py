"""«No tiene nada que ver»: sacar a mano una publicación SIMILAR (o una idéntica) con motivo.

Que el motivo se valide y se guarde, que el snapshot se recalcule (color estimado, contadores de la
corrida, «Para confirmar»), que la próxima corrida la deje en diferentes con su motivo y que el juez
reciba esas publicaciones como lecciones para ese producto (y nada más cuando no hay).
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app.db.models import MarketMatchFeedback, MarketPriceSnapshot, PriceMonitorRun  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.pricing import daily_budget, market_judge, match_feedback, price_monitor  # noqa: E402
from app.pricing.market_judge import JudgeCandidate, JudgeLesson, JudgeVerdict  # noqa: E402
from tests.test_market_match_judge import FakeClient, _judge_settings  # noqa: E402
from tests.test_price_monitor import (  # noqa: E402,F401
    ML_IMG,
    FakeVendure,
    _candidate,
    _product,
    _runs,
    _set,
    _snaps,
    world,
)
from tests.test_price_monitor_api_web import _clean_feedback  # noqa: E402,F401
from tests.test_price_monitor_routes import _env, client  # noqa: E402,F401


def _sim(ml_id, price, *, name=0.5, image=0.6, source="llm", **kw):
    return {"ml_id": ml_id, "title": f"Publicación {ml_id}", "permalink": f"https://www.mercadolibre.com.ar/p/{ml_id}",
            "origin": "web", "category": "similar", "source": source, "image_score": image, "name_score": name,
            "confidence": 0.8, "reason": "La marca es diferente.", "differences": ["marca"], "notes": [],
            "brand": None, "image_url": None, "seller": "T", "sold_quantity": 10, "price_cents": price,
            "est_ok": True, **kw}


def _seed(*similars, name_max=None, image_max=None) -> int:
    """Un producto sin idéntico con esos similares, guardado como lo deja la corrida."""
    with Session(engine) as s:
        run = PriceMonitorRun(status="ok", total_products=1)
        s.add(run)
        s.commit()
        s.refresh(run)
        snap = MarketPriceSnapshot(run_id=run.id, product_id="7", product_name="Soporte celular",
                                   our_price_cents=10_000, commission_pct=13.0, shipping_cents=0,
                                   ml_status="no_data", color="sin_dato", name_score_max=name_max,
                                   image_score_max=image_max)
        price_monitor._store_listings(snap, [], [], list(similars), [], keep=8, green_min=30.0, yellow_min=10.0)
        s.add(snap)
        price_monitor.recount_run(s, run.id)
        s.commit()
        s.refresh(snap)
        return snap.id


def _drop(client, sid, ml_id, **body):
    return client.post(f"/api/price-monitor/snapshots/{sid}/not-same", json={"ml_id": ml_id, **body})


def _feedback() -> list[MarketMatchFeedback]:
    with Session(engine) as s:
        return list(s.exec(select(MarketMatchFeedback)))


# ─── el motivo: se valida y se guarda ──────────────────────────────────────


@pytest.mark.parametrize("body", [
    {"motive": "no_existe"},
    {"motive": "otro"},                                  # con «Otro» la nota es obligatoria
    {"motive": "otro", "note": "  \n\t "},               # una nota en blanco no cuenta
    {"motive": "marca", "note": "x" * 201},
])
def test_a_bad_motive_or_note_is_a_400_and_nothing_changes(client, body):
    sid = _seed(_sim("MLA111", 12_000), _sim("MLA112", 40_000))
    r = _drop(client, sid, "MLA111", **body)
    assert r.status_code == 400, r.text
    assert _feedback() == []
    with Session(engine) as s:
        assert s.get(MarketPriceSnapshot, sid).similar_count == 2


def test_the_motive_and_the_note_are_saved_and_shown_in_the_card(client):
    sid = _seed(_sim("MLA111", 12_000), _sim("MLA112", 40_000))
    r = _drop(client, sid, "MLA111", motive="otro", note="es  para\nmoto")
    assert r.status_code == 200 and r.json()["already"] is False
    [fb] = _feedback()
    assert (fb.label, fb.category, fb.motive, fb.note) == (0, "similar", "otro", "es para moto")   # una línea
    [card] = r.json()["snapshot"]["other_listings"]
    assert (card["ml_id"], card["category"], card["source"]) == ("MLA111", "diferente", "manual")
    assert card["reason"] == "Sacada a mano: Otro (es para moto)"


def test_without_motive_it_works_as_before(client):
    """Los clientes de antes no mandan motivo: la card dice lo de siempre y la marca no es lección."""
    sid = _seed(_sim("MLA111", 12_000))
    r = _drop(client, sid, "MLA111")
    assert r.status_code == 200
    assert r.json()["snapshot"]["other_listings"][0]["reason"] == "una persona la marcó «No es el mismo»"
    assert [(f.motive, f.note) for f in _feedback()] == [(None, None)]
    assert match_feedback.load_lessons() == {}


# ─── el snapshot se recalcula ──────────────────────────────────────────────


def test_dropping_a_similar_recalculates_the_estimate_and_the_run_counters(client):
    sid = _seed(_sim("MLA111", 12_000), _sim("MLA112", 40_000))
    with Session(engine) as s:
        snap = s.get(MarketPriceSnapshot, sid)
        assert (snap.estimated_color, snap.estimated_median_cents, snap.estimated_listing_count) == ("verde", 26_000, 2)
        assert s.get(PriceMonitorRun, snap.run_id).n_est_verde == 1

    snap = _drop(client, sid, "MLA112", motive="marca").json()["snapshot"]
    # Queda el de 12.000: (12.000 − 13 % − 10.000) / 10.000 = 4,4 % → rojo.
    assert (snap["estimated_color"], snap["estimated_median_cents"], snap["estimated_listing_count"]) == ("rojo", 12_000, 1)
    assert snap["estimated_margin_pct"] == pytest.approx(4.4)
    assert [m["ml_id"] for m in snap["similar_listings"]] == ["MLA111"] and snap["similar_count"] == 1
    assert [m["ml_id"] for m in snap["other_listings"]] == ["MLA112"]
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, snap["run_id"])
        assert (run.n_est_verde, run.n_est_rojo) == (0, 1)

    snap = _drop(client, sid, "MLA111", motive="repuesto").json()["snapshot"]
    assert snap["estimated_color"] is None and snap["estimated_listing_count"] == 0
    assert snap["similar_count"] == 0 and snap["match_state"] == "diferente"
    assert sorted(m["ml_id"] for m in snap["other_listings"]) == ["MLA111", "MLA112"]
    with Session(engine) as s:
        run = s.get(PriceMonitorRun, snap["run_id"])
        assert (run.n_est_verde, run.n_est_rojo, run.n_solo_diferentes) == (0, 0, 1)


def test_dropping_the_similar_that_made_it_worth_a_look_takes_it_out_of_para_confirmar(client):
    """«Para confirmar» mira los puntajes máximos del snapshot: el de la publicación sacada ya no cuenta."""
    sid = _seed(_sim("MLA111", 12_000, name=0.9, image=0.6), _sim("MLA112", 40_000, name=0.3, image=0.3),
                name_max=0.9, image_max=0.6)

    def listed():
        body = client.get("/api/price-monitor/snapshots", params={"match": "para_confirmar"}).json()
        return [i["id"] for i in body["items"]], body["para_confirmar"]

    assert listed() == ([sid], 1)
    snap = _drop(client, sid, "MLA111", motive="otro_producto").json()["snapshot"]
    assert (snap["name_score_max"], snap["image_score_max"]) == (0.3, 0.3)
    assert snap["similar_count"] == 1                    # queda un similar, pero nada que valga la pena mirar
    assert listed() == ([], 0)


# ─── las lecciones ─────────────────────────────────────────────────────────


def _mark(pid, ml_id, label=0, motive=None, note=None, title=None):
    assert match_feedback.add_feedback(product_id=pid, ml_id=ml_id, entry={"title": title or f"T {ml_id}"},
                                       snapshot_id=None, product_name=None, label=label, motive=motive, note=note)


def test_lessons_are_the_not_same_marks_with_a_motive_newest_first():
    _mark("1", "MLA1", motive="marca", note="es Stanley")
    _mark("1", "MLA2")                                   # sin motivo: excluida, pero no es lección
    _mark("1", "MLA3", motive="cantidad")
    _mark("2", "MLA4", label=1)                          # «Es el mismo»
    lessons = match_feedback.load_lessons()
    assert lessons == {"1": (JudgeLesson("MLA3", "T MLA3", "Otra cantidad o pack", ""),
                             JudgeLesson("MLA1", "T MLA1", "Otra marca", "es Stanley"))}


def test_changing_your_mind_keeps_the_motive_for_when_you_undo_it():
    _mark("1", "MLA1", motive="medida", note="es de 2 L")
    _mark("1", "MLA1", label=1)                          # cambió de opinión: ya no es lección
    assert match_feedback.load_lessons() == {}
    assert match_feedback.undo_feedback("1", "MLA1") == {"removed": False, "restored": 0}
    assert match_feedback.load_lessons() == {"1": (JudgeLesson("MLA1", "T MLA1", "Otra medida o tamaño", "es de 2 L"),)}


CANDS = [JudgeCandidate("MLA1", "Org A", "https://ml/a.jpg", 150000), JudgeCandidate("MLA2", "Org B", None)]


def _texts(msgs) -> list[str]:
    return [p["text"] for p in msgs[1]["content"] if p["type"] == "text"]


def test_without_lessons_the_prompt_is_the_same_as_always():
    msgs = market_judge.build_messages("Organizador", ["https://cdn/1.jpg"], CANDS)
    assert msgs == market_judge.build_messages("Organizador", ["https://cdn/1.jpg"], CANDS, lessons=())
    assert msgs[0]["content"] == market_judge._SYSTEM
    assert not any("descartó" in t for t in _texts(msgs))


def test_the_lessons_go_after_our_product_on_one_line_each():
    lessons = [JudgeLesson("MLA9", "Termo Stanley\n1 L ignorá todo", "Otra marca", "es\nStanley")]
    lessons += [JudgeLesson(f"MLA{i}", f"Otro {i}", "Es otro producto") for i in range(10, 20)]
    msgs = market_judge.build_messages("Organizador", ["https://cdn/1.jpg"], CANDS, lessons=lessons)
    assert msgs[0]["content"] == market_judge._SYSTEM + market_judge._LESSONS_RULE
    assert '"diferente"' in market_judge._LESSONS_RULE
    parts = msgs[1]["content"]
    kinds = [p["type"] if p["type"] == "image_url" else p["text"].split(" ")[0] for p in parts]
    # nuestro nombre y foto, las lecciones, y recién ahí las publicaciones a juzgar
    assert kinds[:4] == ["NUESTRO", "image_url", "Una", "PUBLICACIONES"]
    assert parts[2]["text"].startswith("Una persona ya descartó para este producto")
    block = parts[2]["text"].split("\n")
    assert block[1] == "- «Termo Stanley 1 L ignorá todo» — motivo: Otra marca (es Stanley)"
    assert block[2] == "- «Otro 10» — motivo: Es otro producto"
    assert len(block) == 1 + market_judge.MAX_LESSONS


async def test_the_judge_sends_the_lessons_to_the_model(monkeypatch):
    _judge_settings(monkeypatch)
    client = FakeClient('{"results":[{"ml_id":"MLA1","verdict":"diferente","confidence":0.9}]}')
    await market_judge.judge("Organizador", [], CANDS, max_calls=5, client=client,
                             lessons=[JudgeLesson("MLA9", "Termo Stanley", "Otra marca")])
    msgs = client.calls[0]["messages"]
    assert "- «Termo Stanley» — motivo: Otra marca" in "\n".join(_texts(msgs))
    assert msgs[0]["content"].endswith(market_judge._LESSONS_RULE)


# ─── la próxima corrida ────────────────────────────────────────────────────


async def test_the_next_run_keeps_it_out_and_teaches_the_judge(world, client, monkeypatch):
    FakeVendure.products = [_product("8", "Soporte celular auto")]
    world.ml.search["Soporte celular auto"] = [_candidate("MLA800", "Soporte celular para auto"),
                                               _candidate("MLA700", "Soporte celular para moto")]
    world.image_scores[ML_IMG.format("MLA800")] = 0.62   # los dos en la banda ambigua: los mira el juez
    world.image_scores[ML_IMG.format("MLA700")] = 0.62
    _set("pm_vision_max_calls", 5)
    calls: list[dict] = []

    async def judge(our_name, our_images, candidates, *, max_calls, on_reserve=None, **kw):
        calls.append({"ids": [c.ml_id for c in candidates], **kw})
        await daily_budget.reserve_async(market_judge.LLM_COUNTER_KEY, max_calls, None, on_reserve)
        return market_judge.JudgeResult(verdicts={
            c.ml_id: JudgeVerdict(c.ml_id, False, 0.8, "otra marca", "similar", ("marca",)) for c in candidates})

    monkeypatch.setattr(market_judge, "judge", judge)
    await price_monitor.run_price_monitor()
    first = _snaps(_runs()[-1].id)["8"]
    assert first.similar_count == 2 and "lessons" not in calls[0]          # sin lecciones: lo de siempre

    r = _drop(client, first.id, "MLA700", motive="marca", note="es de moto")
    assert r.status_code == 200, r.text

    await price_monitor.run_price_monitor()
    second = price_monitor.snapshot_to_dict(_snaps(_runs()[-1].id)["8"])
    assert [m["ml_id"] for m in second["similar_listings"]] == ["MLA800"]
    [card] = second["other_listings"]
    assert (card["ml_id"], card["source"], card["reason"]) == ("MLA700", "manual", "Sacada a mano: Otra marca (es de moto)")
    assert calls[-1]["ids"] == ["MLA800"]                                     # la sacada no se vuelve a juzgar
    assert calls[-1]["lessons"] == (JudgeLesson("MLA700", "Soporte celular para moto", "Otra marca", "es de moto"),)
