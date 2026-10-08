"""Herramienta de calibración del filtro "mismo producto": la parte offline
(CSV etiquetado → precisión/recall) y la búsqueda de umbrales. El CSV de
estos tests es sintético y chico; el real lo arma `export` en prod."""

from __future__ import annotations

import csv
import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.pricing import calibrate_market_match as cal  # noqa: E402
from app.pricing.market_match import Thresholds  # noqa: E402

THR = Thresholds(image=0.65, name=0.60, image_strong=0.80, image_veto=0.40, name_veto=0.30)


def _row(pid, ml, image, name, label, **extra):
    return {"product_id": pid, "ml_id": ml, "image_score": image, "name_score": name,
            "same_product": label, **extra}


ROWS = [
    _row("1", "a", "0.90", "0.90", "1"),     # VP por imagen fuerte
    _row("2", "b", "0.70", "0.70", "si"),    # VP por imagen + nombre
    _row("3", "c", "0.62", "0.90", "sí"),    # FN en banda ambigua
    _row("4", "d", "0.70", "0.70", "no"),    # FP
    _row("5", "e", "0.30", "0.90", "0"),     # VN (veto)
    _row("6", "f", "0.90", "0.90", ""),      # sin etiqueta: se ignora
    _row("7", "g", "", "0.95", "true"),      # FN: sin imagen nunca es match
]


def test_parse_label_variants():
    assert [cal.parse_label(v) for v in ("1", "Sí", "x", "0", "NO", "", "tal vez")] == [
        True, True, True, False, False, None, None]


def test_load_skips_unlabeled_and_computes_missing_name_score():
    rows = [*ROWS, _row("8", "h", "0.9", "", "1", our_name="Taza de ceramica", ml_title="Taza ceramica")]
    pairs = cal.load_labeled(rows)
    assert len(pairs) == 7
    assert pairs[-1].name_score == pytest.approx(1.0)
    assert pairs[5].image_score is None


def test_evaluate_counts_the_confusion_matrix():
    m = cal.evaluate(cal.load_labeled(ROWS), THR)
    assert (m.tp, m.fp, m.fn, m.tn) == (2, 1, 2, 1)
    assert m.precision == pytest.approx(2 / 3) and m.recall == pytest.approx(0.5)
    assert m.positives_in_ambiguous == 1
    assert (m.products_with_match, m.products_labeled) == (3, 6)


def test_grid_search_finds_thresholds_that_meet_the_precision():
    found = cal.grid_search(cal.load_labeled(ROWS), THR, min_precision=0.9)
    assert found is not None
    thr, m = found
    assert m.precision >= 0.9 and m.fp == 0
    # Para no aceptar el FP (0.70/0.70) y sí el ambiguo (0.62/0.90) hace falta
    # bajar imagen y subir nombre.
    assert m.tp == 2


def test_evaluate_cli_exit_code_follows_the_criterion(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cal.market_match.Thresholds, "from_runtime", classmethod(lambda cls: THR))
    path = tmp_path / "pares.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cal.CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in cal.CSV_FIELDS} for r in ROWS)
    assert cal.main(["evaluate", str(path)]) == 1  # 67 % de precisión: no cumple
    out = capsys.readouterr().out
    assert "NO CUMPLE" in out and "precisión 66.7 %" in out
    # Con umbrales que sacan el FP la precisión llega a 100 % y el recall a 50 %
    # (la fila sin imagen nunca puede ser match): cumple si se pide 50 %.
    assert cal.main(["evaluate", str(path), "--image", "0.60", "--name", "0.80",
                     "--image-strong", "0.85", "--min-recall", "0.5"]) == 0


def test_evaluate_cli_without_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(cal.market_match.Thresholds, "from_runtime", classmethod(lambda cls: THR))
    path = tmp_path / "vacio.csv"
    path.write_text(",".join(cal.CSV_FIELDS) + "\n", encoding="utf-8")
    assert cal.main(["evaluate", str(path)]) == 2


# ─── CSV injection (security L7) ────────────────────────────────────────────


@pytest.mark.parametrize("text", ["=HYPERLINK(\"http://x\")", "+1", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_formula_like_text_is_escaped(text):
    assert cal.csv_safe(text) == "'" + text
    assert cal._unescape(cal.csv_safe(text)) == text


def test_export_row_escapes_text_but_keeps_scores_numeric():
    from types import SimpleNamespace

    from app.pricing.market_match import Decision
    from app.pricing.market_ml import MlCandidate

    product = SimpleNamespace(id="1", product_code="BX1", name="=cmd|' /C calc'!A0",
                              featured_image_url="https://cdn/1.jpg")
    decision = Decision(MlCandidate(id="MLA1", name="@SUM(1+1)", image_urls=[], permalink=""),
                        image_score=-0.05, name_score=0.5, verdict="no")
    row = cal.export_row(product, decision)
    assert row["our_name"].startswith("'=") and row["ml_title"].startswith("'@")
    assert row["image_score"] == "-0.0500" and row["name_score"] == "0.5000"
    assert row["product_id"] == "1" and row["same_product"] == ""
    # Y al leerlo etiquetado, el score negativo sigue siendo número.
    [pair] = cal.load_labeled([{**row, "same_product": "0"}])
    assert pair.image_score == -0.05


def test_export_row_marks_known_negatives_from_the_dashboard():
    from types import SimpleNamespace

    from app.pricing.market_match import MATCH, Decision
    from app.pricing.market_ml import MlCandidate

    product = SimpleNamespace(id="1", product_code="BX1", name="Taza", featured_image_url="")
    decision = Decision(MlCandidate(id="MLA5", name="Taza x6"), 0.7, 0.8, MATCH, "clip")
    assert cal.export_row(product, decision)["same_product"] == ""
    assert cal.export_row(product, decision, known_negative=True)["same_product"] == "0"
    assert cal.parse_label(cal.export_row(product, decision, known_negative=True)["same_product"]) is False
