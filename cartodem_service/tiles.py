"""CartoDEM tile cache: fetch missing tiles from Bhoonidhi on demand.

data/raw/<item>/<tile>.tif  ->  data/cache/<tile>.tif  (Cloud-Optimized GeoTIFF)
data/cache/*.tif            ->  data/cartodem.vrt      (mosaic on a fixed India grid)

The mosaic always spans the full CartoDEM extent, so the service can accept
requests anywhere in it; areas whose tiles are not cached yet read as nodata
until ensure_tiles() fetches them.
"""
import json
import math
import os
import subprocess
from contextlib import ExitStack

import rasterio

from cartodem_service import bhoonidhi
from cartodem_service.bhoonidhi import DATA_DIR, LOCK_DIR, file_lock

CACHE_DIR = DATA_DIR / 'cache'
VRT_PATH = DATA_DIR / 'cartodem.vrt'
ABSENT_FILE = CACHE_DIR / 'absent.json'

# CartoDEM coverage of India and surroundings, in whole-degree tiles
EXTENT = (68, 6, 98, 38)
RES = 1 / 3600
NODATA = -32768
EXPECTED_EPSG = 4326
# Most uncached tiles one request may trigger downloading
MAX_FETCH = 4
# Tolerance for float noise when snapping bounds to the grid
EPS = 1e-9


class TileFetchError(Exception):
    """A tile could not be fetched; `status` is the HTTP status to report."""

    def __init__(self, msg, status=503):
        super().__init__(msg)
        self.status = status


def item_id(lat, lon):
    return f'P5_PAN_CD_N{lat:02d}_000_E{lon:03d}_000_30m'


def cache_path(item):
    return CACHE_DIR / f"{item.replace('_30m', '_DEM_30m')}.tif"


def tiles_for_bounds(left, bottom, right, top):
    """Item ids of every 1-degree tile touching the bounds, within EXTENT."""
    x0, y0, x1, y1 = EXTENT
    lons = range(max(x0, math.floor(left + EPS)), min(x1, math.ceil(right - EPS)))
    lats = range(max(y0, math.floor(bottom + EPS)), min(y1, math.ceil(top - EPS)))
    return [item_id(lat, lon) for lat in lats for lon in lons]


# --- Preparing tiles ----------------------------------------------------------

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout


def check_tile(src):
    """Confirm the raw tile is a single-band EPSG:4326 raster."""
    with rasterio.open(src) as ds:
        epsg = ds.crs.to_epsg() if ds.crs else None
        if epsg != EXPECTED_EPSG:
            raise ValueError(f'{src.name}: expected EPSG:{EXPECTED_EPSG}, got {epsg}')
        if ds.count != 1:
            raise ValueError(f'{src.name}: expected 1 band, got {ds.count}')


def prepare_tile(src):
    """Convert one raw tile to a COG in the cache. Returns the cached path."""
    dst = CACHE_DIR / src.name
    if dst.exists():
        return dst

    check_tile(src)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + '.part')
    run([
        'gdal_translate', str(src), str(tmp),
        '-of', 'COG',
        '-a_nodata', str(NODATA),
        '-co', 'COMPRESS=DEFLATE',
        '-co', 'PREDICTOR=FLOATING_POINT',
        '-co', 'BLOCKSIZE=512',
        '-co', 'OVERVIEWS=AUTO',
        '-co', 'OVERVIEW_RESAMPLING=AVERAGE',
        '-co', 'NUM_THREADS=ALL_CPUS',
    ])
    os.replace(tmp, dst)
    return dst


def build_mosaic():
    """Rebuild the VRT over every cached tile, on the fixed full-extent grid.

    Written to a temporary file and swapped in, so requests in flight keep
    reading a complete mosaic.
    """
    with file_lock(LOCK_DIR / 'mosaic'):
        tiles = sorted(CACHE_DIR.glob('*.tif'))
        if not tiles:
            raise RuntimeError('No cached tiles to mosaic')
        tmp = VRT_PATH.with_name(VRT_PATH.name + '.tmp')
        run(['gdalbuildvrt', '-q',
             '-te', *map(str, EXTENT),
             '-tr', repr(RES), repr(RES),
             '-srcnodata', str(NODATA), '-vrtnodata', str(NODATA),
             str(tmp)] + [str(t) for t in tiles])
        os.replace(tmp, VRT_PATH)
        return len(tiles)


# --- Fetching on demand ---------------------------------------------------------

def _absent():
    return set(bhoonidhi.read_json(ABSENT_FILE) or [])


def _mark_absent(items):
    with file_lock(LOCK_DIR / 'absent'):
        known = _absent() | set(items)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = ABSENT_FILE.with_name(ABSENT_FILE.name + '.tmp')
        tmp.write_text(json.dumps(sorted(known)))
        os.replace(tmp, ABSENT_FILE)


def fetch_tile(item, online):
    """Download and prepare one tile. Caller must hold the tile's lock."""
    if online != 'Y':
        raise TileFetchError(f'{item} is not online at Bhoonidhi; it must be ordered '
                             'through the portal first')
    folder = bhoonidhi.download_tile(item)
    src = next(folder.rglob('*_DEM_30m.tif'), None)
    if src is None:
        raise TileFetchError(f'{item}: download contained no DEM GeoTIFF')
    prepare_tile(src)


def _missing(items):
    absent = _absent()
    return [t for t in items if t not in absent and not cache_path(t).exists()]


def ensure_tiles(left, bottom, right, top):
    """Make sure every tile touching the bounds is cached. Returns ids fetched.

    Locks every missing tile before searching, so concurrent requests for the
    same area wait for one fetch instead of each searching Bhoonidhi. The
    mosaic is rebuilt before the locks are released, so a request that waited
    and then finds a tile cached also finds it in the mosaic.
    """
    missing = _missing(tiles_for_bounds(left, bottom, right, top))
    if not missing:
        return []
    if len(missing) > MAX_FETCH:
        raise TileFetchError(
            f'Request needs {len(missing)} tiles that are not cached yet; at most '
            f'{MAX_FETCH} can be fetched per request. Request a smaller area.', status=413)

    with ExitStack() as locks:
        # Sorted, so two requests needing overlapping tiles can't deadlock
        for item in sorted(missing):
            locks.enter_context(file_lock(LOCK_DIR / f'tile_{item}'))

        missing = _missing(missing)
        if not missing:
            return []

        try:
            found = {f['id']: f.get('properties', {}).get('Online')
                     for f in bhoonidhi.search(ids=missing).get('features', [])}
        except bhoonidhi.BhoonidhiError as err:
            raise TileFetchError(f'Bhoonidhi catalogue unavailable: {err}') from err

        # Tiles the catalogue doesn't have (open sea, outside coverage) stay nodata
        _mark_absent(set(missing) - set(found))

        fetched = []
        try:
            for item, online in found.items():
                fetch_tile(item, online)
                fetched.append(item)
        except bhoonidhi.SessionExpired as err:
            raise TileFetchError(f'{err}. The service operator must log in again with an '
                                 'OTP: python3 scripts/download_cartodem.py login') from err
        except bhoonidhi.BhoonidhiError as err:
            raise TileFetchError(str(err)) from err
        finally:
            if fetched:
                build_mosaic()
        return fetched
