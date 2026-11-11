"""Parte la suite en tandas parejas para correrlas en paralelo en CI.

    python backend/scripts/ci_shard.py <tandas> <cuál (1..tandas)>

Imprime, uno por línea, los archivos `backend/tests/test_*.py` de esa tanda (rutas relativas a la raíz del repo). El
reparto es determinista: los archivos, del que más pruebas tiene al que menos (contando `def test_`), van cada uno a la
tanda más liviana hasta ese momento. Todo archivo cae en exactamente una tanda: correr todas las tandas es correr la suite
entera. Sin dependencias: solo la biblioteca estándar.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "backend" / "tests"
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.MULTILINE)


def weight(path: Path) -> int:
    """Cuánto pesa un archivo: sus funciones de prueba (al menos 1, para que ninguno quede afuera del reparto)."""
    try:
        return max(1, len(_TEST_DEF.findall(path.read_text(encoding="utf-8", errors="replace"))))
    except OSError:
        return 1


def shards(total: int) -> list[list[Path]]:
    files = sorted(TESTS.glob("test_*.py"), key=lambda p: (-weight(p), p.name))
    out: list[list[Path]] = [[] for _ in range(total)]
    load = [0] * total
    for path in files:
        i = min(range(total), key=lambda k: (load[k], k))
        out[i].append(path)
        load[i] += weight(path)
    return [sorted(group) for group in out]


def main(argv: list[str]) -> int:
    try:
        total, index = int(argv[1]), int(argv[2])
    except (IndexError, ValueError):
        print(__doc__, file=sys.stderr)
        return 2
    if total < 1 or not 1 <= index <= total:
        print(f"tanda {index} de {total}: fuera de rango", file=sys.stderr)
        return 2
    for path in shards(total)[index - 1]:
        print(path.relative_to(ROOT).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
