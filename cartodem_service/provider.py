"""pygeoapi coverage provider for the CartoDEM mosaic.

Extends pygeoapi's RasterioProvider with its own query logic:
- clips with a pixel window instead of a polygon mask, so every pixel that
  touches the requested area is returned with its real value (the base
  provider's mask blanks edge pixels to nodata)
- supports subsetting one axis or both, and bbox in any EPSG CRS
- reports bad requests (subset + bbox together, area outside the data) as
  400 instead of 500
- describes the band as `elevation` in metres, with nodata as null in
  CoverageJSON and grid coordinates at pixel centres
- fetches tiles that are not cached yet from Bhoonidhi before reading
  (see cartodem_service.tiles), and caps the size of a single request
"""
import logging
import math

import numpy as np
from pyproj import CRS, Transformer
import rasterio
from rasterio.io import MemoryFile
from rasterio.windows import Window, bounds as window_bounds, transform as window_transform

from pygeoapi.provider.base import (ProviderGenericError, ProviderInvalidQueryError,
                                    ProviderRequestEntityTooLargeError)
from pygeoapi.provider.rasterio_ import RasterioProvider

from cartodem_service import tiles
from cartodem_service.tiles import EPS

LOGGER = logging.getLogger(__name__)

FIELD = 'elevation'
BAND = 1

# Largest area one request may return, in square degrees (1 deg2 is one tile)
MAX_DEG2 = {'GTiff': 4.0, 'json': 0.1}


class UpstreamUnavailableError(ProviderGenericError):
    """Bhoonidhi could not supply a tile the request needs"""
    ogc_exception_code = 'ServiceUnavailable'
    http_status_code = 503
    default_msg = 'Upstream data source unavailable'


PARAMETER = {
    'type': 'Parameter',
    'description': {'en': 'Surface elevation above the WGS84 ellipsoid'},
    'unit': {
        'label': {'en': 'metre'},
        'symbol': {
            'value': 'm',
            'type': 'http://www.opengis.net/def/uom/UCUM/'
        }
    },
    'observedProperty': {
        'id': 'http://vocab.nerc.ac.uk/standard_name/height_above_reference_ellipsoid/',  # noqa
        'label': {'en': 'Elevation'}
    }
}


