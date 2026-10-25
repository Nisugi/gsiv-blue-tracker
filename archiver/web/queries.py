"""All SQL used by the viewer. Every query is parameterized; IDs leave this module as strings."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import re

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .search import ParsedQuery, build_text_filter

MAX_RESULTS = 10_000
PER_PAGE = 25
MAX_PER_PAGE = 100
BROAD_SEARCH_DAYS = 90
REGEX_TIMEOUT_MS = 3000   # regex searches scan every post; fail fast instead of holding a connection
THREAD_TYPES = ("public_thread", "private_thread", "news_thread")

AUTHOR_NAME = "COALESCE(m.tracked_name, m.display_name, m.username, 'Unknown')"

# Discord mention syntax in message content: <@123> or <@!123>, <#123>, <@&123>
MENTION_PATTERNS = {
    "users": re.compile(r"<@!?(\d+)>"),
    "channels": re.compile(r"<#(\d+)>"),
    "roles": re.compile(r"<@&(\d+)>"),
}

# A channel is hidden if it, its parent, or its grandparent (thread -> channel -> category)
# is in hidden_channels. Expects aliases c (channel), pc (parent channel).
HIDDEN_FILTER = """
    p.chan_id <> ALL(%(hidden)s::bigint[])
    AND COALESCE(c.parent_id, 0) <> ALL(%(hidden)s::bigint[])
    AND COALESCE(pc.parent_id, 0) <> ALL(%(hidden)s::bigint[])
"""

POST_COLUMNS = f"""
    p.post_id, p.chan_id, p.author_id, p.content, p.created_at, p.edited_at,
    p.reply_to_id, p.reason, p.pinned,
    c.name AS channel_name, c.parent_id AS channel_parent_id,
    {AUTHOR_NAME} AS author_name, m.avatar_url, COALESCE(m.is_tracked, FALSE) AS is_tracked,
    (SELECT count(*) FROM post_revisions r WHERE r.post_id = p.post_id) AS edit_count,
    (SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id', a.attach_id::text, 'filename', a.filename, 'url', a.url,
            'content_type', a.content_type, 'width', a.width, 'height', a.height)
            ORDER BY a.attach_id), '[]'::jsonb)
       FROM attachments a WHERE a.post_id = p.post_id) AS attachments
"""

POST_JOINS = """
    LEFT JOIN channels c ON c.chan_id = p.chan_id
    LEFT JOIN channels pc ON pc.chan_id = c.parent_id
    LEFT JOIN members m ON m.member_id = p.author_id
