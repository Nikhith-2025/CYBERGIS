# CYBERGIS

This repository contains an implementation of **OGC API - Coverages** using [pygeoapi](https://pygeoapi.io/), which
serves the **CartoDEM 30 m** elevation data of ISRO/NRSC from [Bhoonidhi](https://bhoonidhi.nrsc.gov.in).

#### Live website: **[cybergis.indiasouthcentral.cloudapp.azure.com](https://cybergis.indiasouthcentral.cloudapp.azure.com)**

[![Coverage Service landing page: search for a place or draw a box, then download its elevation as a GeoTIFF](docs/landing-page.png)](https://cybergis.indiasouthcentral.cloudapp.azure.com)

Users can search for a place in India, or draw a box on the map, and download its elevation as a GeoTIFF. The same
data can also be requested directly from the API:

```bash
curl -o hyderabad.tif \
  "https://cybergis.indiasouthcentral.cloudapp.azure.com/collections/cartodem/coverage?bbox=78.40,17.35,78.50,17.45&f=GTiff"
```

**Full documentation:** [OGC_API_COVERAGES.md](OGC_API_COVERAGES.md) presents the evidence for the gap, the service
design, the implementation choices, the API reference, the compliance results, the setup procedure and the known
limitations.

---

## Identified Gap

Bhoonidhi does not provide a standard method for retrieving only the portion of a raster that a user requires. Users
must download the complete product and clip it themselves.

## Proposed Solution

The solution implements **OGC API - Coverages** using pygeoapi, which serves the same purpose as WCS and returns only
the area or bands requested. The first dataset is **CartoDEM** (single band); the service can subsequently be
extended to multi-band rasters and temporal data.

## Bhoonidhi Login

New tiles are downloaded from Bhoonidhi, which requires a login verified by a one-time password (OTP) sent to the
email address of the account. The Bhoonidhi user ID and password must be entered in `.env`, after which a single login
is performed on the machine that runs the service, since the session is bound to its IP address:

```bash
docker compose run --rm app1 python scripts/download_cartodem.py login   # with Docker
.venv/bin/python scripts/download_cartodem.py login                      # without Docker
```

The OTP received by email is entered at the prompt. The service then renews the session automatically every hour,
so no further OTP is required while it remains running. If the service is stopped for more than 6 hours, the session
expires and one further login is required. The complete setup procedure is described in
[OGC_API_COVERAGES.md](OGC_API_COVERAGES.md).
