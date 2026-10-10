# CYBERGIS

This repo has the implementation of **OGC API - Coverages** using [pygeoapi](https://pygeoapi.io/).

**Full documentation:** [OGC_API_COVERAGES.md](OGC_API_COVERAGES.md) covers the gap evidence, service design, implementation choices, API reference, compliance results, setup and known limitations.

---

## Identified Gap

Bhoonidhi doesn't provide a standard way to get only the part of a raster that a user needs. Users have to download the full product and clip it themselves.

## Proposed Fix

Implement **OGC API - Coverages** using pygeoapi, which does the same job as WCS. It returns only the area or bands the user requests. The first dataset will be **CartoDEM** (single-band), and after that it can be extended to multi-band rasters and time.
