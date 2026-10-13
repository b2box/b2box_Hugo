"""Resumen diario de la bandeja para Pao (PR2): un mail por día, sin duplicar y sin novedades no sale (criterio 2-7)."""

from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.models import PriceReviewEvent, PriceReviewItem  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.notifier import email as email_chan  # noqa: E402
from app.pricing import price_review as pr  # noqa: E402
from app.pricing import price_review_digest as digest  # noqa: E402
from app.scheduler import jobs  # noqa: E402
from tests import review_world as w  # noqa: E402

NOW = w.NOW
TO = "pao@b2box.pro, gabriel@b2box.pro"


class Smtp:
    def __init__(self) -> None:
        self.sent: list = []
        self.fail = False

    async def send(self, msg, **kw):
        if self.fail:
            raise ConnectionError("smtp caído")
        self.sent.append((msg, kw))


@pytest.fixture
def smtp(monkeypatch):
    w.reset_db()
    fake = Smtp()
    monkeypatch.setattr(email_chan.aiosmtplib, "send", fake.send)
    set_config(monkeypatch)
    return fake


def set_config(monkeypatch, **over):
    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", alert_smtp_user="alertas@b2box.pro",
                   alert_smtp_pass="clave", alert_email_to="tech@b2box.pro", pm_review_emails=TO, **over)
    monkeypatch.setattr(email_chan, "get_settings", lambda: cfg)
    monkeypatch.setattr(digest, "get_settings", lambda: cfg)
    return cfg


def add_item(pid: str, *, age_days: float = 0, state: str = "propuesto", notified: bool = False, cause: str = "a",
             margin: float | None = -21.0, name: str | None = None) -> int:
    with Session(engine) as s:
        item = PriceReviewItem(product_id=pid, open_key=pid if state == "propuesto" else None, state=state,
                               state_since=NOW - timedelta(days=age_days), opened_at=NOW - timedelta(days=age_days),
                               product_name=name or f"Producto {pid}", product_code=f"BX{pid}", probable_cause=cause,
                               last_margin_pct=margin)
        s.add(item)
        s.flush()
        s.add(PriceReviewEvent(item_id=item.id, product_id=pid, at=NOW - timedelta(days=age_days), actor="hugo:auto",
                               kind="propuesto", to_state="propuesto", notified_at=NOW if notified else None))
        s.commit()
        return int(item.id)


def item_lines(msg) -> list[str]:
    return [l for l in msg.get_content().splitlines() if l.startswith(" - ")]


# ─── 2-7 ─────────────────────────────────────────────────────────


async def test_three_new_and_two_old_make_one_mail_with_five_lines(smtp):
    for pid in ("n1", "n2", "n3"):
        add_item(pid)
    for pid in ("v1", "v2"):
        add_item(pid, age_days=9, notified=True)
    result = await digest.send_digest(NOW)
    assert result["sent"] is True and result["new"] == 3 and result["waiting"] == 2 and result["pending"] == 5
    [(msg, kw)] = smtp.sent
    assert msg["To"] == "pao@b2box.pro, gabriel@b2box.pro"
    assert msg["Subject"] == "[Hugo] Revisión de precios: 3 nuevos, 5 esperando decisión"
    lines = item_lines(msg)
    assert len(lines) == 5
    body = msg.get_content()
    assert "NUEVOS (3)" in body and "ESPERANDO DECISIÓN HACE MÁS DE 7 DÍAS (2)" in body and "hace 9 días" in body
    assert "BXn1 · Producto n1 · -21 % · caja fuera de rango" in body
    assert "no modificó nada en Vendure" in body
    assert kw["hostname"] and kw["username"] == "alertas@b2box.pro"


