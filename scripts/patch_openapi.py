"""Correct the coverage endpoint in the OpenAPI document pygeoapi generates.

pygeoapi describes /collections/{id}/coverage with shared, feature-oriented
definitions: `f` says GeoJSON and omits GTiff, `bbox` talks about features and
is an array (so Swagger UI asks for one number at a time), and the 200 response
is OGC API - Features' "Features" document. This replaces them, for the coverage
endpoint only, with descriptions of what the service actually does.

Usage: python scripts/patch_openapi.py pygeoapi-openapi.yml  (run by serve.sh)
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartodem_service.provider import MAX_DEG2  # noqa: E402
from cartodem_service.tiles import MAX_FETCH  # noqa: E402

COVERAGE_PATH = '/collections/cartodem/coverage'
EXAMPLE_BBOX = '77.98,15.76,78.10,15.88'  # Kurnool

PARAMETERS = [
    {'$ref': '#/components/parameters/lang'},
    {
        'name': 'f', 'in': 'query', 'required': False,
        'description': 'Output format: `GTiff` (GeoTIFF), `json` (CoverageJSON) or `html` (map view). '
                       'Default: CoverageJSON. `Accept: image/tiff` also selects GeoTIFF.',
        'schema': {'type': 'string', 'enum': ['GTiff', 'json', 'html'], 'default': 'json'},
    },
    {
        'name': 'bbox', 'in': 'query', 'required': False,
        'description': 'Area to return: `minLon,minLat,maxLon,maxLat` in degrees (WGS 84 unless `bbox-crs` '
                       'says otherwise). Every pixel that touches the box is returned. Cannot be combined '
                       'with a `Lat`/`Long` subset.',
        'schema': {'type': 'string', 'pattern': r'^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$'},
        'example': EXAMPLE_BBOX,
    },
    {'$ref': '#/components/parameters/bbox-crs'},
    {
        'name': 'subset', 'in': 'query', 'required': False,
        # No `example` here: Swagger UI pre-fills examples, and bbox + subset together is a 400
        'description': 'Alternative to `bbox`: a range on one or both axes, `Lat(low:high)` and '
                       '`Long(low:high)`, in degrees, for example `Lat(15.76:15.88),Long(77.98:78.10)`. '
                       'A single axis spans the full extent of the other.',
        'schema': {'type': 'string'},
    },
    {
        'name': 'properties', 'in': 'query', 'required': False,
        'description': 'Fields to return. The only field is `elevation` (metres above the WGS 84 ellipsoid).',
        'schema': {'type': 'string', 'enum': ['elevation']},
    },
]

DESCRIPTION = (
    'Returns CartoDEM elevation (metres above the WGS 84 ellipsoid, 1 arc-second grid) for the requested '
    'area. Choose the area with `bbox` or `subset`.\n\n'
    f'**Limits:** at most {MAX_DEG2["GTiff"]:g} square degrees as GeoTIFF and {MAX_DEG2["json"]:g} as '
    f'CoverageJSON; at most {MAX_FETCH} tiles not yet cached per request. A request with no area asks for '
    'the whole coverage and is refused with 413.\n\n'
    '**First request for a new area** takes 10-30 s while its tile is fetched from Bhoonidhi; after that '
    'the area is served from cache.'
)

RESPONSES = {
    '200': {
        'description': 'The requested area. Pixels with no data (for example open sea) are -32768 in '
                       'GeoTIFF and null in CoverageJSON.',
        'content': {
            'image/tiff': {'schema': {'type': 'string', 'format': 'binary'}},
            'application/prs.coverage+json': {'schema': {'type': 'object'}},
            'text/html': {'schema': {'type': 'string'}},
        },
    },
    '400': {'description': 'Invalid request: malformed `bbox` or `subset`, unknown axis or property, '
                           'area outside the coverage, or `bbox` together with a `Lat`/`Long` subset.'},
    '404': {'description': 'Unknown collection.'},
    '413': {'description': 'Too large: over the area limit, or needs more uncached tiles than one request '
                           'may fetch. Request a smaller area.'},
    '500': {'description': 'Unexpected server error.'},
    '503': {'description': 'A tile is needed from Bhoonidhi but cannot be fetched right now.'},
}


def main():
    path = Path(sys.argv[1])
    doc = yaml.safe_load(path.read_text())
    op = doc['paths'][COVERAGE_PATH]['get']
    op['description'] = DESCRIPTION
    op['parameters'] = PARAMETERS
    op['responses'] = RESPONSES
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    print(f'Patched {COVERAGE_PATH} in {path}')


if __name__ == '__main__':
    main()