class CartoDEMProvider(RasterioProvider):
    """Rasterio provider with CartoDEM band metadata and window-based clipping"""

    def get_fields(self):
        if not self._fields:
            self._fields[FIELD] = {
                'title': 'Elevation',
                'type': 'number',
                'x-ogc-unit': 'm'
            }
        return self._fields

    def query(self, properties=None, subsets=None, bbox=None, bbox_crs=4326,
              datetime_=None, format_='json', **kwargs):
        # `properties` needs no handling: pygeoapi has already checked the
        # names against get_fields(), and `elevation` is the only band.
        # CartoDEM has no time axis, so `datetime_` is ignored too.
        subsets = subsets or {}
        x_axis = self._coverage_properties['x_axis_label']
        y_axis = self._coverage_properties['y_axis_label']
        has_xy_subset = x_axis in subsets or y_axis in subsets

        if bbox and has_xy_subset:
            raise ProviderInvalidQueryError(
                user_msg='bbox and subsetting by coordinates are exclusive')

        with rasterio.open(self.data) as src:
            left, bottom, right, top = src.bounds

            if bbox:
                left, bottom, right, top = self._bbox_to_native(bbox, bbox_crs, src.crs)
            if x_axis in subsets:
                left, right = subsets[x_axis]
            if y_axis in subsets:
                bottom, top = subsets[y_axis]

            window = self._window(src, left, bottom, right, top)
            self._check_size(window, src.res, format_)
            area = window_bounds(window, src.transform)

        self._ensure_tiles(area)

        # Reopen: fetching may have rebuilt the mosaic
        with rasterio.open(self.data) as src:
            data = src.read(BAND, window=window)
            out_transform = window_transform(window, src.transform)
            profile = {
                'driver': 'GTiff',
                'dtype': data.dtype,
                'count': 1,
                'height': data.shape[0],
                'width': data.shape[1],
                'crs': src.crs,
                'transform': out_transform,
                'nodata': src.nodata,
            }

        if format_ == 'json':
            return self._covjson(data, out_transform, profile['nodata'])

        profile.update(self.options or {})
        with MemoryFile() as memfile:
            with memfile.open(**profile) as dst:
                dst.write(data, 1)
            return memfile.read()

    @staticmethod
    def _check_size(window, res, format_):
        kind = 'json' if format_ == 'json' else 'GTiff'
        deg2 = window.width * res[0] * window.height * res[1]
        if deg2 > MAX_DEG2[kind] + EPS:
            hint = ' Use f=GTiff for larger areas.' if kind == 'json' else ''
            raise ProviderRequestEntityTooLargeError(
                user_msg=f'Requested area is {deg2:.3f} square degrees; the limit for '
                         f'{kind} output is {MAX_DEG2[kind]}. Request a smaller subset.'
                         + hint)

    @staticmethod
    def _ensure_tiles(area):
        try:
            fetched = tiles.ensure_tiles(*area)
        except tiles.TileFetchError as err:
            LOGGER.error(err)
            if err.status == 413:
                raise ProviderRequestEntityTooLargeError(user_msg=str(err)) from err
            raise UpstreamUnavailableError(user_msg=str(err)) from err
        if fetched:
            # WARNING is the service's log level, so fetches always show in the log
            LOGGER.warning(f'Fetched from Bhoonidhi: {fetched}')

    @staticmethod
    def _bbox_to_native(bbox, bbox_crs, native_crs):
        minx, miny, maxx, maxy = bbox
        crs_src = CRS.from_user_input(bbox_crs)
        if crs_src == CRS.from_user_input(native_crs):
            return minx, miny, maxx, maxy
        t = Transformer.from_crs(crs_src, native_crs, always_xy=True)
        xs, ys = t.transform([minx, minx, maxx, maxx], [miny, maxy, miny, maxy])
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _window(src, left, bottom, right, top):
        """Pixel window covering every pixel that touches the requested bounds."""
        if left >= right or bottom >= top:
            raise ProviderInvalidQueryError(
                user_msg='Invalid subset: lower bound must be less than upper bound')

        a = src.transform
        col0 = max(0, math.floor((left - a.c) / a.a + EPS))
        col1 = min(src.width, math.ceil((right - a.c) / a.a - EPS))
        row0 = max(0, math.floor((top - a.f) / a.e + EPS))
        row1 = min(src.height, math.ceil((bottom - a.f) / a.e - EPS))

        if col0 >= col1 or row0 >= row1:
            b = src.bounds
            raise ProviderInvalidQueryError(
                user_msg='Requested area is outside the coverage extent '
                f'({b.left:.4f}, {b.bottom:.4f}, {b.right:.4f}, {b.top:.4f})')

        return Window(col0, row0, col1 - col0, row1 - row0)

    def _covjson(self, data, out_transform, nodata):
        height, width = data.shape
        res_x, res_y = out_transform.a, out_transform.e
        # CoverageJSON grid axes are pixel centres
        x0 = out_transform.c + res_x / 2
        y0 = out_transform.f + res_y / 2

        values = data.astype('float64')
        if nodata is not None:
            values[data == nodata] = np.nan

        return {
            'type': 'Coverage',
            'domain': {
                'type': 'Domain',
                'domainType': 'Grid',
                'axes': {
                    'x': {'start': x0, 'stop': x0 + res_x * (width - 1), 'num': width},
                    'y': {'start': y0, 'stop': y0 + res_y * (height - 1), 'num': height}
                },
                'referencing': [{
                    'coordinates': ['x', 'y'],
                    'system': {
                        'type': self._coverage_properties['crs_type'],
                        'id': self._coverage_properties['bbox_crs']
                    }
                }]
            },
            'parameters': {FIELD: PARAMETER},
            'ranges': {
                FIELD: {
                    'type': 'NdArray',
                    'dataType': 'float',
                    'axisNames': ['y', 'x'],
                    'shape': [height, width],
                    'values': [None if math.isnan(v) else v
                               for v in values.ravel().tolist()]
                }
            }
        }