async def test_a_retry_does_not_duplicate_the_mail(smtp):
    for pid in ("n1", "n2", "n3"):
        add_item(pid)
    for pid in ("v1", "v2"):
        add_item(pid, age_days=9, notified=True)
    await digest.send_digest(NOW)
    again = await digest.send_digest(NOW + timedelta(minutes=1))
    tomorrow = await digest.send_digest(NOW + timedelta(days=1))
    assert len(smtp.sent) == 1
    assert again == {"sent": False, "reason": "sin novedades"} and tomorrow["reason"] == "sin novedades"
    assert all(e.notified_at is not None for e in w.events())
    assert len(w.events("aviso_sla")) == 2                       # cada «esperando» sale UNA vez


async def test_nothing_new_means_no_mail(smtp):
    add_item("v1", age_days=2, notified=True)                    # esperando, pero hace 2 días: no es noticia
    assert (await digest.send_digest(NOW))["reason"] == "sin novedades"
    assert smtp.sent == []


async def test_an_empty_recipient_setting_sends_nothing_and_marks_nothing(smtp, monkeypatch):
    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", alert_smtp_user="u", alert_smtp_pass="p", pm_review_emails="")
    monkeypatch.setattr(digest, "get_settings", lambda: cfg)
    add_item("n1")
    assert (await digest.send_digest(NOW))["reason"] == "PM_REVIEW_EMAILS vacío"
    assert smtp.sent == [] and w.events()[0].notified_at is None


async def test_if_the_smtp_fails_nothing_is_marked_and_the_next_run_sends_it(smtp):
    add_item("n1")
    smtp.fail = True
    res = await digest.send_digest(NOW)
    assert res["sent"] is False and "SMTP" in res["reason"] and w.events()[0].notified_at is None
    smtp.fail = False
    assert (await digest.send_digest(NOW + timedelta(days=1)))["sent"] is True and len(smtp.sent) == 1


async def test_without_smtp_credentials_it_does_not_send_or_mark(smtp, monkeypatch):
    cfg = Settings(vendure_api_url="https://example.invalid/admin-api", pm_review_emails=TO)
    monkeypatch.setattr(email_chan, "get_settings", lambda: cfg)
    monkeypatch.setattr(digest, "get_settings", lambda: cfg)
    add_item("n1")
    assert (await digest.send_digest(NOW))["sent"] is False
    assert smtp.sent == [] and w.events()[0].notified_at is None


async def test_at_most_15_new_ones_are_listed_and_the_rest_are_counted(smtp):
    for i in range(18):
        add_item(f"n{i:02d}")
    await digest.send_digest(NOW)
    [(msg, _)] = smtp.sent
    assert len(item_lines(msg)) == 15 and "… y 3 más en la bandeja" in msg.get_content()
    assert msg["Subject"].endswith("18 nuevos, 18 esperando decisión")


async def test_a_ficha_that_closed_itself_before_the_mail_is_counted_not_listed(smtp):
    add_item("n1")
    gone = add_item("n2")
    with Session(engine) as s:
        item = s.get(PriceReviewItem, gone)
        pr.close_item(s, item, note="Dejó de ser rojo", now=NOW)
        s.commit()
    await digest.send_digest(NOW)
    [(msg, _)] = smtp.sent
    body = msg.get_content()
    assert len(item_lines(msg)) == 1 and "Se cerraron solas desde el último mail: 1" in body
    assert msg["Subject"].endswith("1 nuevos, 1 esperando decisión")


async def test_text_from_vendure_cannot_break_the_mail(smtp):
    add_item("n1", name="Taza\r\nBcc: atacante@mala.com\x00   térmica " + "x" * 200)
    await digest.send_digest(NOW)
    [(msg, _)] = smtp.sent
    [line] = item_lines(msg)
    assert "\r" not in line and "Bcc" in line and len(line) < 200
    assert msg["Bcc"] is None and msg["To"] == "pao@b2box.pro, gabriel@b2box.pro"


async def test_the_link_to_the_inbox_only_appears_when_configured_with_https(smtp, monkeypatch):
    add_item("n1")
    set_config(monkeypatch, hugo_public_url="https://hugo.b2box.test")
    await digest.send_digest(NOW)
    assert "Abrí la bandeja: https://hugo.b2box.test/" in smtp.sent[0][0].get_content()
    w.reset_db()
    set_config(monkeypatch, hugo_public_url="http://inseguro")
    add_item("n2")
    await digest.send_digest(NOW)
    assert "Abrí la bandeja" not in smtp.sent[1][0].get_content()


