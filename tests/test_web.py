import json
import queue
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from archiver.web.app import _date, create_app, filters_from_args
from archiver.web.live import Broadcaster
from archiver.web.queries import (
    MAX_RESULTS, SearchFilters, build_search_sql, order_channels, serialize_post,
)
from archiver.web.search import build_text_filter, parse_search_query

GUILD = 226045346399256576
BIG_ID = 1234567890123456789  # beyond JS Number precision

SETTINGS = SimpleNamespace(
    database_url="postgresql://unused", guild_id=GUILD, server_name="GemStone IV", label="GM",
    hidden_channels=frozenset({613879283038814228}),
    featured_channels={387271714012135425: "help", 387270949499830273: "game-chatter"},
)


# -- query parser -------------------------------------------------------------

def test_parse_words_are_or_terms():
    p = parse_search_query("dragon  sword")
    assert p.words == ["dragon", "sword"]
    assert not p.phrases and not p.and_groups and not p.excluded


def test_parse_phrase_and_group_regex_exclusion():
    p = parse_search_query('"ebon gate" loot + chest /bo+ss/i -spam -"free gift" extra')
    assert p.phrases == ["ebon gate"]
    assert p.and_groups == [["loot", "chest"]]
    assert p.regexes == [("bo+ss", True)]
    assert p.excluded == ["free gift", "spam"]
    assert p.words == ["extra"]


def test_parse_and_chain_and_case_sensitive_regex():
    p = parse_search_query("a + b + c /Exact/")
    assert p.and_groups == [["a", "b", "c"]]
    assert p.regexes == [("Exact", False)]


def test_parse_hyphenated_word_is_not_exclusion():
    p = parse_search_query("co-op")
    assert p.words == ["co-op"] and not p.excluded


def test_parse_empty():
    assert parse_search_query("").empty
    assert parse_search_query("   ").empty


def test_text_filter_sql_is_parameterized():
    sql, params = build_text_filter(parse_search_query('"xq yq" aq + bq zq -nq /rq/i'))
    assert "phraseto_tsquery('english', %(q" in sql
    assert " && " in sql and " || " in sql
    assert "p.content ~* %(" in sql
    assert "NOT p.tsv @@" in sql
    assert set(params.values()) == {"xq yq", "aq", "bq", "zq", "nq", "rq"}
    for value in params.values():
        assert value not in sql


def test_text_filter_empty():
    assert build_text_filter(parse_search_query("")) == (None, {})


def test_injection_attempt_stays_in_params():
    sql, params = build_text_filter(parse_search_query("x'); DROP TABLE posts; --"))
    assert "DROP" not in sql
    assert any("DROP" in v for v in params.values())


# -- SQL building ---------------------------------------------------------------

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_default_search_uses_visible_posts_and_recent_window():
    rows_sql, count_sql, params = build_search_sql(SearchFilters(), SETTINGS.hidden_channels, NOW)
    assert "FROM visible_posts p" in rows_sql and "FROM visible_posts p" in count_sql
    assert params["hidden"] == [613879283038814228]
    assert "%(hidden)s" in rows_sql
    assert params["date_from"] == datetime(2026, 7, 3, tzinfo=timezone.utc)
    assert "ORDER BY p.created_at DESC" in rows_sql
    assert params["limit"] == 25 and params["offset"] == 0
    assert f"LIMIT {MAX_RESULTS + 1}" in count_sql


def test_all_time_drops_window_and_filters_apply():
    f = SearchFilters(query=parse_search_query("rift"), author_ids=[1, 2], channel_ids=[3],
                      sort_asc=True, page=3, all_time=True)
    rows_sql, _, params = build_search_sql(f, set(), NOW)
    assert "date_from" not in params
    assert params["authors"] == [1, 2] and params["channels"] == [3]
    assert "c.parent_id = ANY(%(channels)s" in rows_sql  # threads of a selected channel
    assert "ORDER BY p.created_at ASC" in rows_sql
    assert params["offset"] == 50


def test_query_alone_is_not_broad():
    _, _, params = build_search_sql(SearchFilters(query=parse_search_query("x")), set(), NOW)
    assert "date_from" not in params


