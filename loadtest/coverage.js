// k6 load test for the coverage service.
//
//   k6 run -e SCENARIO=geotiff loadtest/coverage.js
//   k6 run -e SCENARIO=covjson -e BASE=http://localhost:5000 loadtest/coverage.js
//
// SCENARIO: geotiff (city-sized GeoTIFFs), covjson (small CoverageJSON) or
// mixed (70% GeoTIFF, 15% CoverageJSON, 15% HTML/JSON pages).
// Every request uses a random box inside a tile that is already cached, so the
// test measures this service, not Bhoonidhi.
import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://localhost:5000';
const SCENARIO = __ENV.SCENARIO || 'mixed';
const COVERAGE = `${BASE}/collections/cartodem/coverage`;

// [lat, lon] of the south-west corner of each cached 1-degree tile
const TILES = [
  [12, 77], [13, 80], [15, 77], [15, 78], [17, 78], [17, 79],
  [18, 73], [18, 79], [19, 72], [19, 80], [28, 77],
];

const geotiffTime = new Trend('geotiff_duration', true);
const covjsonTime = new Trend('covjson_duration', true);
const pageTime = new Trend('page_duration', true);

export const options = {
  stages: [
    { duration: '30s', target: 25 },
    { duration: '30s', target: 50 },
    { duration: '30s', target: 100 },
    { duration: '2m', target: 100 },
    { duration: '15s', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],
    http_req_failed: ['rate<0.01'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

// Random square box of the given size, entirely inside a random cached tile
function randomBox(size) {
  const [lat, lon] = TILES[Math.floor(Math.random() * TILES.length)];
  const w = lon + Math.random() * (1 - size);
  const s = lat + Math.random() * (1 - size);
  return [w, s, w + size, s + size].map((v) => v.toFixed(4)).join(',');
}

function geotiff() {
  const size = 0.05 + Math.random() * 0.1; // 5-15 km boxes
  const res = http.get(`${COVERAGE}?bbox=${randomBox(size)}&f=GTiff`, { tags: { kind: 'geotiff' } });
  check(res, {
    'geotiff 200': (r) => r.status === 200,
    'geotiff is tiff': (r) => (r.headers['Content-Type'] || '').startsWith('image/tiff'),
  });
  geotiffTime.add(res.timings.duration);
}

function covjson() {
  const size = 0.02 + Math.random() * 0.03; // 2-5 km boxes
  const res = http.get(`${COVERAGE}?bbox=${randomBox(size)}&f=json`, { tags: { kind: 'covjson' } });
  check(res, { 'covjson 200': (r) => r.status === 200 });
  covjsonTime.add(res.timings.duration);
}

const PAGES = ['/?f=html', '/collections?f=html', '/collections/cartodem?f=json', '/conformance?f=json'];

function page() {
  const res = http.get(BASE + PAGES[Math.floor(Math.random() * PAGES.length)], { tags: { kind: 'page' } });
  check(res, { 'page 200': (r) => r.status === 200 });
  pageTime.add(res.timings.duration);
}

export default function () {
  if (SCENARIO === 'geotiff') { geotiff(); return; }
  if (SCENARIO === 'covjson') { covjson(); return; }
  const r = Math.random();
  if (r < 0.70) { geotiff(); } else if (r < 0.85) { covjson(); } else { page(); }
}
