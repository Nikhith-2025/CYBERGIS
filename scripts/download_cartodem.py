"""Log in to Bhoonidhi and pre-fetch CartoDEM tiles for an area of interest.

The service fetches missing tiles on demand; this script is for logging in
(the portal needs an email OTP from a person) and for warming the cache.
Log in once: the running service renews the session every hour after that.

Credentials are read from .env (BHOONIDHI_USER_ID, BHOONIDHI_PASSWORD).

Usage:
    python3 scripts/download_cartodem.py login             # mails an OTP and asks for it
    python3 scripts/download_cartodem.py renew             # renew the session now
    python3 scripts/download_cartodem.py fetch [--bbox minx,miny,maxx,maxy]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cartodem_service import bhoonidhi, tiles  # noqa: E402

# Hyderabad plus the tile to its east
DEFAULT_BBOX = '78.2,17.2,79.3,17.6'


def ask_otp(pending):
    """Prompt until the portal accepts an OTP; the portal limits the attempts."""
    print('An OTP has been mailed to your registered email.')
    while True:
        otp = input('OTP (empty to stop): ').strip()
        if not otp:
            sys.exit('No OTP entered; not logged in.')
        try:
            bhoonidhi.portal_login_verify(pending, otp)
            return
        except bhoonidhi.BhoonidhiError as err:
            print(err)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('login', help='log in to the portal (mails an OTP and asks for it)')
    sub.add_parser('renew', help='renew the portal session now (no OTP)')
    fetch_cmd = sub.add_parser('fetch', help='download and prepare tiles for an area')
    fetch_cmd.add_argument('--bbox', default=DEFAULT_BBOX,
                           help=f'minx,miny,maxx,maxy in degrees (default {DEFAULT_BBOX})')
    args = parser.parse_args()

    try:
        if args.command == 'login':
            pending = bhoonidhi.portal_login_start()
            if pending:
                ask_otp(pending)
            print('Logged in. The service renews the session from now on.')
        elif args.command == 'renew':
            bhoonidhi.portal_renew(force=True)
            print('Portal session renewed.')
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
