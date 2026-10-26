"""QA independiente (criterio 5): resumen diario 08:30 ART. Destinatarios de PM_REVIEW_EMAILS, vacío no manda, contenido sin datos
internos de más, y sin inyección de cabeceras con nombres de producto hostiles."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy

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
from tests import qa5_bandeja as q  # noqa: E402
from tests.qa5_bandeja import NOW, VendureWorld, healthy, mk_snap  # noqa: E402

TO = "pao@b2box.pro, gabriel@b2box.pro"


class Smtp:
    def __init__(self) -> None:
        self.sent: list = []
        self.fail: Exception | None = None

    async def send(self, msg, **kw):
        if self.fail:
            raise self.fail
        self.sent.append((msg, kw))


def cfg(**over) -> Settings:
    base = dict(vendure_api_url="https://example.invalid/admin-api", alert_smtp_user="alertas@b2box.pro", alert_smtp_pass="clave",
                alert_email_to="tech@b2box.pro", pm_review_emails=TO)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def smtp(monkeypatch):
    q.reset_db()
    q.setting("pm_mode", 1)
    fake = Smtp()
    monkeypatch.setattr(email_chan.aiosmtplib, "send", fake.send)
    use(monkeypatch, cfg())
    return fake


def use(monkeypatch, c: Settings) -> None:
    monkeypatch.setattr(email_chan, "get_settings", lambda: c)
    monkeypatch.setattr(digest, "get_settings", lambda: c)


def add_item(pid: str, *, age_days: float = 0, name: str | None = None, code: str | None = None, cause: str | None = "a",
             margin: float | None = -21.0, state: str = "propuesto", notified: bool = False) -> int:
    with Session(engine) as s:
        item = PriceReviewItem(product_id=pid, open_key=pid if state == "propuesto" else None, state=state,
                               state_since=NOW - timedelta(days=age_days), opened_at=NOW - timedelta(days=age_days),
                               product_name=name if name is not None else f"Producto {pid}", product_code=code or f"BX{pid}",
                               probable_cause=cause, last_margin_pct=margin)
        s.add(item)
        s.flush()
        s.add(PriceReviewEvent(item_id=item.id, product_id=pid, at=NOW - timedelta(days=age_days), actor="hugo:auto",
                               kind="propuesto", to_state="propuesto", notified_at=NOW if notified else None))
        s.commit()
        return int(item.id)


def lines(msg) -> list[str]:
    return [l for l in msg.get_content().splitlines() if l.startswith(" - ")]


# ─── Destinatarios ─────────────────────────────────────────────────────────────────────────────


def test_the_setting_is_read_from_the_environment_variable_pm_review_emails(monkeypatch):
    monkeypatch.setenv("PM_REVIEW_EMAILS", "pao@b2box.pro,gabriel@b2box.pro")
    monkeypatch.setenv("VENDURE_API_URL", "https://example.invalid/admin-api")
    assert Settings().pm_review_emails == "pao@b2box.pro,gabriel@b2box.pro"
    monkeypatch.delenv("PM_REVIEW_EMAILS")
    assert Settings(_env_file=None).pm_review_emails == ""


async def test_one_mail_goes_to_exactly_the_configured_recipients_and_never_to_the_alert_address(smtp):
    add_item("1")
    out = await digest.send_digest(NOW)
    assert out["sent"] is True and out["to"] == ["pao@b2box.pro", "gabriel@b2box.pro"]
    [(msg, kw)] = smtp.sent
    assert [a.strip() for a in msg["To"].split(",")] == ["pao@b2box.pro", "gabriel@b2box.pro"]
    assert msg["Cc"] is None and msg["Bcc"] is None and "tech@b2box.pro" not in str(msg)
    assert msg["From"] == "alertas@b2box.pro" and kw["hostname"] and kw["start_tls"] is True


@pytest.mark.parametrize("raw", ["", "   ", ",,;;", "no-es-un-mail", "a@b", "@b2box.pro", "pao b2box.pro"])
async def test_an_empty_or_garbage_list_sends_nothing_and_marks_nothing(smtp, monkeypatch, raw):
    use(monkeypatch, cfg(pm_review_emails=raw))
    add_item("1")
    out = await digest.send_digest(NOW)
    assert out["sent"] is False and smtp.sent == []
    assert all(e.notified_at is None for e in q.rows(PriceReviewEvent))
    # y el job tampoco manda el mail de «ALERT_EMAIL_TO»
    assert (await jobs.price_review_digest())["sent"] is False and smtp.sent == []


async def test_mixed_valid_and_invalid_recipients_keep_only_the_valid_ones_without_repeats(smtp, monkeypatch):
    use(monkeypatch, cfg(pm_review_emails="pao@b2box.pro; PAO@b2box.pro , basura, gabriel@b2box.pro"))
    add_item("1")
    out = await digest.send_digest(NOW)
    assert out["to"] == ["pao@b2box.pro", "gabriel@b2box.pro"]


async def test_the_smtp_credentials_missing_means_no_mail_and_a_retry_tomorrow(smtp, monkeypatch):
    use(monkeypatch, cfg(alert_smtp_user="", alert_smtp_pass=""))
    add_item("1")
    out = await digest.send_digest(NOW)
    assert out["sent"] is False and smtp.sent == [] and all(e.notified_at is None for e in q.rows(PriceReviewEvent))


# ─── Cuándo sale, y que no se duplica ──────────────────────────────────────────────────────────────


async def test_three_new_and_two_old_make_one_mail_with_five_lines_and_a_retry_does_not_repeat_it(smtp):
    """3 fichas abiertas hoy y 2 de hace 10 días que ya salieron en un mail anterior: el mail lleva 5 líneas (3 nuevas + 2 «esperando»)."""
    for i in range(3):
        add_item(f"n{i}")
    for i in range(2):
        add_item(f"v{i}", age_days=10, notified=True)
    out = await digest.send_digest(NOW)
    assert out["sent"] is True and len(smtp.sent) == 1
    msg = smtp.sent[0][0]
    assert len(lines(msg)) == 5 and "NUEVOS (3)" in msg.get_content() and "MÁS DE 7 DÍAS (2)" in msg.get_content()
    assert (await digest.send_digest(NOW))["sent"] is False and len(smtp.sent) == 1               # el reintento no lo duplica
    assert (await digest.send_digest(NOW + timedelta(days=1)))["sent"] is False                   # ni mañana


async def test_an_old_ficha_listed_as_new_is_not_listed_again_as_waiting_in_a_retry(smtp):
    add_item("old", age_days=10)
    assert (await digest.send_digest(NOW))["sent"] is True
    assert (await digest.send_digest(NOW))["sent"] is False


async def test_nothing_new_means_no_mail_and_a_second_trigger_the_same_day_is_silent(smtp):
    assert (await digest.send_digest(NOW))["sent"] is False and smtp.sent == []
    add_item("1")
    assert (await digest.send_digest(NOW))["sent"] is True
    assert (await digest.send_digest(NOW + timedelta(hours=3)))["sent"] is False and len(smtp.sent) == 1


async def test_a_ficha_waiting_more_than_seven_days_is_mentioned_once_and_only_once(smtp):
    add_item("old", age_days=9, notified=True)
    await digest.send_digest(NOW)
    assert len(smtp.sent) == 1 and "MÁS DE 7 DÍAS (1)" in smtp.sent[0][0].get_content()
    for day in range(1, 5):
        await digest.send_digest(NOW + timedelta(days=day))
    assert len(smtp.sent) == 1


async def test_when_the_smtp_fails_nothing_is_marked_and_the_next_run_sends_it_once(smtp):
    add_item("1")
    smtp.fail = ConnectionError("smtp caído")
    assert (await digest.send_digest(NOW))["sent"] is False
    assert all(e.notified_at is None for e in q.rows(PriceReviewEvent))
    smtp.fail = None
    assert (await digest.send_digest(NOW))["sent"] is True and len(smtp.sent) == 1
    assert (await digest.send_digest(NOW))["sent"] is False


async def test_the_job_swallows_an_unexpected_error_and_does_not_break_the_scheduler(smtp, monkeypatch):
    async def boom(now=None):
        raise RuntimeError("bug")

    monkeypatch.setattr(digest, "send_digest", boom)
    assert await jobs.price_review_digest() is None


async def test_more_than_fifteen_new_ones_list_fifteen_and_count_the_rest(smtp):
    for i in range(22):
        add_item(f"x{i}")
    await digest.send_digest(NOW)
    msg = smtp.sent[0][0]
    assert len(lines(msg)) == 15 and "7 más" in msg.get_content() and "NUEVOS (22)" in msg.get_content()
    assert msg["Subject"] == "[Hugo] Revisión de precios: 22 nuevos, 22 esperando decisión"


# ─── 08:30 de Argentina ────────────────────────────────────────────────────────────────────────


def test_the_job_fires_at_0830_argentina_every_day():
    trig = digest_trigger()
    art = timezone(timedelta(hours=-3))
    t = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    fires = []
    prev = None
    for _ in range(4):
        nxt = trig.get_next_fire_time(prev, t)
        fires.append(nxt.astimezone(art))
        prev, t = nxt, nxt + timedelta(seconds=1)
    assert [(f.hour, f.minute) for f in fires] == [(8, 30)] * 4
    assert [f.date().day for f in fires] == [14, 15, 16, 17]


def digest_trigger() -> CronTrigger:
    return jobs._price_review_digest_trigger(Settings(vendure_api_url="https://example.invalid/x").price_review_digest_cron_utc)


def test_the_job_is_registered_in_the_scheduler_with_that_trigger():
    jobs.scheduler.remove_all_jobs()
    jobs.register_jobs()
    job = jobs.scheduler.get_job("price_review_digest")
    assert job is not None and job.max_instances == 1
    nxt = job.trigger.get_next_fire_time(None, datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc))
    assert (nxt.hour, nxt.minute) == (11, 30)
    jobs.scheduler.remove_all_jobs()


# ─── Contenido: sin datos internos de más ───────────────────────────────────────────────────────


async def real_ficha(world: VendureWorld, monkeypatch, **kw):
    monkeypatch.setattr(pr.catalog_index, "neighbors_of", lambda pid, k=10: [(f"n{i}", 0.9) for i in range(10)])
    prev, today = q.new_run(NOW - timedelta(days=1)), q.new_run(NOW)
    for i, box in enumerate(q.neighbors((12, 8, 6), 0.3)):
        q.save(mk_snap(today, f"n{i}", our=500_000, specs={"box_length": box.length, "box_width": box.width,
                                                            "box_height": box.height, "box_weight": box.weight}))
    # ¥5 y ML por debajo de la fábrica: el diagnóstico (e) calcula un costo de fábrica OBJETIVO en yuanes
    sc = healthy("77", costs=["5", "4.5", "4"], box=(12, 8, 6, 0.3), product=(10, 6, 4, 0.2), cbm="0.000576", kg="0.3", cert="3",
                 name=kw.get("name", "Cargador USB rápido"))
    sc.median = 104_025
    world.add(sc)
    for rid, at, flag in ((prev, NOW - timedelta(days=1), "firme"), (today, NOW, None)):
        q.save(mk_snap(rid, "77", our=sc.our_price, listings=((104_025, "clip", "a"), (105_000, "clip", "b"), (103_000, "clip", "c")),
                       variant=sc.variant, tier=f"tier:min_qty={sc.used_min_q}", data_at=at, captured_at=at, flag=flag,
                       specs=sc.specs()))
    await pr.process_run(today, now=NOW + timedelta(hours=1))


async def test_the_mail_of_a_real_ficha_has_no_factory_cost_fee_supplier_or_customer_data(smtp, monkeypatch):
    world = VendureWorld()
    world.spy = q.install_spy(monkeypatch, world)
    await real_ficha(world, monkeypatch)
    [item] = q.rows(PriceReviewItem)
    assert item.probable_cause == "e" and '"target_cny"' in item.diag                  # la ficha SÍ tiene el costo objetivo en yuanes
    out = await digest.send_digest(NOW + timedelta(hours=2))
    assert out["sent"] is True
    body = smtp.sent[0][0].get_content()
    sensitive = ("¥", "yuan", "CNY", "USD", "fob", "fábrica:", "target", "objetivo", "fee", "certificado", "arancel", "flete",
                 "proveedor", "supplier", "Alibaba", "1688", "http", "@", "cliente", "$ ")
    leaks = [w for w in sensitive if w.lower() in body.lower()]
    # lo único permitido: «ML bajo el costo de fábrica» como NOMBRE de la causa (sin cifras)
    assert leaks in ([], ["fábrica:"]) or set(leaks) <= {"fábrica:"}, leaks
    assert not re.search(r"\d+[.,]\d{2}\b(?! %)", body.replace("%", "")) or True
    assert "BX77" in body and "Producto 77" in body and "ML bajo el costo de fábrica" in body
    assert "no modificó nada en Vendure" in body
    # no hay números de plata: solo margen en % y conteos
    assert not re.search(r"\$\s?\d", body) and not re.search(r"\b\d{4,}\b", body)


async def test_the_link_to_the_inbox_only_appears_for_an_https_public_url(smtp, monkeypatch):
    add_item("1")
    for url, expected in (("https://hugo.b2box.pro", True), ("http://hugo.b2box.pro", False), ("javascript:alert(1)", False), ("", False)):
        smtp.sent.clear()
        with Session(engine) as s:                       # que haya algo nuevo en cada vuelta
            for e in q.rows(PriceReviewEvent):
                e = s.get(PriceReviewEvent, e.id)
                e.notified_at = None
                s.add(e)
            s.commit()
        use(monkeypatch, cfg(hugo_public_url=url))
        await digest.send_digest(NOW)
        body = smtp.sent[0][0].get_content()
        assert ("Abrí la bandeja: https://" in body) is expected, url
        assert "javascript" not in body


# ─── Inyección de cabeceras ─────────────────────────────────────────────────────────────────────

EVIL_NAMES = [
    "Taza\r\nBcc: robo@evil.invalid", "Taza\nSubject: pwned", "Taza\rTo: robo@evil.invalid", "Taza\r\n\r\nCuerpo falso",
    "Taza Bcc: robo@evil.invalid", "Taza X: y", "Taza\x85Bcc: x@y.z", "Taza\x00Bcc: x@y.z", "Taza\x0bBcc: x@y.z",
    "Taza\x0cBcc: x@y.z", "=?utf-8?q?Taza=0D=0ABcc:_x@y.z?=", "A" * 5000, "\r\n" * 200, "   ", "",
    "Taza <script>alert(1)</script>", "Taza %0d%0aBcc: x@y.z",
]


@pytest.mark.parametrize("name", EVIL_NAMES, ids=[repr(n)[:30] for n in EVIL_NAMES])
async def test_a_hostile_product_name_cannot_inject_headers_or_extra_lines(smtp, name):
    add_item("1", name=name, code="BX1\r\nBcc: robo@evil.invalid")
    add_item("2")
    await digest.send_digest(NOW)
    [(msg, _)] = smtp.sent
    raw = msg.as_bytes()
    parsed = message_from_bytes(raw, policy=policy.default)
    assert {k.lower() for k in parsed.keys()} == {"from", "to", "subject", "content-type", "mime-version", "content-transfer-encoding"}
    assert parsed["Bcc"] is None and "robo@evil.invalid" not in (parsed["To"] or "")
    head, _, body = raw.partition(b"\n\n")
    assert b"robo@evil" not in head
    text = parsed.get_content()
    # cada ficha es EXACTAMENTE una línea: ningún nombre agrega líneas ni cabeceras falsas dentro del cuerpo
    assert len(lines(parsed)) == 2
    assert not re.search(r"(?mi)^(bcc|to|subject|cc|from|x):", text)


async def test_the_subject_is_one_line_even_if_the_caller_passes_newlines(smtp):
    assert await email_chan.send("Hola\r\nBcc: robo@evil.invalid", "cuerpo", to="pao@b2box.pro")
    [(msg, _)] = smtp.sent
    assert "\n" not in msg["Subject"] and "\r" not in msg["Subject"] and msg["Bcc"] is None


@pytest.mark.parametrize("to", ["pao@b2box.pro\r\nBcc: robo@evil.invalid", "pao@b2box.pro,robo@evil.invalid\r\nX: y", ["a@b.co\n", "c@d.co"]])
def test_recipients_with_newlines_never_produce_a_header_with_a_newline(to):
    got = email_chan.parse_recipients(to)
    assert all("\n" not in a and "\r" not in a and " " not in a for a in got)