def test_pagination_capped_at_max_results():
    f = SearchFilters(page=400, per_page=25, all_time=True)
    assert build_search_sql(f, set(), NOW)[2]["limit"] == 25
    f = SearchFilters(page=401, per_page=25, all_time=True)
    assert build_search_sql(f, set(), NOW)[2]["limit"] == 0


def test_surprise_uses_all_posts_and_deleted_filter():
    f = SearchFilters(all_posts=True, deleted_only=True)
    rows_sql, _, _ = build_search_sql(f, set(), NOW)
    assert "FROM posts p" in rows_sql and "p.deleted_at IS NOT NULL" in rows_sql


def test_serialize_post_ids_are_strings():
    row = dict(post_id=BIG_ID, chan_id=BIG_ID + 1, author_id=BIG_ID + 2, content="hi",
               created_at=NOW, reply_to_id=BIG_ID - 1, reason="author", channel_parent_id=None)
    out = serialize_post(row, GUILD)
    assert out["id"] == str(BIG_ID) and out["channel_id"] == str(BIG_ID + 1)
    assert out["author_id"] == str(BIG_ID + 2) and out["reply_to_id"] == str(BIG_ID - 1)
    assert out["jump_url"] == f"https://discord.com/channels/{GUILD}/{BIG_ID + 1}/{BIG_ID}"
    assert out["created_at"] == "2026-10-01T00:00:00+00:00"


def test_order_channels_featured_first_with_display_names():
    rows = [
        dict(chan_id=1, name="old", last_post=datetime(2020, 1, 1, tzinfo=timezone.utc)),
        dict(chan_id=387270949499830273, name="game-chatter-raw", last_post=None),
        dict(chan_id=2, name="new", last_post=datetime(2025, 1, 1, tzinfo=timezone.utc)),
        dict(chan_id=387271714012135425, name="help-raw", last_post=None),
    ]
    out = order_channels(rows, SETTINGS.featured_channels)
    assert [c["name"] for c in out] == ["help", "game-chatter", "new", "old"]
    assert out[0]["featured"] and not out[2]["featured"]
    assert all(isinstance(c["id"], str) for c in out)


# -- request parsing --------------------------------------------------------------

def test_filters_from_args():
    f = filters_from_args({"q": '"a b"', "authors": f"{BIG_ID},junk,5", "channels": "7",
                           "date_from": "2025-01-02", "date_to": "2025-01-05",
                           "page": "-3", "per_page": "999", "sort": "asc"})
    assert f.author_ids == [BIG_ID, 5] and f.channel_ids == [7]
    assert f.page == 1 and f.per_page == 100 and f.sort_asc
    assert f.date_to == datetime(2025, 1, 6, tzinfo=timezone.utc)  # whole end day included
    assert not f.all_posts and not f.deleted_only


def test_bad_date_ignored():
    assert _date("not-a-date") is None


# -- routes (query layer stubbed) ----------------------------------------------------

def post(pid, **extra):
    base = {"id": str(pid), "channel_id": "10", "content": "hello", "reply_to_id": None,
            "replied_to": None}
    base.update(extra)
    return base


class FakeArchive:
    def __init__(self):
        self.searches = []
        self.posts = {BIG_ID: post(BIG_ID)}

    def search(self, f):
        self.searches.append(f)
        return {"results": [post(BIG_ID)], "total": 1, "capped": False, "page": f.page,
                "per_page": f.per_page, "total_pages": 1, "limited_to_recent": f.broad}

    def get_post(self, post_id, visible_only=False):
        return self.posts.get(post_id)

    def revisions(self, post_id):
        if post_id not in self.posts:
            return None
        return [{"id": "1", "kind": "edit", "content": "old", "captured_at": "2026-01-01T00:00:00+00:00"}]

    def tracked_authors(self):
        return [{"id": str(BIG_ID), "name": "Naijin", "avatar_url": None}]

    def members(self):
        return []

    def channels(self):
        return [{"id": "1", "name": "help", "featured": True}]

    def threads(self, chan_id):
        return []

    def stats(self):
        return {"total_posts": 5, "tracked_authors": 2, "stored_posts": 9, "latest_post": None}

    def ping(self):
        return True


