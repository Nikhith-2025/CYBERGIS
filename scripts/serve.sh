#!/usr/bin/env bash
# Start the CartoDEM OGC API - Coverages service on http://localhost:5000
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="$PWD"
export PYGEOAPI_CONFIG=pygeoapi-config.yml
export PYGEOAPI_OPENAPI=pygeoapi-openapi.yml

.venv/bin/pygeoapi openapi generate "$PYGEOAPI_CONFIG" --output-file "$PYGEOAPI_OPENAPI"
.venv/bin/python scripts/patch_openapi.py "$PYGEOAPI_OPENAPI"
exec .venv/bin/python -m cartodem_service.app
