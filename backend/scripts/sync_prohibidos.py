"""Vuelve a copiar la lista de lo que no vendemos desde la lista canónica (la del radar de productos).

    python scripts/sync_prohibidos.py /ruta/al/prohibidos.yaml

Copia SOLO los datos (categorías, palabras y excepciones), sin los comentarios del original, a `app/prohibidos.yaml`.
La copia no se edita a mano: se corrige el original y se vuelve a correr esto.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

DESTINO = Path(__file__).resolve().parents[1] / "app" / "prohibidos.yaml"
CABECERA = (
    "# Lo que no vendemos. COPIA de la lista canónica (la del radar de productos): NO editar a mano.\n"
    "# Se regenera con `python scripts/sync_prohibidos.py <lista canónica>`.\n"
    "# Forma y reglas de comparación: ver app/prohibidos.py.\n"
)
CAMPOS = ("categoria", "porque", "es", "zh", "excepto")


def main(origen: str) -> int:
    crudo = yaml.safe_load(Path(origen).read_text(encoding="utf-8"))
    filas = crudo.get("prohibidos") if isinstance(crudo, dict) else None
    if not isinstance(filas, list) or not filas:
        print("el origen no tiene la clave `prohibidos` con categorías", file=sys.stderr)
        return 1
    limpio = []
    for fila in filas:
        if not isinstance(fila, dict) or not fila.get("categoria"):
            continue
        item = {k: fila[k] for k in CAMPOS if k in fila}
        if isinstance(item.get("porque"), str):
            item["porque"] = " ".join(item["porque"].split())
        limpio.append(item)
    cuerpo = yaml.safe_dump({"prohibidos": limpio}, allow_unicode=True, sort_keys=False, width=110)
    DESTINO.write_text(CABECERA + "\n" + cuerpo, encoding="utf-8")
    print(f"{len(limpio)} categorías → {DESTINO}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
