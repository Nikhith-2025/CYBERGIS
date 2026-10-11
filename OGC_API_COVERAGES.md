# CartoDEM OGC API - Coverages over Bhoonidhi

This document describes an **OGC API - Coverages** service for the **CartoDEM 30 m** elevation data of ISRO/NRSC,
built with [pygeoapi](https://pygeoapi.io) on top of the [Bhoonidhi](https://bhoonidhi.nrsc.gov.in) data portal.

**Live service:** https://cybergis.indiasouthcentral.cloudapp.azure.com

Bhoonidhi distributes CartoDEM only as complete 1°×1° tiles, and downloading them requires a login verified by an
emailed one-time password (OTP). This service allows any client to retrieve **only the area it requires** through a
single HTTP request, or by selecting an area on the website:

```bash
# Elevation of central Hyderabad (approximately 11 × 11 km) as a GeoTIFF: one request, no login
curl -o hyderabad.tif \
  "https://cybergis.indiasouthcentral.cloudapp.azure.com/collections/cartodem/coverage?bbox=78.40,17.35,78.50,17.45&f=GTiff"
```

---

## Contents

1. [Problem Statement](#1-problem-statement)
2. [System Architecture](#2-system-architecture)
3. [Dataset](#3-dataset)
4. [Service Usage](#4-service-usage)
5. [OGC Compliance](#5-ogc-compliance)
6. [Performance Evaluation](#6-performance-evaluation)
7. [Deployment](#7-deployment)
8. [Local Installation](#8-local-installation)
9. [Project Structure](#9-project-structure)
10. [Design Decisions](#10-design-decisions)
11. [Known Limitations](#11-known-limitations)
12. [Bhoonidhi API Findings](#12-bhoonidhi-api-findings)
13. [Future Work](#13-future-work)
14. [Data Attribution](#14-data-attribution)

---

## 1. Problem Statement

**No standards-based method exists for requesting a portion of CartoDEM.** Every access route offered by Bhoonidhi
delivers complete tiles, and none of them is an OGC coverage interface (verified on 2026-10-11).

| Access route | Output | Spatial subsetting | OGC coverage interface |
|---|---|---|---|
| Bhoonidhi web portal | Search, cart and download of complete tile archives | No | No |
| Bhoonidhi API `/data/search` | STAC catalogue search | No; `bbox` only selects tiles | No; STAC is a discovery standard |
| Bhoonidhi API `/download` | Complete product by identifier | No | No |
| Portal map view | WMTS tiles with `format=image/jpeg` | No | No; map images, not elevation values |

**Evidence**

- **Bhoonidhi exposes no coverage endpoints.** The [API specification](https://bhoonidhi.nrsc.gov.in/bhoonidhi-api/)
  lists only `/auth/token`, `/auth/logout`, `/data/collections`, `/data/search` and `/download`. Requests for `/wcs`,
  `/collections` and `/conformance` on `bhoonidhi-api.nrsc.gov.in` return `404 Route Not Found`, and
  `bhoonidhi.nrsc.gov.in/geoserver/ows?service=WCS` returns 404.
- **The catalogue itself requires authentication.** `GET /data/collections/CartoSat-1_PAN_CartoDEM_30m` returns 401
  without a token.
- **The STAC items contain no data assets.** A CartoDEM item lists only a `metadata` and a `thumbnail` asset; the
  elevation data is available solely as a complete product download.
- **WCS is disabled on Bhuvan, the other ISRO geoportal.** `bhuvan-vec1…/bhuvan/wcs?request=GetCapabilities` responds
  with `"Service WCS is disabled"`; `bhuvan-ras1` returns 403 and `bhuvan-ras2` returns 404. Its WMS (6,672 layer
  entries) contains no national CartoDEM layer, and WMS returns rendered images rather than values.
- **The documented API cannot download CartoDEM.** With a valid token, `/download` always responds with
  `401 "Invalid session detected! Please login."`, whereas search accepts the same token (all variants tested; see
  [Section 12](#12-bhoonidhi-api-findings)). The only working route is the portal login, which requires a
  **6-digit OTP emailed to the account holder**. The most complete community client,
  [`bhoonidhi-downloader`](https://pypi.org/project/bhoonidhi-downloader/), relies on the same portal login for this
  reason.

**Limitations of STAC.** Bhoonidhi search follows STAC, an OGC Community Standard; however, STAC identifies only
*which tile* covers an area. It provides no means of requesting *the elevation values within an area*: there is no
spatial subsetting, no field selection and no choice of output format. These capabilities are provided by OGC API -
Coverages, as they were by WCS before it.

**Affected users.** Any user who requires elevation for an area rather than a tile (for example, a watershed model, a
terrain profile in an application, or heights along a road) must log in with an OTP, download complete tiles, and
merge and clip them. This process cannot be automated, because the OTP must be entered by a person.

| Task | Bhoonidhi | This service |
|---|---|---|
| Elevation for an 11 km area | OTP login → search → cart → download a 44.5 MB archive → extract 52 MB → clip manually | **One request**, approximately 330 KB |
| Machine-to-machine access | Not possible | Any HTTP client |
| Standard | STAC search only | OGC API - Coverages |

---

## 2. System Architecture

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
        │  3. ensure the 1° tiles beneath it are cached ──────▶ Bhoonidhi
        │       (catalogue search, download, conversion)        (new tiles only)
        │  4. read the window from the mosaic
        ▼  5. return GeoTIFF or CoverageJSON
 data/: tile cache (Cloud-Optimized GeoTIFFs) + VRT mosaic, shared by both replicas
```

### 2.1 Request Processing

1. pygeoapi validates the OGC parameters (`bbox`, `subset`, `properties`, `f`).
2. `CartoDEMProvider` converts the area into a **pixel window** that includes every pixel touching it.
3. Requests exceeding the size limits receive **413** (see [Section 2.4](#24-request-limits)).
4. Every 1° tile beneath the window is made available, and is retrieved from Bhoonidhi if necessary
   ([Section 2.2](#22-on-demand-tile-retrieval)).
5. The window is read from the VRT mosaic; only the required 512×512 blocks of each tile are read from disk.
6. The pixels are returned as GeoTIFF (DEFLATE compressed) or CoverageJSON (nodata as `null`).

The returned pixels are **bit-identical** to the source data; the compliance tests compare them with a GDAL clip.

### 2.2 On-Demand Tile Retrieval

The service does not maintain a complete copy of CartoDEM. It **retrieves tiles the first time an area is requested**
and retains them, so that Bhoonidhi remains the authoritative source and disk space is used only for areas in demand.

```
area ─▶ which 1° tiles? ─▶ all cached (or known to be sea)? ── yes ─▶ serve
                                     │ no
                                     ▼
                       more than 4 missing? ── yes ─▶ 413
                                     │ no
                                     ▼
          lock the missing tiles ─▶ search Bhoonidhi's catalogue ─▶ download ─▶ convert ─▶ add to mosaic ─▶ serve
                                     │
                                     ├─ not in the catalogue (open sea) ─▶ recorded, served as nodata
                                     └─ not online at Bhoonidhi          ─▶ 503 (must be ordered in the portal)
```

| Situation | Measured result |
|---|---|
| First request for a new area | 200 in approximately **10–13 s** (tile retrieved once) |
| Subsequent request for the same area | 200 in approximately **15 ms** |
| 6 concurrent users requesting the same new tile | **One** download; all 6 receive identical data |
| An area with no tile (Bay of Bengal) | 200, entirely nodata; repeated requests complete in 7 ms with no Bhoonidhi call |

**Concurrency.** Each tile has a file lock shared by all threads, processes and replicas, so that each tile is
downloaded only once. Locks are acquired in sorted order to prevent deadlocks, and before the catalogue search, so that
waiting requests do not repeat the search. The mosaic is rebuilt before the locks are released, so that a waiting
request never reads an incomplete mosaic. Files are written under temporary names and then renamed into place, so that
no partially written file is ever read.

### 2.3 Bhoonidhi Integration

| Purpose | Route | Authentication |
|---|---|---|
| Catalogue search | Documented API `POST /data/search` | Password login; tokens are **cached and refreshed** (access token 20 min, refresh token 5 h), so that a search does not consume one of the 20 permitted logins per hour |
| Tile download | Portal `GET /bhoonidhi/data/CARTODEM/P5/PAN/30m/<id>.zip?token=…&product_id=<id>` | A single portal login with a **6-digit OTP emailed to the account holder**; the session is valid for 6 hours and bound to the server IP address, so the service **renews it every hour** without an OTP |

- **Compliance with Bhoonidhi limits:** searches are spaced at least 0.5 s apart across all processes (Bhoonidhi
  permits 3 per second), and a single request may retrieve at most 4 new tiles (Bhoonidhi permits 5 concurrent
  downloads).
- **Session renewal:** each gunicorn worker runs a background thread that checks every 5 minutes and renews the portal
  session once it is one hour old (`VALIDATE_SESSION`). A shared lock and timestamp ensure that only one process
  performs the renewal. A session that the portal declines to renew is marked as expired, and requests for new tiles
  return 503 until the next login.
- **Retries:** an empty response (observed under load) is retried once and then reported as 503.
- **Credentials:** credentials are read from `.env`; tokens and the session are stored in `data/` with owner-only
  permissions. Neither is ever committed to version control.

### 2.4 Request Limits

| Limit | Value | Rationale |
|---|---|---|
| New tiles retrieved per request | **4** | Sufficient for any city or district; remains within the Bhoonidhi limit of 5 concurrent downloads and the daily quota |
| Largest GeoTIFF | **4 square degrees** (approximately 4 tiles) | Limits memory per request (approximately 200 MB at most) |
| Largest CoverageJSON | **0.1 square degrees** | CoverageJSON is a text format; 0.04 deg² already amounts to approximately 18 MB |

A request without an area refers to the entire coverage (960 square degrees) and receives a 413 response that
explains the limits.

---

## 3. Dataset

| Property | Value (from the Bhoonidhi item metadata and the files) |
|---|---|
| Bhoonidhi collection | `CartoSat-1_PAN_CartoDEM_30m` |
| Source | Cartosat-1 (`P5`) panchromatic stereo imagery, produced by NRSC, Version 4.0 |
| Resolution | 1 arc-second (approximately 30 m) |
| Accuracy | 8 m vertical (LE90), 15 m horizontal (CE90) |
| Coordinates | WGS84 longitude/latitude (EPSG:4326) |
| Tiles | 1°×1°, 3600 × 3600 pixels, with identifiers such as `P5_PAN_CD_N17_000_E078_000_30m` |
| Values | Float32 metres, nodata −32768 |
| Served extent | 68–98°E, 6–38°N (India and surrounding regions) |

- **Heights are referenced to the WGS84 ellipsoid, not to mean sea level.** Values are 50–90 m lower than commonly
  cited heights (Delhi approximately 163 m, Bengaluru approximately 821 m, Hyderabad approximately 460 m), which is
  consistent with the geoid–ellipsoid separation over India.
- **The dataset is a surface model** derived from optical imagery; heights therefore include buildings and vegetation.

**Processing.** Each downloaded tile (a 44.5 MB archive containing a 52 MB GeoTIFF) is losslessly converted into a
**Cloud-Optimized GeoTIFF** with 512×512 internal tiles, DEFLATE compression and overviews, of approximately 40 MB.
The raw download is then deleted. A GDAL **VRT mosaic** (a few kilobytes of XML) references all cached tiles on a
single fixed grid covering the entire extent, so that every request maps to the same pixel grid regardless of whether
its tiles are already cached.

---

## 4. Service Usage

### 4.1 Web Interface

The home page is an **area selector**: users can search for a place (for example, "Kurnool") or select **Draw area**
and click two corners. The page displays the size of the box, checks it against the limits, and offers **Download
GeoTIFF**, **View on map** (terrain colours) and the exact request URL. It only constructs the standard request
described below and has no API of its own. Place search uses the OpenStreetMap Nominatim service.

The remaining pages (collections, collection details, schema, conformance and the Swagger API documentation) allow the
service to be browsed. Every endpoint also returns HTML when requested with `?f=html`.

### 4.2 Endpoints

| Endpoint | Response |
|---|---|
| `/` | Landing page, with links to all endpoints below |
| `/conformance` | The implemented conformance classes |
| `/openapi` | OpenAPI 3.0 description (`?f=json`); Swagger UI in a browser |
| `/collections` | The available collections (one: `cartodem`) |
| `/collections/cartodem` | Collection metadata and extent |
| `/collections/cartodem/schema` | The field `elevation`, in metres |
| `/collections/cartodem/coverage` | **The coverage**, with the parameters below |

### 4.3 Coverage Parameters

| Parameter | Example | Description |
|---|---|---|
| `bbox` | `bbox=78.40,17.35,78.50,17.45` | Area as `minLon,minLat,maxLon,maxLat` in degrees |
| `subset` | `subset=Lat(17.35:17.45),Long(78.40:78.50)` | Area as a range on one or both axes; cannot be combined with `bbox` |
| `bbox-crs` | `bbox-crs=http://www.opengis.net/def/crs/EPSG/0/32644` | CRS of `bbox`, if it is not WGS84 longitude/latitude |
| `properties` | `properties=elevation` | Field to return (the only field is `elevation`) |
| `f` | `f=GTiff`, `f=json`, `f=html` | GeoTIFF, CoverageJSON (default) or a map view |
| `Accept` header | `Accept: image/tiff` | Equivalent to `f=GTiff` |

### 4.4 Examples

```bash
B=https://cybergis.indiasouthcentral.cloudapp.azure.com/collections/cartodem/coverage

curl -o hyd.tif "$B?bbox=78.40,17.35,78.50,17.45&f=GTiff"                       # GeoTIFF by bbox
curl -o hyd.tif "$B?subset=Lat(17.35:17.45),Long(78.40:78.50)&f=GTiff"          # same area by subset
curl "$B?bbox=78.40,17.40,78.41,17.41&properties=elevation&f=json"              # CoverageJSON values
curl -H "Accept: image/tiff" -o hyd.tif "$B?bbox=78.40,17.35,78.50,17.45"       # GeoTIFF by Accept header
```

In Python, the response must be downloaded first: responses are generated per request, so GDAL cannot open the URL
with range requests.

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

### 4.5 Response Codes

| Status | Condition |
|---|---|
| `200` | The requested area, as `image/tiff` or `application/prs.coverage+json`. Areas without a tile (open sea) are nodata |
| `400` | Malformed `bbox` or `subset`, unknown axis or property, area outside the extent, or `bbox` combined with `subset` |
| `404` | Unknown collection |
| `413` | Request too large: the area limit is exceeded, or more than 4 new tiles are required |
| `429` | Too many requests from one IP address (more than 20 per second) |
| `503` | A new tile cannot currently be retrieved (Bhoonidhi is unavailable, or the operator session has expired) |

Errors are returned as OGC exception documents with a `description` of the problem.

---

## 5. OGC Compliance

Implemented conformance classes (OGC API - Coverages Part 1):
`core` · `oas30` · `html` · `geodata-coverage` · `coverage-subset` · `coverage-rangesubset` · `coverage-bbox` · `coverage-datetime`

`tests/compliance_test.py` tests every class against a running server with real requests and verifies the returned
GeoTIFFs with GDAL. **Result: all 39 checks pass**, on both the live server and a local installation.

| Class | Checks | Verified behaviour |
|---|---|---|
| core | 16 | Landing page and its links, `/conformance`, `/collections`, collection extent, coverage returned as a CoverageJSON grid, `elevation` field in metres, schema |
| geodata-coverage | 2 | Collections link to their coverage |
| coverage-subset | 7 | Bounds match the request; **pixels identical to a GDAL clip of the source**; single-axis subset; invalid axis, invalid syntax and out-of-extent requests return 4xx |
| coverage-bbox | 4 | `bbox` produces the same result as the equivalent `subset`; invalid combinations return 4xx |
| coverage-rangesubset | 2 | `properties=elevation` is supported; an unknown field returns 400 |
| coverage-datetime | 1 | `datetime` is accepted (CartoDEM has no time axis) |
| oas30 | 2 | Valid OpenAPI 3.x document describing the coverage endpoint |
| html | 3 | Landing page, collections and collection available as HTML |
| general | 2 | An unknown collection returns 404; `Accept: image/tiff` returns GeoTIFF |

**Corrections to pygeoapi.** The standard pygeoapi raster provider failed 6 of these checks. The custom provider and a
small middleware component correct them:

| Problem | Correction |
|---|---|
| Clipping with a polygon mask set the outer row and column to nodata | A pixel window is read instead; pixels now match GDAL exactly |
| A subset on a single axis, an area outside the extent, or `bbox` combined with `subset` returned 500 | Handled correctly, returning results or 400 |
| A malformed `bbox` returned 500 | The middleware validates it and responds with 400 |
| `Accept: image/tiff` was ignored | The middleware maps it to `f=GTiff` |
| The band was unnamed (`null`) and had no units | Named `elevation`, in metres |
| A request without parameters returned the XML of the mosaic | A GeoTIFF is returned, or 413 if the request is too large |

The OpenAPI document generated by pygeoapi also described the coverage endpoint as a Features endpoint (GeoJSON,
"selects features", no GeoTIFF). `scripts/patch_openapi.py` corrects this description; the result validates as
OpenAPI 3.0.

---

## 6. Performance Evaluation

The service was load-tested with [k6](https://k6.io) at **100 concurrent users** (targets: p95 below 500 ms and an
error rate below 1%), using random areas within cached tiles so that the test measures the service rather than
Bhoonidhi. The service and k6 ran on the same 16-core machine.

| Scenario | Flask development server (1 process) | gunicorn (8 processes × 4 threads) |
|---|---|---|
| Mixed: 70% GeoTIFF, 15% CoverageJSON, 15% pages | 107 req/s, p95 1.05 s ✗ | **500 req/s, p95 289 ms** ✓ |
| GeoTIFF, 5–15 km areas | 168 req/s, p95 631 ms ✗ | **478 req/s, p95 319 ms** ✓ |
| CoverageJSON, 2–5 km areas | 55 req/s, p95 2.07 s ✗ | **394 req/s, p95 369 ms** ✓ |

A total of 383,066 requests were served without errors. Summaries are available in `loadtest/results/`.

- The single development server process was the bottleneck; under load, even static pages waited approximately 1 s.
- **Retrieval of new tiles does not delay other requests.** gunicorn uses threaded workers, so a request waiting on a
  Bhoonidhi download occupies only one thread. During a 19.8 s download in the middle of a 100-user test, the p95
  latency of other requests changed from 340 ms to 349 ms (`loadtest/results/cold-fetch-under-load.md`).

```bash
k6 run -e SCENARIO=mixed loadtest/coverage.js      # scenarios: mixed, geotiff, covjson, hot
k6 run -e BASE=https://<host> -e SCENARIO=mixed loadtest/coverage.js
```

---

## 7. Deployment

The live service runs on an Azure virtual machine (Ubuntu 24.04, 2 CPUs, India South Central) using Docker Compose:

```
Internet ──HTTPS──▶ nginx ──┬──▶ app1 (gunicorn) ──┐
                            └──▶ app2 (gunicorn) ──┴──▶ data/ (tiles, mosaic, Bhoonidhi session)
```

| Feature | Configuration |
|---|---|
| Replicas | Two identical service containers, health-checked every 15 s and restarted if they exit |
| Failover | nginx bypasses a failing replica; requests to a crashed replica are retried on the other |
| Cache | Coverage responses are cached for 24 h (`X-Cache-Status: HIT`); pages and metadata are always current |
| Rate limit | 20 requests/s per IP address (bursts of 40), after which 429 is returned |
| HTTPS | Let's Encrypt certificate, HTTP redirected to HTTPS, HSTS, automatic renewal |
| Timeouts | 120 s, to accommodate first requests that retrieve a tile |

A virtual machine is used instead of a managed platform because the Bhoonidhi session requires a **fixed outbound IP
address** and the tile cache requires **persistent storage**. The India South Central region is located close to
Bhoonidhi.

**Deployment on a server** (Ubuntu with Docker and a fixed public IP address):

```bash
# Copy the project (excluding .venv, data and .env) to the server, then:
cd CYBERGIS
cp .env.example .env && nano .env                 # Bhoonidhi user ID and password
echo "PYGEOAPI_SERVER_URL=http://<address>" >> .env
docker compose up -d --build

# Bhoonidhi login, performed once on the server: an OTP is emailed and requested at the prompt.
# The running service renews the session thereafter.
docker compose run --rm app1 python scripts/download_cartodem.py login
```

**Enabling HTTPS** requires a domain name. On Azure, a free DNS name label on the public IP address provides
`<label>.<region>.cloudapp.azure.com`:

```bash
docker compose -f docker-compose.yml -f docker-compose.https.yml run --rm --entrypoint certbot certbot \
  certonly --webroot -w /var/www/certbot --cert-name coverage -d <domain> \
  --agree-tos --register-unsafely-without-email
sed -i 's#^PYGEOAPI_SERVER_URL=.*#PYGEOAPI_SERVER_URL=https://<domain>#' .env
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
```

The certbot container renews the certificate when it is within 30 days of expiry, and nginx reloads every 6 hours to
apply it.

---

## 8. Local Installation

Requirements: Linux or macOS, Python 3.10 or later, GDAL command-line tools 3.1 or later (with the COG driver), and a
[Bhoonidhi account](https://bhoonidhi.nrsc.gov.in/bhoonidhi/login.html) for retrieving new tiles.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                                          # Bhoonidhi user ID and password

.venv/bin/python scripts/download_cartodem.py login           # once: an OTP is emailed and requested
.venv/bin/python scripts/download_cartodem.py fetch           # optional: pre-fetch Hyderabad

scripts/serve.sh                                              # http://localhost:5000 (DEV=1 for the Flask development server)
.venv/bin/python tests/compliance_test.py                     # 39 compliance checks
```

Tiles downloaded manually from the portal can be extracted into `data/raw/` and prepared with
`scripts/prepare_tiles.py`. The Docker stack can also be run locally:
`HTTP_PORT=8080 PYGEOAPI_SERVER_URL=http://localhost:8080 docker compose up -d`.

---

## 9. Project Structure

```
cartodem_service/
  app.py              WSGI application: pygeoapi + middleware (bbox validation, Accept: image/tiff)
  provider.py         CartoDEMProvider: pixel windows, size limits, CoverageJSON, tile retrieval
  tiles.py            Tile cache: tile selection, locks, conversion, mosaic
  bhoonidhi.py        Bhoonidhi client: catalogue search, OTP login, session renewal, tile download
scripts/
  download_cartodem.py  OTP login, session renewal and pre-fetching
  prepare_tiles.py      Preparation of manually added tiles
  patch_openapi.py      Correction of the OpenAPI description of the coverage endpoint
  serve.sh              Local service start-up
tests/compliance_test.py  The 39 compliance checks
loadtest/               k6 load test and results
theme/templates/        Web pages: base layout, area selector, collections, conformance, Swagger
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

## 10. Design Decisions

| Decision | Rationale |
|---|---|
| **OGC API - Coverages** rather than WCS 2.0 | The current OGC successor to WCS: the same purpose, with REST/JSON, an OpenAPI description and HTML pages |
| **pygeoapi** with a **custom provider** | pygeoapi implements the standard; the custom provider corrects its raster clipping and integrates tile retrieval |
| **On-demand tile retrieval with caching** | Keeps Bhoonidhi as the source without depending on it for every request; forwarding every request would immediately exceed the Bhoonidhi login and download limits |
| **Synchronous retrieval** | Standard clients expect data in the response; the cost is a slower first request for each new tile |
| **Portal download with a single OTP login and session renewal** | The documented `/download` endpoint rejects every token; renewing the portal session keeps the single OTP login valid |
| **Cloud-Optimized GeoTIFFs and a VRT mosaic** | A small request reads only a few blocks; adding a tile rewrites a few kilobytes rather than an entire mosaic |
| **gunicorn with threads** | Multiple processes provide throughput; threads ensure that a request waiting on Bhoonidhi occupies one thread rather than an entire process |
| **nginx and two replicas on one virtual machine** | Caching, rate limiting, HTTPS and failover without changes to the service; a virtual machine provides a fixed IP address and persistent storage |

---

## 11. Known Limitations

- **One OTP login is required after each prolonged outage.** The service renews the session while it is running; if it
  is stopped for more than 6 hours, the session expires. Cached areas remain available, but requests for new areas
  return 503 until the operator logs in again.
- **The session is bound to the server IP address.** It must be created on the machine that runs the service.
- **The download route is not part of the Bhoonidhi API specification.** It is the internal route of the portal and
  may change without notice.
- **First requests for new tiles are slow** (approximately 10–13 s, and longer when Bhoonidhi is under heavy load).
- **Size limits:** at most 4 new tiles, 4 deg² of GeoTIFF or 0.1 deg² of CoverageJSON per request.
- **Heights are ellipsoidal (WGS84),** not referenced to mean sea level.
- **No time axis:** `datetime` is accepted and ignored. **No output scaling or reprojection:** results are returned on
  the native 1 arc-second grid in EPSG:4326 (`bbox-crs` only affects how the request box is specified).
- **Tiles not online at Bhoonidhi** must first be ordered through the portal.
- **Single server:** the replicas share one disk; additional servers would require shared storage.
- **Caches are not cleared automatically:** coverage responses remain in the nginx cache for 24 h, tiles are never
  evicted (approximately 40 MB each), and tiles identified as sea are recorded in `data/cache/absent.json`.
- **The web pages use third-party assets** (Bootstrap, Google Fonts, map libraries, Esri basemap, Nominatim). The API
  does not depend on them.

---

## 12. Bhoonidhi API Findings

| Finding | Detail |
|---|---|
| `/download` rejects every token | Always returns `401 "Invalid session detected! Please login."` (the specification states "…Please login **again**", which suggests that an undocumented check fails). Variants tested: new and refreshed tokens, without `Bearer`, as a query parameter, POST instead of GET, with the portal logged out, and with an OTP-verified portal token |
| Portal downloads require an OTP | `LoginServlet`: password → OTP emailed → session token valid for 6 hours and bound to the client IP address |
| Portal sessions can be renewed | `LoginServlet` with `{"action": "VALIDATE_SESSION"}` and the token in a `token` header returns a new session token without an OTP. An expired token receives HTTP 200 with an empty `JWT` |
| An empty search returns 404 | `404 "NO RESULTS FOUND"` instead of the STAC-conformant `200` with no features |
| Search may return an empty response under load | HTTP 200 with an empty body when several searches arrive simultaneously |
| The catalogue requires authentication | Even `GET /data/collections/{id}` returns 401 |
| STAC items contain no data asset | CartoDEM items contain only `metadata` and `thumbnail` |
| Inconsistencies in the specification | The logout link points to `/auth/token`; inputs are described as a POST body, whereas download uses GET; error keys appear as both `errorCode` and `ErrorCode` |

---

## 13. Future Work

- **Background retrieval:** respond to a request for a new area with a "retry shortly" response and download the tile
  in the background, so that no request waits on Bhoonidhi.
- **Multiple servers:** store tiles in object storage (S3, R2 or GCS) shared by several servers.
- **Further conformance classes:** `scaling` and `crs`.
- **Additional data:** CartoDEM at 10 m and 2.5 m (referenced in the portal source code) and other Bhoonidhi raster
  collections.

---

## 14. Data Attribution

CartoDEM is produced by the **National Remote Sensing Centre (NRSC), ISRO** and distributed through
[Bhoonidhi](https://bhoonidhi.nrsc.gov.in). This service provides subsets of the data without modification, apart from
lossless recompression. Users should comply with the Bhoonidhi terms of use for the data.
