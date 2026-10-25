"""Live updates: one thread LISTENs on the `new_post` channel and fans posts out to SSE clients."""
import json
import logging
import queue
import threading
import time

import psycopg

log = logging.getLogger(__name__)

CHANNEL = "new_post"
CLIENT_QUEUE_SIZE = 100


class Broadcaster:
    def __init__(self, database_url: str, load_post):
        """`load_post(post_id) -> dict | None` returns a visible, serialized post (or None to skip)."""
        self.database_url = database_url
        self.load_post = load_post
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=CLIENT_QUEUE_SIZE)
        with self._lock:
            self._clients.add(q)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="new-post-listener", daemon=True)
                self._thread.start()
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def stop(self) -> None:
        self._stop.set()

    def publish(self, payload: str) -> None:
        """Handle one NOTIFY payload (a post id). Loads the post once, then fans out."""
        try:
            post_id = int(payload)
        except (TypeError, ValueError):
            log.warning("Ignoring malformed %s payload: %r", CHANNEL, payload)
            return
        post = self.load_post(post_id)
        if post is None:
            return
        message = json.dumps(post)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(message)
            except queue.Full:
                pass  # slow client; it misses this one rather than blocking everyone

    def _run(self) -> None:
        delay = 1
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.database_url, autocommit=True) as conn:
                    conn.execute(f"LISTEN {CHANNEL}")
                    log.info("Listening for %s notifications", CHANNEL)
                    delay = 1
                    while not self._stop.is_set():
                        for notify in conn.notifies(timeout=5.0):
                            try:
                                self.publish(notify.payload)
                            except Exception:
                                log.exception("Failed to publish post %s", notify.payload)
            except Exception as e:
                log.warning("LISTEN connection lost (%s); retrying in %ss", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
