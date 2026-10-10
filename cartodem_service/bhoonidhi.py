"""Bhoonidhi client: STAC search (documented API) and tile download (portal session).

Search uses the documented API at bhoonidhi-api.nrsc.gov.in. Tokens are cached
on disk and renewed with the refresh grant, so the service doesn't spend one
of the 20 logins per hour on every search.

Download uses the portal's login (password + email OTP), because the
documented /download endpoint rejects API tokens with 401 "Invalid session
detected". The OTP has to be entered by a person, so the portal session is
created with scripts/download_cartodem.py and only reused here.
"""
import fcntl
import json
import os
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

API = 'https://bhoonidhi-api.nrsc.gov.in'
PORTAL = 'https://bhoonidhi.nrsc.gov.in/bhoonidhi'
COLLECTION = 'CartoSat-1_PAN_CartoDEM_30m'

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / 'data'
RAW_DIR = DATA_DIR / 'raw'
LOCK_DIR = DATA_DIR / '.locks'
# Files holding live tokens: owner-only permissions, never committed (data/ is git-ignored)
API_TOKENS_FILE = DATA_DIR / '.api_tokens.json'
PENDING_FILE = DATA_DIR / '.portal_pending.json'
SESSION_FILE = DATA_DIR / '.portal_session.json'

# Renew the access token this many seconds before it expires
TOKEN_MARGIN = 60
# Bhoonidhi allows 3 searches per second per IP; keep well under it
SEARCH_INTERVAL = 0.5
SEARCH_STAMP_FILE = LOCK_DIR / 'last_search'

PORTAL_HEADERS = {
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Content-Type': 'application/json',
    'X-Requested-With': 'XMLHttpRequest',
    'Origin': 'https://bhoonidhi.nrsc.gov.in',
    'Referer': f'{PORTAL}/login.html',
}


class BhoonidhiError(Exception):
    """Bhoonidhi could not be reached or refused the request."""


class SessionExpired(BhoonidhiError):
    """The portal session is missing or expired; a person must log in with an OTP."""


def credentials():
    load_dotenv(ROOT / '.env')
    user = os.getenv('BHOONIDHI_USER_ID')
    password = os.getenv('BHOONIDHI_PASSWORD')
    if not user or not password:
        raise BhoonidhiError('Set BHOONIDHI_USER_ID and BHOONIDHI_PASSWORD in .env')
    return user, password


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


