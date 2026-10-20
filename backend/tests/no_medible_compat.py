"""La API de hoy, vista como la servía origin/main, para los dorados de regresión (fix/semaforo-sin-dato).

«No medible» (sin precio en Vendure, sin foto, sin nombre, otra moneda) salió de «Sin dato»: en la base esas filas
siguen siendo `sin_dato`, pero la API ahora las cuenta en `colors.no_medible` y `color=sin_dato` ya no las trae (las
trae `color=no_medible`). Es un cambio buscado, no aditivo. Para que el dorado siga probando que TODO lo demás quedó
igual, acá se deshace exactamente ese cambio y nada más:

  * todo dict con `sin_dato` y `no_medible` (los `colors` de la tabla, de la corrida y de Salud) vuelve a sumarlos;
  * `color=sin_dato` se pide también como `color=no_medible` y se juntan los items en el orden de la tabla.
"""

from __future__ import annotations

from typing import Any


def fold_no_medible(value: Any) -> Any:
    if isinstance(value, dict):
        out = {k: fold_no_medible(v) for k, v in value.items()}
        if isinstance(out.get("sin_dato"), int) and isinstance(out.get("no_medible"), int):
            out["sin_dato"] += out.pop("no_medible")
        return out
    if isinstance(value, list):
        return [fold_no_medible(v) for v in value]
    return value


def _table_order(item: dict) -> tuple:
    """El ORDER BY de /snapshots: con margen real primero, después con estimado, después por nombre."""
    est, guess = item.get("est_margin_pct"), item.get("estimated_margin_pct")
    return (est is None, est or 0.0, guess is None, guess or 0.0, (item.get("product") or {}).get("name") or "")


def get_as_origin_main(client, path: str) -> Any:
    body = client.get(path).json()
    if "color=sin_dato" in path:
        extra = client.get(path.replace("color=sin_dato", "color=no_medible")).json()
        body["items"] = sorted([*body["items"], *extra["items"]], key=_table_order)
        body["total"] += extra["total"]
        body["has_more"] = (body["page"] + 1) * body["page_size"] < body["total"]
    return fold_no_medible(body)
