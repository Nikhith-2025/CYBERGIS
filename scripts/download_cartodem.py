"""Log in to Bhoonidhi and pre-fetch CartoDEM tiles for an area of interest.

The service fetches missing tiles on demand; this script is for logging in
(the portal needs an email OTP from a person) and for warming the cache.

Credentials are read from .env (BHOONIDHI_USER_ID, BHOONIDHI_PASSWORD).

Usage:
    python3 scripts/download_cartodem.py login             # mails you an OTP
    python3 scripts/download_cartodem.py login --otp 123456
    python3 scripts/download_cartodem.py fetch [--bbox minx,miny,maxx,maxy]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartodem_service import bhoonidhi, tiles  # noqa: E402

# Hyderabad plus the tile to its east
DEFAULT_BBOX = '78.2,17.2,79.3,17.6'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    login_cmd = sub.add_parser('login', help='log in to the portal (mails an OTP)')
    login_cmd.add_argument('--otp', help='the 6-digit OTP from your email')
    fetch_cmd = sub.add_parser('fetch', help='download and prepare tiles for an area')
    fetch_cmd.add_argument('--bbox', default=DEFAULT_BBOX,
                           help=f'minx,miny,maxx,maxy in degrees (default {DEFAULT_BBOX})')
    args = parser.parse_args()

    try:
        if args.command == 'login' and args.otp:
            bhoonidhi.portal_login_verify(args.otp)
            print('Logged in. Portal session saved.')
        elif args.command == 'login':
            print(bhoonidhi.portal_login_start())
            print('Now run: python3 scripts/download_cartodem.py login --otp <6-digit code>')
        else:
            bounds = [float(v) for v in args.bbox.split(',')]
            print(f'Tiles for {bounds}: {tiles.tiles_for_bounds(*bounds)}')
            fetched = tiles.ensure_tiles(*bounds)
            print(f'Fetched: {fetched or "nothing, all cached"}')
            bhoonidhi.api_logout()
    except (bhoonidhi.BhoonidhiError, tiles.TileFetchError) as err:
        sys.exit(str(err))


if __name__ == '__main__':
    main()
