"""gunicorn settings for the coverage service (used by scripts/serve.sh and the Docker image).

Threaded workers: a request waiting on a Bhoonidhi tile download holds one
thread, not a whole process, so cached requests keep being served meanwhile.
Tile downloads and mosaic rebuilds are already safe across processes (file locks).
"""
import os

bind = os.getenv('BIND', '0.0.0.0:5000')
workers = int(os.getenv('WORKERS', '8'))
threads = int(os.getenv('THREADS', '4'))
worker_class = 'gthread'

# A first request for an uncached area waits for a Bhoonidhi download (10-30 s,
# longer when Bhoonidhi is slow); gunicorn's default 30 s timeout would kill it
timeout = 120
graceful_timeout = 30
keepalive = 5

accesslog = None
errorlog = '-'
loglevel = 'warning'