"""


@dataclass
class SearchFilters:
    query: ParsedQuery = field(default_factory=ParsedQuery)
    author_ids: list[int] = field(default_factory=list)
    channel_ids: list[int] = field(default_factory=list)   # threads of these are included
    date_from: datetime | None = None
    date_to: datetime | None = None
    all_time: bool = False
    sort_asc: bool = False
    page: int = 1
    per_page: int = PER_PAGE

    @property
    def broad(self) -> bool:
        return (self.query.empty and not self.author_ids and not self.channel_ids
                and not self.date_from and not self.date_to)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page


def build_search_sql(f: SearchFilters, hidden_channels, now: datetime | None = None):
    """Return (rows_sql, count_sql, params). Count is capped at MAX_RESULTS + 1."""
    params: dict = {"hidden": sorted(hidden_channels)}
    where = [HIDDEN_FILTER]

    text_sql, text_params = build_text_filter(f.query)
    if text_sql:
        where.append(text_sql)
        params.update(text_params)
    if f.author_ids:
        where.append("p.author_id = ANY(%(authors)s::bigint[])")
        params["authors"] = list(f.author_ids)
    if f.channel_ids:
        where.append("(p.chan_id = ANY(%(channels)s::bigint[]) OR c.parent_id = ANY(%(channels)s::bigint[]))")
        params["channels"] = list(f.channel_ids)
    if f.date_from:
        where.append("p.created_at >= %(date_from)s")
        params["date_from"] = f.date_from
    elif f.broad and not f.all_time:
        where.append("p.created_at >= %(date_from)s")
        params["date_from"] = (now or datetime.now(timezone.utc)) - timedelta(days=BROAD_SEARCH_DAYS)
    if f.date_to:
        where.append("p.created_at < %(date_to)s")
        params["date_to"] = f.date_to

    source = "visible_posts"
    where_sql = " AND ".join(f"({w.strip()})" for w in where)
    order = "ASC" if f.sort_asc else "DESC"

    rows_sql = f"""
        SELECT {POST_COLUMNS}
        FROM {source} p {POST_JOINS}
        WHERE {where_sql}
        ORDER BY p.created_at {order}, p.post_id {order}
        LIMIT %(limit)s OFFSET %(offset)s
    """
    count_sql = f"""
        SELECT count(*) AS n FROM (
            SELECT 1 FROM {source} p {POST_JOINS}
            WHERE {where_sql}
            LIMIT {MAX_RESULTS + 1}
        ) capped
    """
    limit = max(0, min(f.per_page, MAX_RESULTS - f.offset))
    params.update(limit=limit, offset=f.offset)
    return rows_sql, count_sql, params


def _iso(value):
    return value.isoformat() if value else None


def _str(value):
    return str(value) if value is not None else None


def serialize_post(row: dict, guild_id: int) -> dict:
    out = {
        "id": str(row["post_id"]),
        "channel_id": str(row["chan_id"]),
        "channel": row.get("channel_name") or f"unknown-{row['chan_id']}",
        "channel_parent_id": _str(row.get("channel_parent_id")),
        "author_id": str(row["author_id"]),
        "author_name": row.get("author_name") or "Unknown",
        "avatar_url": row.get("avatar_url"),
        "is_tracked": bool(row.get("is_tracked")),
        "content": row.get("content") or "",
        "created_at": _iso(row["created_at"]),
        "edited_at": _iso(row.get("edited_at")),
        "reason": row.get("reason"),
        "pinned": bool(row.get("pinned")),
        "edit_count": int(row.get("edit_count") or 0),
        "attachments": row.get("attachments") or [],
        "reply_to_id": _str(row.get("reply_to_id")),
        "replied_to": None,
        "mentions": {"users": {}, "channels": {}, "roles": {}},
        "jump_url": f"https://discord.com/channels/{guild_id}/{row['chan_id']}/{row['post_id']}",
    }
    return out


class Archive:
    """Query layer. The Flask app only talks to the database through this class."""

    def __init__(self, settings, pool: ConnectionPool | None = None):
        self.settings = settings
        self.hidden = sorted(settings.hidden_channels)
        self.pool = pool or ConnectionPool(
            settings.database_url, min_size=1, max_size=10, open=True,
            kwargs={"row_factory": dict_row, "autocommit": True,
                    "options": "-c statement_timeout=15000"},
        )

    def close(self):
        self.pool.close()

    def _all(self, sql, params=None):
        with self.pool.connection() as conn:
            return conn.execute(sql, params or {}).fetchall()

    def _one(self, sql, params=None):
        with self.pool.connection() as conn:
            return conn.execute(sql, params or {}).fetchone()

    def ping(self) -> bool:
        return self._one("SELECT 1 AS ok")["ok"] == 1

    # -- posts ---------------------------------------------------------------

    def search(self, f: SearchFilters, now: datetime | None = None) -> dict:
        rows_sql, count_sql, params = build_search_sql(f, self.hidden, now)
        with self.pool.connection() as conn, conn.transaction():
            if f.query.regexes:
                conn.execute(f"SET LOCAL statement_timeout = {REGEX_TIMEOUT_MS}")
            rows = conn.execute(rows_sql, params).fetchall() if params["limit"] else []
            total = conn.execute(count_sql, params).fetchone()["n"]
        posts = [serialize_post(r, self.settings.guild_id) for r in rows]
        self.attach_reply_context(posts)
        self.attach_mentions(posts)
        capped = total > MAX_RESULTS
        total = min(total, MAX_RESULTS)
        return {
            "results": posts,
            "total": total,
            "capped": capped,
            "page": f.page,
            "per_page": f.per_page,
            "total_pages": (total + f.per_page - 1) // f.per_page,
            "limited_to_recent": f.broad and not f.all_time and not f.date_from,
        }

    def attach_reply_context(self, posts: list[dict]) -> None:
        """Fill `replied_to` for posts whose reply target is stored and not hidden."""
        ids = sorted({int(p["reply_to_id"]) for p in posts if p["reply_to_id"]})
        if not ids:
            return
        rows = self._all(f"""
            SELECT p.post_id, p.chan_id, p.author_id, p.content, p.created_at,
                   {AUTHOR_NAME} AS author_name, COALESCE(m.is_tracked, FALSE) AS is_tracked
            FROM posts p {POST_JOINS}
            WHERE p.post_id = ANY(%(ids)s::bigint[]) AND {HIDDEN_FILTER}
        """, {"ids": ids, "hidden": self.hidden})
        targets = {str(r["post_id"]): r for r in rows}
        for post in posts:
            t = targets.get(post["reply_to_id"] or "")
            if t:
                post["replied_to"] = {
                    "id": str(t["post_id"]),
                    "channel_id": str(t["chan_id"]),
                    "author_id": str(t["author_id"]),
                    "author_name": t["author_name"],
                    "is_tracked": bool(t["is_tracked"]),
                    "content": t["content"] or "",
                    "created_at": _iso(t["created_at"]),
                    "jump_url": f"https://discord.com/channels/{self.settings.guild_id}/{t['chan_id']}/{t['post_id']}",
                }

    def attach_mentions(self, posts: list[dict]) -> None:
        """Give each post a `mentions` map of {users, channels, roles} names for the IDs in its text."""
        texts = {p["id"]: p["content"] + " " + ((p.get("replied_to") or {}).get("content") or "") for p in posts}
        found = {key: set() for key in MENTION_PATTERNS}
        for text in texts.values():
            for key, pattern in MENTION_PATTERNS.items():
                found[key].update(int(i) for i in pattern.findall(text))
        if not any(found.values()):
            for p in posts:
                p["mentions"] = {"users": {}, "channels": {}, "roles": {}}
            return
        names = {"users": {}, "channels": {}, "roles": {}}
        if found["users"]:
            for r in self._all(f"SELECT m.member_id AS id, {AUTHOR_NAME} AS name FROM members m "
                               "WHERE m.member_id = ANY(%(ids)s::bigint[])", {"ids": sorted(found["users"])}):
                names["users"][str(r["id"])] = r["name"]
        if found["channels"]:
            for r in self._all("SELECT chan_id AS id, name FROM channels WHERE chan_id = ANY(%(ids)s::bigint[])",
                               {"ids": sorted(found["channels"])}):
                names["channels"][str(r["id"])] = r["name"]
        if found["roles"]:
            for r in self._all("SELECT role_id AS id, name FROM roles WHERE role_id = ANY(%(ids)s::bigint[])",
                               {"ids": sorted(found["roles"])}):
                names["roles"][str(r["id"])] = r["name"]
        for p in posts:
            text = texts[p["id"]]
            p["mentions"] = {key: {i: names[key][i] for i in MENTION_PATTERNS[key].findall(text) if i in names[key]}
                             for key in MENTION_PATTERNS}

    def get_post(self, post_id: int, visible_only: bool = False) -> dict | None:
        source = "visible_posts" if visible_only else "posts"
        row = self._one(f"""
            SELECT {POST_COLUMNS} FROM {source} p {POST_JOINS}
            WHERE p.post_id = %(id)s AND {HIDDEN_FILTER}
        """, {"id": post_id, "hidden": self.hidden})
        if not row:
            return None
        post = serialize_post(row, self.settings.guild_id)
        self.attach_reply_context([post])
        self.attach_mentions([post])
        return post

    def revisions(self, post_id: int) -> list[dict] | None:
        """Revision history, oldest first; None if the post is missing or hidden."""
        if not self.get_post(post_id):
            return None
        rows = self._all("""
            SELECT rev_id, content, captured_at FROM post_revisions
            WHERE post_id = %(id)s ORDER BY captured_at, rev_id
        """, {"id": post_id})
        return [{"id": str(r["rev_id"]), "content": r["content"],
                 "captured_at": _iso(r["captured_at"])} for r in rows]

    # -- lists ---------------------------------------------------------------

    def tracked_authors(self) -> list[dict]:
        rows = self._all(f"""
            SELECT m.member_id, {AUTHOR_NAME} AS name, m.avatar_url
            FROM members m WHERE m.is_tracked
            ORDER BY lower({AUTHOR_NAME})
        """)
        return [{"id": str(r["member_id"]), "name": r["name"], "avatar_url": r["avatar_url"]} for r in rows]

    def channel_rows(self) -> list[dict]:
        """Non-thread channels that have visible posts (directly or in their threads)."""
        return self._all("""
            WITH active AS (
                SELECT p.chan_id, max(p.created_at) AS last_post FROM visible_posts p GROUP BY p.chan_id
            ),
            rolled AS (
                SELECT CASE WHEN c.type = ANY(%(threads)s) THEN c.parent_id ELSE c.chan_id END AS chan_id,
                       max(a.last_post) AS last_post, count(*) FILTER (WHERE c.type = ANY(%(threads)s)) AS threads
                FROM active a JOIN channels c ON c.chan_id = a.chan_id
                GROUP BY 1
            )
            SELECT c.chan_id, c.name, c.type, c.parent_id, cat.name AS category,
                   r.last_post, r.threads
            FROM rolled r
            JOIN channels c ON c.chan_id = r.chan_id
            LEFT JOIN channels cat ON cat.chan_id = c.parent_id
            WHERE c.chan_id <> ALL(%(hidden)s::bigint[])
              AND COALESCE(c.parent_id, 0) <> ALL(%(hidden)s::bigint[])
        """, {"threads": list(THREAD_TYPES), "hidden": self.hidden})

    def channels(self) -> list[dict]:
        return order_channels(self.channel_rows(), self.settings.featured_channels)

    def threads(self, parent_id: int) -> list[dict]:
        if parent_id in self.settings.hidden_channels:
            return []
        rows = self._all("""
            SELECT c.chan_id, c.name, max(p.created_at) AS last_post, count(*) AS posts
            FROM channels c JOIN visible_posts p ON p.chan_id = c.chan_id
            LEFT JOIN channels pc ON pc.chan_id = c.parent_id
            WHERE c.parent_id = %(parent)s
              AND c.chan_id <> ALL(%(hidden)s::bigint[])
              AND COALESCE(pc.parent_id, 0) <> ALL(%(hidden)s::bigint[])
            GROUP BY c.chan_id, c.name ORDER BY last_post DESC
        """, {"parent": parent_id, "hidden": self.hidden})
        return [{"id": str(r["chan_id"]), "name": r["name"], "last_post": _iso(r["last_post"]),
                 "posts": r["posts"]} for r in rows]

    def stats(self) -> dict:
        row = self._one("""
            SELECT
              (SELECT count(*) FROM visible_posts p
                 LEFT JOIN channels c ON c.chan_id = p.chan_id
                 LEFT JOIN channels pc ON pc.chan_id = c.parent_id
                WHERE """ + HIDDEN_FILTER + """) AS visible_posts,
              (SELECT count(*) FROM members WHERE is_tracked) AS tracked_authors,
              (SELECT count(*) FROM posts) AS stored_posts,
              (SELECT max(created_at) FROM visible_posts) AS latest_post
        """, {"hidden": self.hidden})
        return {"total_posts": row["visible_posts"], "tracked_authors": row["tracked_authors"],
                "stored_posts": row["stored_posts"], "latest_post": _iso(row["latest_post"])}


def order_channels(rows, featured: dict[int, str]) -> list[dict]:
    """Featured channels first (config order, config names), then the rest by latest activity."""
    order = {cid: i for i, cid in enumerate(featured)}
    out_featured, others = [], []
    for r in rows:
        cid = r["chan_id"]
        item = {
            "id": str(cid),
            "name": featured.get(cid, r["name"]),
            "category": r.get("category"),
            "type": r.get("type"),
            "featured": cid in featured,
            "threads": int(r.get("threads") or 0),
            "last_post": _iso(r.get("last_post")),
        }
        (out_featured if cid in featured else others).append(item)
    out_featured.sort(key=lambda c: order[int(c["id"])])
    epoch = datetime.min.replace(tzinfo=timezone.utc).isoformat()
    others.sort(key=lambda c: c["last_post"] or epoch, reverse=True)
    return out_featured + others
