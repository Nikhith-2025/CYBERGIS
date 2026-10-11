# Tile Retrieval Under Load

**Setup.** gunicorn with 8 workers × 4 threads. k6 generated `mixed` cached traffic from 100 constant virtual users
for 90 s (`k6 run -e SCENARIO=mixed --vus 100 --duration 90s --no-thresholds --out csv=cold.csv loadtest/coverage.js`).
After 30 s, a single request was issued for an uncached area (Jaipur, tile N26_E075):

```
curl -o jaipur.tif "http://localhost:5000/collections/cartodem/coverage?bbox=75.75,26.85,75.85,26.95&f=GTiff"
```

**Result.** The Jaipur request returned HTTP 200 in 19.8 s, which included retrieving the tile from Bhoonidhi,
preparing it and adding it to the mosaic.

Latency of the cached requests, taken from the k6 CSV output:

| Window | Requests | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| Before (first 10 s excluded) | 11,530 | 173 ms | 340 ms | 405 ms | 577 ms |
| During the 19.8 s download | 10,669 | 177 ms | 349 ms | 433 ms | 695 ms |
| After | 20,707 | 181 ms | 360 ms | 430 ms | 599 ms |

Entire run: 48,161 requests, 534 requests/s, 0 errors.
