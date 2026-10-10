"""Prepare every raw tile under data/raw and rebuild the mosaic.

For tiles added by hand (e.g. downloaded through the Bhoonidhi portal and
unzipped into data/raw/). Tiles fetched by the service are prepared
automatically.

data/raw/**/<tile>.tif  ->  data/cache/<tile>.tif  (COG)
data/cache/*.tif        ->  data/cartodem.vrt      (mosaic served by pygeoapi)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartodem_service.bhoonidhi import RAW_DIR  # noqa: E402
from cartodem_service.tiles import build_mosaic, prepare_tile  # noqa: E402


def main():
    raw_tiles = sorted(RAW_DIR.rglob('*_DEM_30m.tif'))
    if not raw_tiles:
        sys.exit(f'No raw tiles found under {RAW_DIR}')

    print(f'Preparing {len(raw_tiles)} tile(s):')
    for src in raw_tiles:
        dst = prepare_tile(src)
        print(f'  {src.name} -> {dst.name} ({dst.stat().st_size / 1e6:.1f} MB)')
    print(f'Mosaic rebuilt over {build_mosaic()} tile(s)')


if __name__ == '__main__':
    main()
