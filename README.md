# CYBERGIS

This repository contains an implementation of **OGC API - Coverages** using [pygeoapi](https://pygeoapi.io/), which
serves the **CartoDEM 30 m** elevation data of ISRO/NRSC from [Bhoonidhi](https://bhoonidhi.nrsc.gov.in).

#### Live website: **[https://cybergis.indiasouthcentral.cloudapp.azure.com](https://cybergis.indiasouthcentral.cloudapp.azure.com)**

[![Coverage Service landing page: search for a place or draw a box, then download its elevation as a GeoTIFF](docs/landing-page.png)](https://cybergis.indiasouthcentral.cloudapp.azure.com)

Users can search for a place in India, or draw a box on the map, and download its elevation as a GeoTIFF. The same
data can also be requested directly from the API:

```bash
curl -o hyderabad.tif \
  "https://cybergis.indiasouthcentral.cloudapp.azure.com/collections/cartodem/coverage?bbox=78.40,17.35,78.50,17.45&f=GTiff"
```

**Full documentation:** [OGCAPI_COVERAGES.md](OGCAPI_COVERAGES.md) presents the evidence for the gap, the service
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