class FakeBroadcaster:
    client_count = 0

    def subscribe(self):
        q = queue.Queue()
        q.put(json.dumps(post(BIG_ID)))
        return q

    def unsubscribe(self, q):
        pass


@pytest.fixture
def client_and_archive():
    archive = FakeArchive()
    app = create_app(SETTINGS, archive=archive, broadcaster=FakeBroadcaster())
    app.testing = True
    return app.test_client(), archive


def test_index_uses_label_and_server_name(client_and_archive):
    client, _ = client_and_archive
    html = client.get("/").get_data(as_text=True)
    assert "GemStone IV GM Tracker" in html
    assert "/static/common.js" in html
    assert "surprise" in client.get("/surprise").get_data(as_text=True).lower()


def test_search_route_parses_and_caches(client_and_archive):
    client, archive = client_and_archive
    r = client.get("/api/search?q=dragon+%2B+sword&authors=5")
    assert r.status_code == 200
    body = r.get_json()
    assert body["results"][0]["id"] == str(BIG_ID)
    assert archive.searches[0].query.and_groups == [["dragon", "sword"]]
    assert archive.searches[0].author_ids == [5]
    client.get("/api/search?q=dragon+%2B+sword&authors=5")
    assert len(archive.searches) == 1  # served from cache
    client.get("/api/search?q=other")
    assert len(archive.searches) == 2


def test_surprise_search_reads_all_posts(client_and_archive):
    client, archive = client_and_archive
    client.get("/api/surprise_search?deleted=1&members=3")
    f = archive.searches[0]
    assert f.all_posts and f.deleted_only and f.author_ids == [3]


def test_list_endpoints(client_and_archive):
    client, _ = client_and_archive
    assert client.get("/api/authors").get_json()[0]["id"] == str(BIG_ID)
    assert client.get("/api/channels").get_json()[0]["featured"] is True
    assert client.get("/api/stats").get_json()["total_posts"] == 5
    assert client.get("/api/channels/1/threads").get_json() == []
    assert client.get("/api/members").get_json() == []


def test_post_and_revisions(client_and_archive):
    client, _ = client_and_archive
    assert client.get(f"/api/posts/{BIG_ID}").get_json()["id"] == str(BIG_ID)
    assert client.get("/api/posts/42").status_code == 404
    assert client.get("/api/posts/42").get_json() == {"error": "Not found"}
    assert client.get(f"/api/posts/{BIG_ID}/revisions").get_json()[0]["content"] == "old"
    assert client.get("/api/posts/42/revisions").status_code == 404


def test_health(client_and_archive):
    client, _ = client_and_archive
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"


def test_legacy_notify_endpoint_is_gone(client_and_archive):
    client, _ = client_and_archive
    assert client.post("/api/notify_gm_post", json={}).status_code in (404, 405)


def test_stream_sends_post_events(client_and_archive):
    client, _ = client_and_archive
    r = client.get("/stream", buffered=False)
    assert r.mimetype == "text/event-stream"
    chunks = r.response
    assert next(chunks).decode().startswith("retry:")
    event = next(chunks).decode()
    assert event.startswith("event: post\ndata: ")
    assert json.loads(event.split("data: ", 1)[1])["id"] == str(BIG_ID)
    r.close()


# -- broadcaster fan-out ---------------------------------------------------------------

def test_broadcaster_loads_once_and_fans_out(monkeypatch):
    loads = []

    def load(pid):
        loads.append(pid)
        return None if pid == 2 else {"id": str(pid)}

    b = Broadcaster("postgresql://unused", load)
    monkeypatch.setattr(b, "_run", lambda: None)  # no DB thread
    q1, q2 = b.subscribe(), b.subscribe()
    b.publish(str(BIG_ID))
    b.publish("2")          # not visible (hidden or reply target): skipped
    b.publish("garbage")    # malformed: ignored
    assert loads == [BIG_ID, 2]
    assert json.loads(q1.get_nowait()) == {"id": str(BIG_ID)}
    assert json.loads(q2.get_nowait()) == {"id": str(BIG_ID)}
    assert q1.empty() and q2.empty()
    b.unsubscribe(q1)
    assert b.client_count == 1
