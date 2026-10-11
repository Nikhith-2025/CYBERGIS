"""WSGI entry point: pygeoapi's Flask app plus a small request-fixing middleware.

It also starts the thread that keeps the Bhoonidhi portal session alive, so
new tiles can be downloaded without another OTP login.

The middleware covers two gaps in pygeoapi 0.24's coverage handling:
- a malformed `bbox` is reported as 500; we answer 400 InvalidParameterValue
- `Accept: image/tiff` is ignored; we map it to `f=GTiff` when no `f` is given

Run locally:  python -m cartodem_service.app
WSGI servers: cartodem_service.app:APP
Requires PYGEOAPI_CONFIG and PYGEOAPI_OPENAPI to be set before import.
"""
import json
from urllib.parse import parse_qsl, urlencode

from pygeoapi.api import validate_bbox
from pygeoapi.flask_app import APP, CONFIG

from cartodem_service import bhoonidhi


def _exception(start_response, description):
    body = json.dumps({
        'code': 'InvalidParameterValue',
        'type': 'InvalidParameterValue',
        'description': description,
    }).encode()
    start_response('400 Bad Request', [('Content-Type', 'application/json'),
                                       ('Content-Length', str(len(body)))])
    return [body]


class CoverageRequestMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if environ.get('PATH_INFO', '').rstrip('/').endswith('/coverage'):
            params = parse_qsl(environ.get('QUERY_STRING', ''), keep_blank_values=True)
            names = {k for k, _ in params}

            bbox = next((v for k, v in params if k == 'bbox'), None)
            if bbox is not None:
                try:
                    validate_bbox(bbox)
                except ValueError as err:
                    return _exception(start_response, f'Invalid bbox: {err}')

            accept = environ.get('HTTP_ACCEPT', '')
            if 'f' not in names and 'image/tiff' in accept:
                params.append(('f', 'GTiff'))
                environ['QUERY_STRING'] = urlencode(params)

        return self.app(environ, start_response)


APP.wsgi_app = CoverageRequestMiddleware(APP.wsgi_app)
bhoonidhi.keep_session_alive()


if __name__ == '__main__':
    bind = CONFIG['server']['bind']
    APP.run(host=bind['host'], port=bind['port'], debug=False)
