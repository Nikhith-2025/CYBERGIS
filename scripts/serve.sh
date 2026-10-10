#!/usr/bin/env bash
# Start the coverage service on http://localhost:5000 (gunicorn; DEV=1 for Flask's dev server)
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="$PWD"
export PYGEOAPI_CONFIG=pygeoapi-config.yml
export PYGEOAPI_OPENAPI=pygeoapi-openapi.yml

.venv/bin/pygeoapi openapi generate "$PYGEOAPI_CONFIG" --output-file "$PYGEOAPI_OPENAPI"
.venv/bin/python scripts/patch_openapi.py "$PYGEOAPI_OPENAPI"
if [ "${DEV:-0}" = "1" ]; then
  exec .venv/bin/python -m cartodem_service.app
fi
exec .venv/bin/gunicorn -c gunicorn.conf.py cartodem_service.app:APP
