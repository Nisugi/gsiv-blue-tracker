"""Flask viewer for the archive.

Run: flask --app archiver.web.app run --port 8080  (Flask finds create_app)
"""
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from flask import Flask, Response, abort, g, jsonify, render_template, request, stream_with_context

from .live import Broadcaster
from .queries import MAX_PER_PAGE, PER_PAGE, Archive, SearchFilters
from .search import parse_search_query

log = logging.getLogger("archiver.web")

SLOW_REQUEST_SECONDS = 2.0
SEARCH_TTL = 60
LIST_TTL = 300
HEARTBEAT_SECONDS = 25


class TTLCache:
    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            self._data.pop(key, None)
            return None

    def set(self, key, value, ttl):
        with self._lock:
            if len(self._data) > 1000:
                now = time.monotonic()
                self._data = {k: v for k, v in self._data.items() if v[0] > now}
            self._data[key] = (time.monotonic() + ttl, value)

    def clear(self):
        with self._lock:
            self._data.clear()


def _id_list(value: str | None) -> list[int]:
    ids = []
    for part in (value or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _date(value: str | None, end: bool = False) -> datetime | None:
    """Parse YYYY-MM-DD or an ISO datetime as UTC. A bare date used as an end bound covers the whole day."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end and len(value) == 10:
        dt += timedelta(days=1)
    return dt


def _flag(value) -> bool:
    return str(value).lower() in ("1", "true", "yes", "on")


def filters_from_args(args) -> SearchFilters:
    authors = args.get("authors") or args.get("author") or ""
    return SearchFilters(
        query=parse_search_query(args.get("q", "").strip()),
        author_ids=_id_list(authors),
        channel_ids=_id_list(args.get("channels") or args.get("channel")),
        date_from=_date(args.get("date_from")),
        date_to=_date(args.get("date_to"), end=True),
        all_time=_flag(args.get("all_time", "0")),
        sort_asc=args.get("sort", "desc").lower() == "asc",
        page=_int(args.get("page"), 1, 1, 10_000),
        per_page=_int(args.get("per_page"), PER_PAGE, 1, MAX_PER_PAGE),
    )


def create_app(settings=None, archive=None, broadcaster=None) -> Flask:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if settings is None:
        from archiver import settings as settings_module
        settings = settings_module.load()

    app = Flask(__name__)
    app.json.sort_keys = False
    archive = archive or Archive(settings)
    broadcaster = broadcaster or Broadcaster(
        settings.database_url, lambda post_id: archive.get_post(post_id, visible_only=True))
    cache = TTLCache()
    app.extensions["archive"] = archive
    app.extensions["broadcaster"] = broadcaster
    app.extensions["cache"] = cache

    def cached(key, ttl, compute):
        value = cache.get(key)
        if value is None:
            value = compute()
            cache.set(key, value, ttl)
        return value

    def request_key():
        return (request.path, tuple(sorted(request.args.items(multi=True))))

    @app.context_processor
    def site_context():
        return {"server_name": settings.server_name or "Discord", "label": settings.label,
                "guild_id": str(settings.guild_id)}

    @app.before_request
    def start_timer():
        g.request_start = time.perf_counter()

    @app.after_request
    def log_slow(response):
        start = g.get("request_start")
        if start is not None and request.path != "/stream":
            elapsed = time.perf_counter() - start
            if elapsed > SLOW_REQUEST_SECONDS:
                query = request.query_string.decode("utf-8", "replace")[:200]
                log.warning("SLOW %s %s?%s %s %.2fs", request.method, request.path, query,
                            response.status_code, elapsed)
        return response

    @app.errorhandler(psycopg.errors.InvalidRegularExpression)
    def bad_regex(e):
        return jsonify(error="Invalid regular expression", detail=str(e).splitlines()[0]), 400

    @app.errorhandler(psycopg.errors.QueryCanceled)
    def query_timeout(e):
        return jsonify(error="The search took too long; try narrowing it"), 503

    @app.errorhandler(psycopg.OperationalError)
    def db_down(e):
        log.error("Database error on %s: %s", request.path, e)
        return jsonify(error="Database unavailable"), 503

    # -- pages ---------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html")

    # -- API -----------------------------------------------------------------

    @app.get("/api/search")
    def search():
        f = filters_from_args(request.args)
        return jsonify(cached(request_key(), SEARCH_TTL, lambda: archive.search(f)))

    @app.get("/api/authors")
    def authors():
        return jsonify(cached("authors", LIST_TTL, archive.tracked_authors))

    @app.get("/api/channels")
    def channels():
        return jsonify(cached("channels", LIST_TTL, archive.channels))

    @app.get("/api/channels/<int:chan_id>/threads")
    def threads(chan_id):
        return jsonify(cached(("threads", chan_id), LIST_TTL, lambda: archive.threads(chan_id)))

    @app.get("/api/stats")
    def stats():
        return jsonify(cached("stats", LIST_TTL, archive.stats))

    @app.get("/api/posts/<int:post_id>")
    def post(post_id):
        found = archive.get_post(post_id)
        if not found:
            abort(404)
        return jsonify(found)

    @app.get("/api/posts/<int:post_id>/revisions")
    def revisions(post_id):
        revs = archive.revisions(post_id)
        if revs is None:
            abort(404)
        return jsonify(revs)

    @app.get("/health")
    def health():
        try:
            ok = archive.ping()
        except Exception as e:
            log.error("Health check failed: %s", e)
            ok = False
        body = {"status": "ok" if ok else "degraded", "database": ok,
                "stream_clients": broadcaster.client_count}
        return jsonify(body), 200 if ok else 503

    @app.get("/stream")
    def stream():
        q = broadcaster.subscribe()

        def events():
            try:
                yield "retry: 5000\n\n"
                while True:
                    try:
                        message = q.get(timeout=HEARTBEAT_SECONDS)
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: post\ndata: {message}\n\n"
            finally:
                broadcaster.unsubscribe(q)

        return Response(stream_with_context(events()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return e

    return app

