#!/usr/bin/env bash
# Verifica que backend/uv.lock esté al día con backend/pyproject.toml y que el
# Dockerfile pueda exportarlo. Es la misma comprobación que hace el build
# (`uv export --locked`), pero corrida antes de pushear.
#
#   backend/scripts/check_lock.sh
#
# Sale con 0 si todo está bien, 1 si el lock está desactualizado, 2 si falta uv.
# Pensado también para CI: no modifica nada ni resuelve versiones nuevas.
set -euo pipefail

cd "$(dirname "$0")/.."   # backend/

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: falta uv (https://docs.astral.sh/uv/getting-started/installation/)" >&2
    exit 2
fi

if ! uv lock --check; then
    cat >&2 <<'MSG'

ERROR: backend/uv.lock no coincide con backend/pyproject.toml.
El build de Docker va a fallar (uv export --locked). Para arreglarlo:

    cd backend && uv lock          # re-resuelve solo lo que cambió
    # correr la suite, y commitear pyproject.toml y uv.lock juntos

Ver README, sección "Actualizar dependencias".
MSG
    exit 1
fi

# Las dos variantes de imagen: sin browser (default) y con INSTALL_BROWSER=true.
uv export --locked --no-emit-project --format requirements-txt >/dev/null
uv export --locked --no-emit-project --format requirements-txt --extra browser >/dev/null

echo "OK: uv.lock está al día con pyproject.toml (imagen base y con browser)."