@contextmanager
def file_lock(path):
    """Exclusive lock shared by every process (and gunicorn worker) on this machine."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- Documented API: tokens and STAC search -----------------------------------

def _token_request(body):
    try:
        resp = requests.post(f'{API}/auth/token', json=body, timeout=30)
    except requests.RequestException as err:
        raise BhoonidhiError(f'Bhoonidhi API unreachable: {err}') from err
    if resp.status_code != 200:
        return None
    tokens = resp.json()
    tokens['obtained_at'] = time.time()
    return tokens


def api_access_token():
    """A valid access token, reusing or refreshing the cached one when possible."""
    with file_lock(LOCK_DIR / 'api_tokens'):
        tokens = read_json(API_TOKENS_FILE)
        now = time.time()

        if tokens and now < tokens['obtained_at'] + tokens['expires_in'] - TOKEN_MARGIN:
            return tokens['access_token']

        user, password = credentials()
        if tokens:
            tokens = _token_request({'userId': user, 'refresh_token': tokens['refresh_token'],
                                     'grant_type': 'refresh_token'})
        if not tokens:
            tokens = _token_request({'userId': user, 'password': password,
                                     'grant_type': 'password'})
        if not tokens:
            raise BhoonidhiError('Bhoonidhi API login failed')

        write_private(API_TOKENS_FILE, tokens)
        return tokens['access_token']


def api_logout():
    """Revoke the cached API session (used by the command-line script when it's done)."""
    tokens = read_json(API_TOKENS_FILE)
    if tokens:
        try:
            requests.post(f'{API}/auth/logout',
                          headers={'Authorization': f"Bearer {tokens['refresh_token']}"},
                          timeout=30)
        except requests.RequestException:
            pass
        API_TOKENS_FILE.unlink(missing_ok=True)


def search(ids):
    """STAC search of the CartoDEM collection for the given item ids."""
    body = {'collections': [COLLECTION], 'ids': list(ids), 'limit': len(ids)}

    for attempt in range(2):
        token = api_access_token()
        try:
            resp = _throttled_search(body, token)
        except requests.RequestException as err:
            raise BhoonidhiError(f'Bhoonidhi search unreachable: {err}') from err
        if resp.status_code == 401 and attempt == 0:
            # Cached token was revoked server-side; drop it and log in again
            API_TOKENS_FILE.unlink(missing_ok=True)
            continue
        if resp.status_code == 404 and 'NO RESULTS FOUND' in resp.text:
            # Bhoonidhi answers an empty search with 404 instead of STAC's empty 200
            return {'type': 'FeatureCollection', 'features': []}
        if resp.status_code != 200:
            raise BhoonidhiError(f'Bhoonidhi search failed: HTTP {resp.status_code}')
        try:
            return resp.json()
        except ValueError:
            # Seen under load: HTTP 200 with an empty body. Retry once.
            if attempt == 0:
                time.sleep(1)
                continue
            raise BhoonidhiError('Bhoonidhi search returned an invalid response')
    raise BhoonidhiError('Bhoonidhi search failed: session rejected')


def _throttled_search(body, token):
    """One search at a time across all processes, at most one per SEARCH_INTERVAL."""
    with file_lock(LOCK_DIR / 'search'):
        try:
            wait = float(SEARCH_STAMP_FILE.read_text()) + SEARCH_INTERVAL - time.time()
        except (FileNotFoundError, ValueError):
            wait = 0
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.post(f'{API}/data/search', json=body, timeout=180,
                                 headers={'Authorization': f'Bearer {token}'})
        finally:
            SEARCH_STAMP_FILE.write_text(str(time.time()))


# --- Portal: OTP login and download ------------------------------------------

def _portal_action(payload, encode=False):
    if encode:
        # Matches the portal's encodeObject(): every value is URI-encoded
        payload = {k: quote(str(v), safe='') for k, v in payload.items()}
    resp = requests.post(f'{PORTAL}/LoginServlet', data=json.dumps(payload),
                         headers=PORTAL_HEADERS, timeout=30)
    if resp.status_code == 417:
        raise BhoonidhiError(f'OTP rejected: {resp.text.strip()}')
    if resp.status_code != 200:
        raise BhoonidhiError(f'Portal login failed: HTTP {resp.status_code}')
    try:
        results = resp.json().get('Results') or []
    except ValueError:
        raise BhoonidhiError('Portal login failed: invalid response') from None
    if not results:
        raise BhoonidhiError('Portal login failed: empty response')
    return results[0]


def _save_session(result):
    jwt = result.get('JWT')
    if not jwt or jwt == 'EXCEPTION':
        raise BhoonidhiError(f"Portal login failed: {result.get('MSG') or 'no session token'}")
    write_private(SESSION_FILE, {'jwt': jwt})
    PENDING_FILE.unlink(missing_ok=True)


def portal_login_start():
    """Submit the password. Returns the portal's message; an OTP is usually mailed."""
    user, password = credentials()
    result = _portal_action({'userId': user, 'password': password,
                             'action': 'VALIDATE_LOGIN', 'oldDB': 'false'})
    msg = str(result.get('MSG') or '')
    if msg.startswith('Login OTP has been mailed'):
        write_private(PENDING_FILE, {'pending_token': result.get('pending_token')})
        return msg
    _save_session(result)
    return 'Logged in without OTP'


def portal_login_verify(otp):
    pending = read_json(PENDING_FILE)
    if not pending:
        raise BhoonidhiError('No OTP pending. Start the login first.')
    otp = otp.strip()
    if not (otp.isdigit() and len(otp) == 6):
        raise BhoonidhiError('OTP must be exactly 6 digits.')
    _save_session(_portal_action({'selProds': pending['pending_token'], 'otp': otp,
                                  'action': 'VERIFY_OTP'}, encode=True))


def download_tile(item):
    """Download one CartoDEM tile zip and extract it to data/raw/<item>/.

    Returns the extracted folder. Raises SessionExpired if the portal session
    is missing or no longer accepted.
    """
    target = RAW_DIR / item
    if target.exists():
        return target

    session = read_json(SESSION_FILE)
    if not session:
        raise SessionExpired('No Bhoonidhi portal session')

    url = f'{PORTAL}/data/CARTODEM/P5/PAN/30m/{item}.zip'
    try:
        resp = requests.get(url, params={'token': session['jwt'], 'product_id': item},
                            stream=True, timeout=(10, 300))
    except requests.RequestException as err:
        raise BhoonidhiError(f'Bhoonidhi download unreachable: {err}') from err
    if resp.status_code == 401:
        resp.close()
        raise SessionExpired('Bhoonidhi portal session expired')
    if resp.status_code != 200:
        resp.close()
        raise BhoonidhiError(f'Download of {item} failed: HTTP {resp.status_code}')

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = RAW_DIR / f'{item}.zip'
    part = zip_path.with_name(zip_path.name + '.part')
    with open(part, 'wb') as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            fh.write(chunk)
    part.rename(zip_path)

    # An expired session can come back as HTTP 200 with an error page, not a zip
    if not zipfile.is_zipfile(zip_path):
        zip_path.unlink()
        raise SessionExpired(f'Bhoonidhi returned no data for {item} (session expired?)')

    with zipfile.ZipFile(zip_path) as zf:
        # The zip usually wraps everything in a folder named after the tile already
        wrapped = all(n.startswith(f'{item}/') for n in zf.namelist())
        zf.extractall(RAW_DIR if wrapped else target)
    zip_path.unlink()
    return target
