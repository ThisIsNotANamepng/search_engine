"""Gunicorn settings for the web frontend (search pages, API, dashboard, node heartbeats).

Run from the repo root:

    gunicorn -c gunicorn.conf.py frontend.app:app

or from frontend/ (what deploy/db01/stultus-head.service does):

    python -m gunicorn -c ../gunicorn.conf.py app:app

Everything can be overridden with environment variables (see below); command-line flags win over
this file. See docs/docs/serving.md for the measurements behind the defaults.
"""
import os
import sys

# The app must not start its background threads (dashboard stats prewarm, search warm-up) while
# it is imported in the master process: with preload_app a thread started before the fork does not
# exist in the workers. post_worker_init() starts them inside each worker instead.
os.environ.setdefault("STULTUS_DEFER_BACKGROUND", "1")

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")

# gthread: a small number of processes, many threads each. Requests are mostly waiting on
# Postgres (which releases the GIL), so threads overlap that wait cheaply; extra *processes* add
# CPU parallelism for the Python-side ranking and page rendering, but every process carries its
# own search caches/connection pool and its own dashboard stats thread. Default 1 process keeps
# the dashboard's search-timing panel exact (the timings live in the worker's memory) and matches
# the old service unit; raise WEB_CONCURRENCY if search CPU is the bottleneck and RAM allows.
worker_class = "gthread"
workers = int(os.getenv("WEB_CONCURRENCY", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "16"))

# Load the app once in the master and fork (GUNICORN_PRELOAD=1) or load it inside each worker
# (default). Measured here, preloading saved nothing: Python touches refcounts everywhere, so the
# "shared" pages are copied almost at once and the master just keeps a second ~140 MB copy of the
# app resident. Only worth turning on for several workers, to fail a broken deploy at start.
preload_app = os.getenv("GUNICORN_PRELOAD", "0") == "1"

# Keep client connections open between requests (a page view is HTML + a few assets); with
# gthread an idle keep-alive connection costs no thread.
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "30"))

# Searches are capped by statement_timeout (5 s) and the dashboard polls are quick; 120 s is
# only the "worker is wedged" limit.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30

# Recycle a worker after a lot of requests (a guard against slow leaks); jitter keeps several
# workers from restarting together. Recycling drops the in-process search caches, so it is rare.
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "100000"))
max_requests_jitter = max_requests // 10

# Heartbeat file on tmpfs (the default is /tmp, which may be on the SD card).
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

accesslog = os.getenv("GUNICORN_ACCESSLOG") or None   # e.g. "-" for stdout; off by default
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")


def post_worker_init(worker):
    """Runs in each worker after the app is loaded: start the dashboard prewarm and warm the
    search module (DB pool, corpus statistics) so the first visitor doesn't pay for them."""
    module = sys.modules.get(getattr(worker.wsgi, "import_name", ""))
    start = getattr(module, "start_background_tasks", None)
    if start:
        start()
