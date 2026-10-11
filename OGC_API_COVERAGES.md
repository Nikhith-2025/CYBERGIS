# CartoDEM OGC API - Coverages over Bhoonidhi

An **OGC API - Coverages** service for ISRO/NRSC's **CartoDEM 30 m** elevation data, built with
[pygeoapi](https://pygeoapi.io) on top of the [Bhoonidhi](https://bhoonidhi.nrsc.gov.in) data portal.

**Live:** https://cybergis.indiasouthcentral.cloudapp.azure.com

Bhoonidhi only lets you download CartoDEM as whole 1°×1° tiles, after logging in with an emailed OTP. This service
lets anyone get **just the area they need** with one HTTP request, or by drawing a box on the website:

```bash
# Elevation of central Hyderabad (about 11 × 11 km) as a GeoTIFF: one request, no login
curl -o hyderabad.tif \
  "https://cybergis.indiasouthcentral.cloudapp.azure.com/collections/cartodem/coverage?bbox=78.40,17.35,78.50,17.45&f=GTiff"
```

---

## Contents

1. [The gap](#1-the-gap)
2. [How it works](#2-how-it-works)
3. [The data](#3-the-data)
4. [Using the service](#4-using-the-service)
5. [OGC compliance](#5-ogc-compliance)
6. [Performance](#6-performance)
7. [Deployment](#7-deployment)
8. [Running locally](#8-running-locally)
9. [Project layout](#9-project-layout)
10. [Design decisions](#10-design-decisions)
11. [Known limitations](#11-known-limitations)
12. [Bhoonidhi API findings](#12-bhoonidhi-api-findings)
13. [Future work](#13-future-work)
14. [Data credit](#14-data-credit)

---

## 1. The gap

**There is no standards-based way to request part of CartoDEM.** Every route Bhoonidhi offers delivers whole
tiles, and none of them is an OGC coverage interface (checked on 2026-10-11).

| Access route | What it gives you | Subset an area? | OGC coverage interface? |
|---|---|---|---|
| Bhoonidhi web portal | Search, add to cart, download whole tile zips | No | No |
| Bhoonidhi API `/data/search` | STAC catalogue search | No: `bbox` only chooses tiles | No (STAC is for finding files) |
| Bhoonidhi API `/download` | Whole product by id | No | No |
| Portal map view | WMTS tiles with `format=image/jpeg` | No | Map pictures, not elevation values |

**Evidence**

- **No coverage endpoints on Bhoonidhi.** The [API specification](https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/)
  lists only `/auth/token`, `/auth/logout`, `/data/collections`, `/data/search` and `/download`. Requests for `/wcs`,
  `/collections` and `/conformance` on `bhoonidhi-api.nrsc.gov.in` return `404 Route Not Found`, and
  `bhoonidhi.nrsc.gov.in/geoserver/ows?service=WCS` returns 404.
- **Even the catalogue needs a login.** `GET /data/collections/CartoSat-1_PAN_CartoDEM_30m` returns 401 without a token.
- **No data in the STAC items.** A CartoDEM item lists only a `metadata` and a `thumbnail` asset; the elevation data is
  only available as a whole-product download.
- **Bhuvan, ISRO's other geoportal, has WCS switched off.** `bhuvan-vec1…/bhuvan/wcs?request=GetCapabilities` answers
  `"Service WCS is disabled"`; `bhuvan-ras1` returns 403 and `bhuvan-ras2` 404. Its WMS (6,672 layer entries) has no
  national CartoDEM, and WMS returns pictures, not values.
- **The documented API can't download CartoDEM.** With a valid token, `/download` always answers
  `401 "Invalid session detected! Please login."`, while search accepts the same token (every variant tried; see
  [section 12](#12-bhoonidhi-api-findings)). The only working route is the portal's own login, which needs a
  **6-digit OTP emailed to the account holder**. The most complete community client,
  [`bhoonidhi-downloader`](https://pypi.org/project/bhoonidhi-downloader/), uses that portal login for the same reason.

**Why STAC isn't enough.** Bhoonidhi's search follows STAC, an OGC Community Standard, but STAC only says *which
tile* covers an area. It has no way to ask for *the elevation values inside an area*: no spatial subsetting, no field
selection, no output format. That is what OGC API - Coverages (and WCS before it) provides.

**Who this affects.** Anyone who needs elevation for an area rather than a tile (a watershed model, a terrain
profile in an app, heights along a road) has to log in with an OTP, download whole tiles, merge and clip them. None of
it can be automated, because the OTP needs a person.

| | Bhoonidhi today | This service |
|---|---|---|
| Elevation for an 11 km area | OTP login → search → cart → download a 44.5 MB zip → unzip 52 MB → clip yourself | **One request**, about 330 KB |
| Machine-to-machine access | Not possible | Any HTTP client |
| Standard | STAC search only | OGC API - Coverages |

---

## 2. How it works

```
 client (browser, curl, Python, GIS)
        │  HTTPS
        ▼
 nginx ── cache, rate limit, gzip, failover
        │
        ▼
 2 replicas: pygeoapi + CartoDEMProvider (gunicorn)
        │  1. requested area → pixel window on a fixed India grid
        │  2. size check (413 if too large)
        │  3. make sure the 1° tiles under it are cached ─────▶ Bhoonidhi
        │       (search the catalogue, download, convert)       (new tiles only)
        │  4. read the window from the mosaic
        ▼  5. return GeoTIFF or CoverageJSON
 data/: tile cache (Cloud-Optimized GeoTIFFs) + VRT mosaic, shared by both replicas
```

### 2.1 Serving a request

1. pygeoapi validates the OGC parameters (`bbox`, `subset`, `properties`, `f`).
2. `CartoDEMProvider` turns the area into a **pixel window**, including every pixel that touches it.
3. Requests over the size limits get **413** (see [2.4](#24-limits)).
4. Every 1° tile under the window is made available, fetching it from Bhoonidhi if needed ([2.2](#22-fetching-tiles-from-bhoonidhi)).
5. The window is read from the VRT mosaic; only the needed 512×512 blocks of each tile are read from disk.
6. The pixels are returned as GeoTIFF (DEFLATE compressed) or CoverageJSON (nodata as `null`).

Returned pixels are **bit-identical** to the source data; the compliance tests compare them with a GDAL clip.

### 2.2 Fetching tiles from Bhoonidhi

The service doesn't keep a full copy of CartoDEM. It **fetches tiles the first time an area is requested** and
keeps them, so Bhoonidhi stays the source and only areas people use take up disk.

```
area ─▶ which 1° tiles? ─▶ all cached (or known to be sea)? ── yes ─▶ serve
                                     │ no
                                     ▼
                       more than 4 missing? ── yes ─▶ 413
                                     │ no
                                     ▼
          lock the missing tiles ─▶ search Bhoonidhi's catalogue ─▶ download ─▶ convert ─▶ add to mosaic ─▶ serve
                                     │
                                     ├─ not in the catalogue (open sea) ─▶ remembered, served as nodata
                                     └─ not online at Bhoonidhi          ─▶ 503 (must be ordered in the portal)
```

| Situation | Result (measured) |
|---|---|
| First request for a new area | 200 in about **10–13 s** (tile fetched once) |
| The same area again | 200 in about **15 ms** |
| 6 users asking for the same new tile at once | **one** download; all 6 get identical data |
| An area with no tile (Bay of Bengal) | 200, all nodata; repeats in 7 ms with no Bhoonidhi call |

**Concurrency.** Each tile has a file lock shared by all threads, processes and replicas, so a tile is downloaded
once. Locks are taken in sorted order (no deadlocks), before the catalogue search (waiting requests don't search
again), and the mosaic is rebuilt before they're released (a waiting request never reads an incomplete mosaic).
Files are written to temporary names and renamed into place, so nothing half-written is ever read.

### 2.3 Talking to Bhoonidhi

| Purpose | Route | Authentication |
|---|---|---|
| Catalogue search | Documented API `POST /data/search` | Password login; tokens are **cached and refreshed** (access 20 min, refresh 5 h), so a search doesn't use one of the 20 logins per hour |
| Tile download | Portal `GET /bhoonidhi/data/CARTODEM/P5/PAN/30m/<id>.zip?token=…&product_id=<id>` | Portal login with a **6-digit OTP emailed to the account holder**, once; the session lasts 6 hours and is tied to the server's IP, so the service **renews it every hour** without an OTP |

- **Within Bhoonidhi's limits:** searches are spaced at least 0.5 s apart across all processes (Bhoonidhi allows 3 per
  second), and a request can fetch at most 4 new tiles (Bhoonidhi allows 5 concurrent downloads).
- **Session renewal:** each gunicorn worker runs a background thread that checks every 5 minutes and renews the
  portal session once it is an hour old (`VALIDATE_SESSION`). A shared lock and timestamp mean only one process
  renews. A session the portal refuses to renew is marked expired, and new tiles return 503 until the next login.
- **Retries:** an empty reply (seen under load) is retried once, then reported as 503.
- **Secrets:** credentials come from `.env`; tokens and the session are kept in `data/` with owner-only permissions.
  Neither is ever committed.

### 2.4 Limits

| Limit | Value | Why |
|---|---|---|
| New tiles fetched per request | **4** | Covers any city or district; stays under Bhoonidhi's 5 concurrent downloads and daily quota |
| Largest GeoTIFF | **4 square degrees** (about 4 tiles) | Caps memory per request (about 200 MB at most) |
| Largest CoverageJSON | **0.1 square degrees** | CoverageJSON is text; 0.04 deg² is already about 18 MB |

A request with no area asks for the whole coverage (960 square degrees) and gets a 413 explaining the limits.

---

## 3. The data

| Property | Value (from Bhoonidhi's item metadata and the files) |
|---|---|
| Bhoonidhi collection | `CartoSat-1_PAN_CartoDEM_30m` |
| Source | Cartosat-1 (`P5`) panchromatic stereo imagery, produced by NRSC, Version 4.0 |
| Resolution | 1 arc-second (about 30 m) |
| Accuracy | 8 m vertical (LE90), 15 m horizontal (CE90) |
| Coordinates | WGS84 longitude/latitude (EPSG:4326) |
| Tiles | 1°×1°, 3600 × 3600 pixels, ids like `P5_PAN_CD_N17_000_E078_000_30m` |
| Values | Float32 metres, nodata −32768 |
| Served extent | 68–98°E, 6–38°N (India and surroundings) |

- **Heights are above the WGS84 ellipsoid, not mean sea level.** Values sit 50–90 m below familiar heights (Delhi
  about 163 m, Bengaluru about 821 m, Hyderabad about 460 m), matching the geoid–ellipsoid gap over India.
- **It is a surface model** from optical imagery, so heights include buildings and trees.

**Processing.** Each downloaded tile (a 44.5 MB zip holding a 52 MB GeoTIFF) is converted, losslessly, into a
**Cloud-Optimized GeoTIFF**: 512×512 internal tiles, DEFLATE compression and overviews, about 40 MB. A GDAL **VRT
mosaic** (a few KB of XML) lists all cached tiles on one fixed grid covering the whole extent, so every request maps
to the same pixel grid whether or not its tiles are cached yet.

---

## 4. Using the service

### 4.1 The website

The home page is an **area picker**: search for a place (e.g. "Kurnool") or press **Draw area** and click two
corners. It shows the box size, checks it against the limits, and offers **Download GeoTIFF**, **View on map**
(terrain colours) and the exact request URL to copy. It only builds the standard request below; it has no API of its
own. Place search uses OpenStreetMap's Nominatim.

The other pages (collections, collection details, schema, conformance and the Swagger API documentation) let you
browse the service. Every endpoint also returns HTML with `?f=html`.

### 4.2 Endpoints

| Endpoint | Returns |
|---|---|
| `/` | Landing page, with links to everything below |
| `/conformance` | The conformance classes implemented |
| `/openapi` | OpenAPI 3.0 description (`?f=json`); Swagger UI in a browser |
| `/collections` | The collections (one: `cartodem`) |
| `/collections/cartodem` | Collection metadata and extent |
| `/collections/cartodem/schema` | The field: `elevation`, in metres |
| `/collections/cartodem/coverage` | **The coverage**, with the parameters below |

### 4.3 Coverage parameters

| Parameter | Example | Meaning |
|---|---|---|
| `bbox` | `bbox=78.40,17.35,78.50,17.45` | Area as `minLon,minLat,maxLon,maxLat` in degrees |
| `subset` | `subset=Lat(17.35:17.45),Long(78.40:78.50)` | Area as a range on one or both axes; can't be combined with `bbox` |
| `bbox-crs` | `bbox-crs=http://www.opengis.net/def/crs/EPSG/0/32644` | CRS of `bbox`, if not WGS84 longitude/latitude |
| `properties` | `properties=elevation` | Field to return (the only one is `elevation`) |
| `f` | `f=GTiff`, `f=json`, `f=html` | GeoTIFF, CoverageJSON (default) or a map view |
| `Accept` header | `Accept: image/tiff` | Same as `f=GTiff` |

### 4.4 Examples

```bash
B=https://cybergis.indiasouthcentral.cloudapp.azure.com/collections/cartodem/coverage

curl -o hyd.tif "$B?bbox=78.40,17.35,78.50,17.45&f=GTiff"                       # GeoTIFF by bbox
curl -o hyd.tif "$B?subset=Lat(17.35:17.45),Long(78.40:78.50)&f=GTiff"          # same area by subset
curl "$B?bbox=78.40,17.40,78.41,17.41&properties=elevation&f=json"              # CoverageJSON values
curl -H "Accept: image/tiff" -o hyd.tif "$B?bbox=78.40,17.35,78.50,17.45"       # GeoTIFF by Accept header
```

In Python (download first: responses are generated per request, so GDAL can't open the URL with range requests):

```python
import requests
from rasterio.io import MemoryFile

url = ("https://cybergis.indiasouthcentral.cloudapp.azure.com/collections/cartodem/coverage"
       "?bbox=78.40,17.35,78.50,17.45&f=GTiff")
resp = requests.get(url, timeout=120)
resp.raise_for_status()
with MemoryFile(resp.content) as mem, mem.open() as src:
    dem = src.read(1, masked=True)
    print(dem.shape, dem.min(), dem.max())
```

### 4.5 Responses

| Status | When |
|---|---|
| `200` | The area, as `image/tiff` or `application/prs.coverage+json`. Areas with no tile (open sea) are nodata |
| `400` | Malformed `bbox`/`subset`, unknown axis or property, area outside the extent, `bbox` together with `subset` |
| `404` | Unknown collection |
| `413` | Too large: over the area limit, or more than 4 new tiles needed |
| `429` | Too many requests from one IP (over 20 per second) |
| `503` | A new tile can't be fetched right now (Bhoonidhi unavailable, or the operator's session has expired) |

Errors are OGC exception documents with a `description` explaining the problem.

---

## 5. OGC compliance

Implemented conformance classes (OGC API - Coverages Part 1):
`core` · `oas30` · `html` · `geodata-coverage` · `coverage-subset` · `coverage-rangesubset` · `coverage-bbox` · `coverage-datetime`

`tests/compliance_test.py` checks every class against a running server with real requests and verifies returned
GeoTIFFs with GDAL. **Result: 39/39 checks pass**, on the live server and locally.

| Class | Checks | What is verified |
|---|---|---|
| core | 16 | Landing page and its links, `/conformance`, `/collections`, collection extent, coverage returned as a CoverageJSON grid, `elevation` field in metres, schema |
| geodata-coverage | 2 | Collections link to their coverage |
| coverage-subset | 7 | Bounds match the request; **pixels identical to a GDAL clip of the source**; single-axis subset; bad axis, bad syntax and out-of-extent requests give 4xx |
| coverage-bbox | 4 | `bbox` gives the same result as the equivalent `subset`; invalid combinations give 4xx |
| coverage-rangesubset | 2 | `properties=elevation` works; an unknown field gives 400 |
| coverage-datetime | 1 | `datetime` is handled (CartoDEM has no time axis) |
| oas30 | 2 | Valid OpenAPI 3.x document describing the coverage endpoint |
| html | 3 | Landing page, collections and collection as HTML |
| general | 2 | Unknown collection gives 404; `Accept: image/tiff` gives GeoTIFF |

**What had to be fixed.** pygeoapi's stock raster provider failed 6 of these checks. The custom provider and a small
middleware fix them:

| Problem | Fix |
|---|---|
| Clipping with a polygon mask blanked the outer row and column to nodata | Read a pixel window instead; pixels now match GDAL exactly |
| A subset on one axis only, an area outside the extent, or `bbox` with `subset` gave 500 | Handled properly: results or 400 |
| A malformed `bbox` gave 500 | Middleware validates it and answers 400 |
| `Accept: image/tiff` was ignored | Middleware maps it to `f=GTiff` |
| The band was unnamed (`null`) with no units | Named `elevation`, in metres |
| A request with no parameters returned the mosaic's XML | Returns a GeoTIFF, or 413 if too large |

pygeoapi's generated OpenAPI document also described the coverage endpoint as a Features endpoint (GeoJSON, "selects
features", no GeoTIFF). `scripts/patch_openapi.py` corrects that description; the result validates as OpenAPI 3.0.

---

## 6. Performance

Load-tested with [k6](https://k6.io) at **100 simultaneous users** (target: p95 under 500 ms, under 1% errors), with
random areas inside cached tiles so the test measures the service, not Bhoonidhi. The service and k6 shared one
16-core machine.

| Scenario | Flask dev server (1 process) | gunicorn (8 processes × 4 threads) |
|---|---|---|
| mixed: 70% GeoTIFF, 15% CoverageJSON, 15% pages | 107 req/s, p95 1.05 s ✗ | **500 req/s, p95 289 ms** ✓ |
| GeoTIFF, 5–15 km areas | 168 req/s, p95 631 ms ✗ | **478 req/s, p95 319 ms** ✓ |
| CoverageJSON, 2–5 km areas | 55 req/s, p95 2.07 s ✗ | **394 req/s, p95 369 ms** ✓ |

383,066 requests in total, with no errors. Summaries are in `loadtest/results/`.

- The single development-server process was the bottleneck; even plain pages waited about 1 s under load.
- **New tiles don't slow anyone else down.** gunicorn uses threaded workers, so a request waiting on a Bhoonidhi
  download holds one thread. During a 19.8 s download in the middle of a 100-user test, other requests' p95 moved
  from 340 ms to 349 ms (`loadtest/results/cold-fetch-under-load.md`).

```bash
k6 run -e SCENARIO=mixed loadtest/coverage.js      # scenarios: mixed, geotiff, covjson, hot
k6 run -e BASE=https://<host> -e SCENARIO=mixed loadtest/coverage.js
```

---

## 7. Deployment

The live service runs on an Azure VM (Ubuntu 24.04, 2 CPUs, India South Central) with Docker Compose:

```
Internet ──HTTPS──▶ nginx ──┬──▶ app1 (gunicorn) ──┐
                            └──▶ app2 (gunicorn) ──┴──▶ data/ (tiles, mosaic, Bhoonidhi session)
```

| Feature | Setting |
|---|---|
| Replicas | Two identical service containers, health-checked every 15 s and restarted if they exit |
| Failover | nginx skips a replica that fails; requests to a crashed replica are retried on the other |
| Cache | Coverage responses cached for 24 h (`X-Cache-Status: HIT`); pages and metadata are always fresh |
| Rate limit | 20 requests/s per IP (bursts of 40), then 429 |
| HTTPS | Let's Encrypt certificate, HTTP redirected to HTTPS, HSTS, automatic renewal |
| Timeouts | 120 s for first requests that fetch a tile |

A VM is used rather than a managed platform because the Bhoonidhi session needs a **fixed outbound IP** and the tile
cache needs a **persistent disk**. India South Central keeps it close to Bhoonidhi.

**Deploying on a server** (Ubuntu with Docker, fixed public IP):

```bash
# Copy the project (without .venv, data and .env) to the server, then:
cd CYBERGIS
cp .env.example .env && nano .env                 # Bhoonidhi user ID and password
echo "PYGEOAPI_SERVER_URL=http://<address>" >> .env
docker compose up -d --build

# Bhoonidhi login, on the server, once: an OTP is emailed and asked for.
# The running service renews the session from then on.
docker compose run --rm app1 python scripts/download_cartodem.py login
```

**Adding HTTPS** (needs a domain name; on Azure, a free DNS name label on the public IP gives
`<label>.<region>.cloudapp.azure.com`):

```bash
docker compose -f docker-compose.yml -f docker-compose.https.yml run --rm --entrypoint certbot certbot \
  certonly --webroot -w /var/www/certbot --cert-name coverage -d <domain> \
  --agree-tos --register-unsafely-without-email
sed -i 's#^PYGEOAPI_SERVER_URL=.*#PYGEOAPI_SERVER_URL=https://<domain>#' .env
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
```

The certbot container renews the certificate when it is within 30 days of expiry, and nginx reloads every 6 hours
to use it.

---

## 8. Running locally

Requirements: Linux or macOS, Python 3.10+, GDAL command-line tools 3.1+ (with the COG driver), and a
[Bhoonidhi account](https://bhoonidhi.nrsc.gov.in/bhoonidhi/login.html) for fetching new tiles.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                                          # Bhoonidhi user ID and password

.venv/bin/python scripts/download_cartodem.py login           # once: OTP is emailed and asked for
.venv/bin/python scripts/download_cartodem.py fetch           # optional: pre-fetch Hyderabad

scripts/serve.sh                                              # http://localhost:5000 (DEV=1 for Flask's dev server)
.venv/bin/python tests/compliance_test.py                     # 39 compliance checks
```

Tiles downloaded by hand from the portal can be unzipped into `data/raw/` and prepared with
`scripts/prepare_tiles.py`. The Docker stack also runs locally:
`HTTP_PORT=8080 PYGEOAPI_SERVER_URL=http://localhost:8080 docker compose up -d`.

---

## 9. Project layout

```
cartodem_service/
  app.py              WSGI app: pygeoapi + middleware (bbox validation, Accept: image/tiff)
  provider.py         CartoDEMProvider: pixel windows, size limits, CoverageJSON, tile fetching
  tiles.py            Tile cache: which tiles, locks, conversion, mosaic
  bhoonidhi.py        Bhoonidhi client: catalogue search, OTP login, session renewal, tile download
scripts/
  download_cartodem.py  OTP login, session renewal and pre-fetching
  prepare_tiles.py      Prepare tiles added by hand
  patch_openapi.py      Correct the coverage endpoint's OpenAPI description
  serve.sh              Start the service locally
tests/compliance_test.py  The 39 compliance checks
loadtest/               k6 load test and results
theme/templates/        Web pages: base layout, area picker, collections, conformance, Swagger
deploy/nginx/           nginx: shared settings, request handling, HTTP and HTTPS servers
Dockerfile              Service image (Python, GDAL tools, gunicorn)
docker-compose.yml      nginx + two replicas
docker-compose.https.yml  HTTPS: port 443 and certificate renewal
gunicorn.conf.py        Server settings (workers, threads, timeout)
pygeoapi-config.yml     Service and collection configuration
.env.example            Template for Bhoonidhi credentials
data/                   (not committed) tiles, mosaic, session
```

---

## 10. Design decisions

| Decision | Why |
|---|---|
| **OGC API - Coverages** rather than WCS 2.0 | The current OGC successor to WCS: same purpose, with REST/JSON, an OpenAPI description and HTML pages |
| **pygeoapi** with a **custom provider** | pygeoapi implements the standard; the custom provider fixes its raster clipping and is where tile fetching plugs in |
| **Fetch tiles on demand, then cache** | Keeps Bhoonidhi as the source without depending on it for every request; proxying each request would exceed Bhoonidhi's login and download limits at once |
| **Synchronous fetching** | Standard clients expect data in the response; the cost is a slow first request per new tile |
| **Portal download with OTP, then renewal** | The documented `/download` endpoint rejects every token; renewing the portal session keeps the one OTP login working |
| **Cloud-Optimized GeoTIFFs + a VRT mosaic** | A small request reads only a few blocks; adding a tile rewrites a few KB, not a whole mosaic |
| **gunicorn with threads** | Several processes for throughput; threads so a request waiting on Bhoonidhi holds one thread, not a whole process |
| **nginx + two replicas on one VM** | Caching, rate limiting, HTTPS and failover without changing the service; a VM for a fixed IP and persistent disk |

---

## 11. Known limitations

- **New tiles need one OTP login per outage.** The service renews the session while it runs, but if it is down for
  more than 6 hours the session expires. Cached areas still work; new areas return 503 until the operator logs in
  again.
- **The session is tied to the server's IP.** It must be created on the machine that runs the service.
- **The download route isn't in Bhoonidhi's API specification.** It is the portal's own route and could change.
- **First requests for new tiles are slow** (about 10–13 s, longer when Bhoonidhi is busy).
- **Size limits:** at most 4 new tiles, 4 deg² of GeoTIFF or 0.1 deg² of CoverageJSON per request.
- **Heights are ellipsoidal (WGS84),** not mean sea level.
- **No time axis:** `datetime` is accepted and ignored. **No output scaling or reprojection:** results come on the
  native 1 arc-second grid in EPSG:4326 (`bbox-crs` only changes how the request box is given).
- **Tiles not online at Bhoonidhi** have to be ordered in the portal first.
- **One server:** the replicas share one disk; more servers would need shared storage.
- **Caches aren't cleared automatically:** coverage responses stay in nginx's cache for 24 h, tiles are never
  evicted (about 40 MB each), and tiles found to be sea are remembered in `data/cache/absent.json`.
- **The web pages use third-party assets** (Bootstrap, Google Fonts, map libraries, Esri basemap, Nominatim). The API
  doesn't depend on them.

---

## 12. Bhoonidhi API findings

| Finding | Detail |
|---|---|
| `/download` rejects every token | Always `401 "Invalid session detected! Please login."` (the specification's wording is "…Please login **again**", so an undocumented check is failing). Tried: fresh and refreshed tokens, without `Bearer`, as a query parameter, POST instead of GET, with the portal logged out, and with an OTP-verified portal token |
| Portal downloads need an OTP | `LoginServlet`: password → OTP emailed → session token valid for 6 hours, tied to the client's IP |
| Portal sessions can be renewed | `LoginServlet` with `{"action": "VALIDATE_SESSION"}` and the token in a `token` header returns a new session token, no OTP. An expired token gets HTTP 200 with an empty `JWT` |
| An empty search returns 404 | `404 "NO RESULTS FOUND"` instead of STAC's `200` with no features |
| Search can return an empty reply under load | HTTP 200 with an empty body when several searches arrive together |
| The catalogue needs a login | Even `GET /data/collections/{id}` returns 401 |
| No data asset in STAC items | CartoDEM items have only `metadata` and `thumbnail` |
| Specification inconsistencies | The logout link points to `/auth/token`; inputs are said to go in a POST body while download is GET; error keys appear as both `errorCode` and `ErrorCode` |

---

## 13. Future work

- **Background fetching:** answer a request for a new area with "retry shortly" and download in the background, so no
  request waits on Bhoonidhi.
- **More than one server:** tiles in object storage (S3, R2 or GCS), shared by several servers.
- **More of the standard:** the `scaling` and `crs` conformance classes.
- **More data:** CartoDEM at 10 m and 2.5 m (referenced in the portal's code) and other Bhoonidhi raster collections.

---

## 14. Data credit

CartoDEM is produced by the **National Remote Sensing Centre (NRSC), ISRO** and distributed through
[Bhoonidhi](https://bhoonidhi.nrsc.gov.in). This service serves subsets of it unmodified apart from lossless
recompression. Users should follow Bhoonidhi's terms of use for the data.