# ─── 2-8: modo 0 ─────────────────────────────────────────────────


async def test_mode_0_leaves_no_ficha_no_event_and_therefore_no_mail(smtp):
    w.set_setting("pm_mode", 0)
    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    for pid in ("a", "b"):
        w.add_snap(prev, pid, flag="firme", data_at=NOW - timedelta(days=1))
        w.add_snap(today, pid)
    await pr.process_run(today, now=NOW)
    assert w.items() == [] and w.events() == []
    assert (await digest.send_digest(NOW))["reason"] == "sin novedades" and smtp.sent == []


async def test_the_whole_cycle_pass_then_mail(smtp):
    w.set_setting("pm_mode", 1)
    prev = w.make_run(started=NOW - timedelta(days=1))
    today = w.make_run()
    for pid in ("a", "b", "c"):
        w.add_snap(prev, pid, flag="firme", data_at=NOW - timedelta(days=1))
        w.add_snap(today, pid, specs={"box_length": 10, "box_width": 10, "box_height": 10, "box_weight": 1})

    class Quick:
        async def get_tier_breakdowns(self, v): return []
        async def get_priced_variant(self, v): return None
        async def get_certification(self, p, c): return None
        async def get_channel_pricing(self): return None

    await pr.process_run(today, now=NOW, source=Quick())
    res = await digest.send_digest(NOW)
    assert res["sent"] and res["new"] == 3
    assert "diagnóstico incompleto" in smtp.sent[0][0].get_content()         # sin desglose: lo dice, no lo inventa


# ─── email.send(to=) y destinatarios ──────────────────────────────


def test_recipients_are_parsed_and_validated():
    assert email_chan.parse_recipients("a@b.com, c@d.org;a@b.com  e@f.io") == ["a@b.com", "c@d.org", "e@f.io"]
    assert email_chan.parse_recipients("") == [] and email_chan.parse_recipients(None) == []
    assert email_chan.parse_recipients(["x@y.com", "no-es-un-mail", "a@b\r\nBcc: z@z.com"]) == ["x@y.com"]
    assert email_chan.parse_recipients("a@b.com\nBcc:evil@x.com") == ["a@b.com"]    # el salto de línea corta: «Bcc:evil@x.com» no es una dirección


async def test_send_without_to_goes_to_the_usual_address_and_with_to_only_to_those(smtp):
    assert await email_chan.send("asunto", "cuerpo") is True
    assert smtp.sent[0][0]["To"] == "tech@b2box.pro"
    assert await email_chan.send("otro", "cuerpo", to="x@y.com, z@w.com") is True
    assert smtp.sent[1][0]["To"] == "x@y.com, z@w.com"
    assert await email_chan.send("nadie", "cuerpo", to="basura") is False and len(smtp.sent) == 2


async def test_a_subject_with_a_newline_cannot_inject_headers(smtp):
    await email_chan.send("hola\r\nBcc: mala@x.com", "cuerpo")
    msg = smtp.sent[0][0]
    assert msg["Bcc"] is None and "\n" not in msg["Subject"]


# ─── el job ──────────────────────────────────────────────────────


def test_the_job_runs_at_08_30_argentina_which_is_11_30_utc(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    w.reset_db()
    monkeypatch.setattr(jobs, "scheduler", AsyncIOScheduler())
    jobs.register_jobs()
    job = jobs.scheduler.get_job("price_review_digest")
    assert job is not None and isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("11", "30") and str(job.trigger.timezone) == "UTC"


def test_a_bad_cron_falls_back_to_the_default():
    t = jobs._price_review_digest_trigger("esto no es un cron")
    assert {f.name: str(f) for f in t.fields}["hour"] == "11"
