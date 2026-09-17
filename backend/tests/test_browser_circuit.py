"""Corta-circuitos del browser: dejar de pagar 50 s por un host que da 0 fotos.

Medido en prod: un render de MercadoLibre tardó 53 s (launch con geoip por
proxy + scroll + networkidle + teardown) para terminar sin una sola foto, con el
cliente esperando del otro lado. Tres seguidos así y el host se saltea.
"""

import pytest

from app.ingest import browser_fetch

URL = "https://www.mercadolibre.com.ar/algo/p/MLA123"
OTRA = "https://detail.1688.com/offer/987.html"


@pytest.fixture(autouse=True)
def _limpio():
    browser_fetch.reset_circuit()
    yield
    browser_fetch.reset_circuit()


def test_arranca_cerrado():
    assert browser_fetch.circuit_open_for(URL) is False


def test_se_abre_recien_con_la_racha_completa(monkeypatch):
    monkeypatch.setattr(
        browser_fetch.get_settings(), "browser_fetch_zero_streak", 3, raising=False
    )
    for _ in range(2):
        browser_fetch.note_render_result(URL, images_found=0)
        assert browser_fetch.circuit_open_for(URL) is False

    browser_fetch.note_render_result(URL, images_found=0)
    assert browser_fetch.circuit_open_for(URL) is True


def test_un_render_con_fotos_reabre_el_host():
    for _ in range(5):
        browser_fetch.note_render_result(URL, images_found=0)
    assert browser_fetch.circuit_open_for(URL) is True

    browser_fetch.note_render_result(URL, images_found=4)
    assert browser_fetch.circuit_open_for(URL) is False


def test_el_castigo_es_por_host_y_no_contagia():
    """Que ML esté bloqueando no puede dejarnos sin browser para 1688."""
    for _ in range(5):
        browser_fetch.note_render_result(URL, images_found=0)

    assert browser_fetch.circuit_open_for(URL) is True
    assert browser_fetch.circuit_open_for(OTRA) is False


def test_se_reabre_solo_cuando_pasa_el_descanso(monkeypatch):
    for _ in range(5):
        browser_fetch.note_render_result(URL, images_found=0)
    assert browser_fetch.circuit_open_for(URL) is True

    # Viajamos al futuro en vez de dormir 900 s.
    real = browser_fetch.time.monotonic
    monkeypatch.setattr(
        browser_fetch.time, "monotonic", lambda: real() + 10_000
    )
    assert browser_fetch.circuit_open_for(URL) is False


def test_url_sin_host_no_rompe():
    browser_fetch.note_render_result("no-es-una-url", images_found=0)
    assert browser_fetch.circuit_open_for("no-es-una-url") is False
