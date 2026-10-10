"""OGC API - Coverages compliance checks against a running server.

Usage:
    python3 tests/compliance_test.py [base_url]     (default http://localhost:5000)

Each check prints PASS/FAIL with the evidence. Exit code is the number of failures.
GDAL command-line tools are used to verify returned GeoTIFFs against the source data.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000").rstrip("/")
COLL = "cartodem"
COV = f"{BASE}/collections/{COLL}/coverage"
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "cartodem.vrt"

REL_COVERAGE = "http://www.opengis.net/def/rel/ogc/1.0/coverage"
REL_SCHEMA = "http://www.opengis.net/def/rel/ogc/1.0/schema"
CONF = "http://www.opengis.net/spec/ogcapi-coverages-1/1.0/conf/"

# A small window over Hyderabad, inside the served extent
W, S, E, N = 78.40, 17.35, 78.50, 17.45
TMP = Path(tempfile.mkdtemp(prefix="covtest_"))

results = []


def check(cls, name, ok, evidence=""):
    results.append((cls, name, bool(ok), evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {cls:22s} {name}" + (f"  ({evidence})" if evidence else ""))


def get(url, **kw):
    kw.setdefault("timeout", 120)
    return requests.get(url, **kw)


def links(doc, rel):
    return [link for link in doc.get("links", []) if link.get("rel") == rel]


def gdal_json(path):
    return json.loads(subprocess.run(["gdalinfo", "-json", "-checksum", str(path)],
                                     capture_output=True, text=True, check=True).stdout)


def save(resp, name):
    path = TMP / name
    path.write_bytes(resp.content)
    return path


def bounds(info):
    c = info["cornerCoordinates"]
    return c["upperLeft"][0], c["lowerRight"][1], c["lowerRight"][0], c["upperLeft"][1]


def close(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


PIXEL = 1 / 3600  # 1 arc-second


def run():
    # ---------------------------------------------------------------- core
    r = get(f"{BASE}/?f=json")
    landing = r.json() if r.ok else {}
    check("core", "landing page returns 200 JSON", r.status_code == 200, r.status_code)
    for rel in ("service-desc", "service-doc", "conformance", "data"):
        check("core", f"landing page has rel={rel} link", links(landing, rel))

    r = get(f"{BASE}/conformance?f=json")
    conforms = r.json().get("conformsTo", []) if r.ok else []
    check("core", "/conformance returns 200", r.status_code == 200, r.status_code)
    advertised = sorted(c[len(CONF):] for c in conforms if c.startswith(CONF))
    check("core", "conformance declares coverages core", f"{CONF}core" in conforms,
          ", ".join(advertised))

    r = get(f"{BASE}/collections?f=json")
    ids = [c["id"] for c in r.json().get("collections", [])] if r.ok else []
    check("core", "/collections lists cartodem", COLL in ids, ids)

    r = get(f"{BASE}/collections/{COLL}?f=json")
    coll = r.json() if r.ok else {}
    check("core", "/collections/cartodem returns 200", r.status_code == 200, r.status_code)
    bbox = coll.get("extent", {}).get("spatial", {}).get("bbox", [[]])[0]
    check("core", "collection has spatial extent", len(bbox) == 4, bbox)

    r = get(COV, params={"subset": f"Lat({S}:{N}),Long({W}:{E})", "f": "json"})
    check("core", "/coverage returns 200", r.status_code == 200, r.status_code)
    check("core", "/coverage default JSON is CoverageJSON",
          "coverage+json" in r.headers.get("Content-Type", ""), r.headers.get("Content-Type"))
    cj = r.json() if r.ok else {}
    check("core", "CoverageJSON is a Grid coverage",
          cj.get("type") == "Coverage" and cj.get("domain", {}).get("domainType") == "Grid")
    check("core", "CoverageJSON parameter is elevation in metres",
          cj.get("parameters", {}).get("elevation", {}).get("unit", {})
            .get("symbol", {}).get("value") == "m", list(cj.get("parameters", {})))

    schema_links = links(coll, REL_SCHEMA)
    check("core", "collection links to schema (range type)", schema_links)
    if schema_links:
        href = next((link["href"] for link in schema_links
                     if "json" in link.get("type", "")), schema_links[0]["href"])
        r = get(href)
        props = r.json().get("properties", {}) if r.ok else {}
        check("core", "schema describes the elevation field", "elevation" in props,
              list(props))

    # ------------------------------------------------------ geodata-coverage
    check("geodata-coverage", "collection links to its coverage",
          links(coll, REL_COVERAGE), [link.get("type") for link in links(coll, REL_COVERAGE)])
    r = get(f"{BASE}/collections?f=json")
    entry = next((c for c in r.json().get("collections", []) if c["id"] == COLL), {})
    check("geodata-coverage", "/collections entry links to the coverage",
          links(entry, REL_COVERAGE))

    # ------------------------------------------------------- coverage-subset
    r = get(COV, params={"subset": f"Lat({S}:{N}),Long({W}:{E})", "f": "GTiff"})
    check("coverage-subset", "Lat+Long subset returns GeoTIFF",
          r.status_code == 200 and r.headers.get("Content-Type") == "image/tiff",
          f"{r.status_code} {r.headers.get('Content-Type')} {len(r.content)}B")
    if r.status_code == 200:
        sub = gdal_json(save(r, "subset.tif"))
        got = bounds(sub)
        check("coverage-subset", "returned bounds match the request (within 1 pixel)",
              close(got, (W, S, E, N), PIXEL), [round(v, 5) for v in got])

        ref = TMP / "reference.tif"
        subprocess.run(["gdal_translate", "-q", "-projwin", str(got[0]), str(got[3]),
                        str(got[2]), str(got[1]), str(SOURCE), str(ref)], check=True)
        ref_info = gdal_json(ref)
        same_size = sub["size"] == ref_info["size"]
        same_sum = (sub["bands"][0].get("checksum") == ref_info["bands"][0].get("checksum"))
        check("coverage-subset", "pixels identical to a GDAL clip of the source",
              same_size and same_sum,
              f"size {sub['size']} vs {ref_info['size']}, checksum "
              f"{sub['bands'][0].get('checksum')} vs {ref_info['bands'][0].get('checksum')}")

    # A single-axis subset spans the whole other axis, so it may exceed the
    # per-request limits; 413 with an explanation is the documented answer then
    r = get(COV, params={"subset": f"Lat({S}:{N})", "f": "GTiff"})
    if r.status_code == 200:
        got = bounds(gdal_json(save(r, "lat_only.tif")))
        ok = got[1] >= S - PIXEL and got[3] <= N + PIXEL
        check("coverage-subset", "single-axis subset (Lat only) is honoured", ok,
              f"returned lat {got[1]:.4f}..{got[3]:.4f}, asked {S}..{N}")
    else:
        check("coverage-subset", "single-axis subset (Lat only) is honoured or 413",
              r.status_code == 413, f"HTTP {r.status_code}: {r.text[:90]!r}")

    for label, subset in (("unknown axis", f"Height({S}:{N})"),
                          ("malformed subset", "Lat(17.3"),
                          ("subset outside the extent", "Lat(10:11),Long(70:71)")):
        r = get(COV, params={"subset": subset, "f": "json"})
        check("coverage-subset", f"{label} -> 4xx", 400 <= r.status_code < 500,
              r.status_code)

    # --------------------------------------------------------- coverage-bbox
    r = get(COV, params={"bbox": f"{W},{S},{E},{N}", "f": "GTiff"})
    check("coverage-bbox", "bbox returns 200 GeoTIFF", r.status_code == 200, r.status_code)
    if r.status_code == 200:
        b = gdal_json(save(r, "bbox.tif"))
        check("coverage-bbox", "bbox result equals the equivalent subset",
              b["bands"][0].get("checksum") == sub["bands"][0].get("checksum")
              and b["size"] == sub["size"], f"size {b['size']}")
    r = get(COV, params={"bbox": f"{W},{S},{E},{N}",
                         "subset": f"Lat({S}:{N}),Long({W}:{E})", "f": "json"})
    check("coverage-bbox", "bbox together with Lat/Long subset -> 4xx",
          400 <= r.status_code < 500, r.status_code)
    r = get(COV, params={"bbox": "78.4,17.35,78.5", "f": "json"})
    check("coverage-bbox", "bbox with 3 numbers -> 4xx", 400 <= r.status_code < 500,
          r.status_code)

    # -------------------------------------------------- coverage-rangesubset
    small = {"subset": "Lat(17.40:17.41),Long(78.40:78.41)", "f": "json"}
    r = get(COV, params={**small, "properties": "elevation"})
    check("coverage-rangesubset", "properties=elevation -> 200",
          r.status_code == 200 and "elevation" in r.json().get("ranges", {}), r.status_code)
    r = get(COV, params={**small, "properties": "temperature"})
    check("coverage-rangesubset", "unknown property -> 400", r.status_code == 400,
          r.status_code)

    # ----------------------------------------------------- coverage-datetime
    r = get(COV, params={**small, "datetime": "2024-01-01T00:00:00Z"})
    check("coverage-datetime", "datetime on a coverage with no time axis is handled",
          r.status_code in (200, 400), f"{r.status_code} {r.text[:80]!r}")

    # ---------------------------------------------------------------- oas30
    api_links = links(landing, "service-desc")
    r = get(api_links[0]["href"] if api_links else f"{BASE}/openapi?f=json",
            headers={"Accept": "application/vnd.oai.openapi+json;version=3.0"})
    try:
        oas = r.json()
    except ValueError:
        oas = {}
    check("oas30", "OpenAPI document is version 3.x", str(oas.get("openapi", "")).startswith("3"),
          oas.get("openapi"))
    check("oas30", "OpenAPI documents the coverage path",
          f"/collections/{COLL}/coverage" in oas.get("paths", {}))

    # ----------------------------------------------------------------- html
    for path in ("/", "/collections", f"/collections/{COLL}"):
        r = get(f"{BASE}{path}?f=html")
        check("html", f"{path} as HTML", r.status_code == 200
              and "text/html" in r.headers.get("Content-Type", ""), r.status_code)

    # -------------------------------------------------------------- general
    r = get(f"{BASE}/collections/does-not-exist/coverage?f=json")
    check("general", "unknown collection -> 404", r.status_code == 404, r.status_code)
    r = get(COV, params={"subset": "Lat(17.40:17.41),Long(78.40:78.41)"},
            headers={"Accept": "image/tiff"})
    check("general", "Accept: image/tiff negotiates GeoTIFF",
          r.headers.get("Content-Type") == "image/tiff",
          f"{r.status_code} {r.headers.get('Content-Type')}")


if __name__ == "__main__":
    run()
    failed = [x for x in results if not x[2]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for cls, name, _, evidence in failed:
        print(f"  FAIL {cls}: {name} ({evidence})")
    sys.exit(len(failed))
