"""El reparto de la suite en tandas para CI (backend/scripts/ci_shard.py): que no se pierda ni se repita ningún archivo."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_shard.py"
spec = importlib.util.spec_from_file_location("ci_shard", SCRIPT)
ci_shard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_shard)


@pytest.mark.parametrize("total", [1, 2, 4, 7])
def test_cada_archivo_cae_en_exactamente_una_tanda(total):
    groups = ci_shard.shards(total)
    flat = [p for g in groups for p in g]
    everything = sorted(ci_shard.TESTS.glob("test_*.py"))
    assert len(groups) == total
    assert sorted(flat) == everything and len(set(flat)) == len(flat)
    assert Path(__file__).resolve() in flat


def test_las_tandas_quedan_parejas_y_el_reparto_es_estable():
    groups = ci_shard.shards(4)
    loads = [sum(ci_shard.weight(p) for p in g) for g in groups]
    assert max(loads) - min(loads) <= max(ci_shard.weight(p) for g in groups for p in g)
    assert ci_shard.shards(4) == groups


def test_argumentos_invalidos_no_listan_nada(capsys):
    assert ci_shard.main(["x"]) == 2 and ci_shard.main(["x", "4", "5"]) == 2 and ci_shard.main(["x", "0", "1"]) == 2
    assert capsys.readouterr().out == ""
    assert ci_shard.main(["x", "4", "1"]) == 0
    listed = capsys.readouterr().out.split()
    assert listed and all(line.startswith("backend/tests/test_") for line in listed)
