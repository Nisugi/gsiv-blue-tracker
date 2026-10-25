"""Store tests against a real, disposable Postgres. Skipped unless TEST_DATABASE_URL is set.
The database is wiped: never point this at a real archive."""
import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import asyncpg
import discord
import pytest

from archiver.bot.store import Store

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def fake_message(mid, content="hello", author_id=7, chan_id=10, reply_to=None):
    chan = NS(id=chan_id, name=f"chan{chan_id}", type=discord.ChannelType.text, topic=None, position=1,
              category_id=5)
    author = NS(id=author_id, name=f"user{author_id}", display_name=f"User {author_id}",
                display_avatar=NS(url="https://cdn/avatar.png"))
    attachment = NS(id=mid * 10, filename="a.png", url="https://cdn/a.png", content_type="image/png",
                    size=10, width=1, height=1)
    embed = NS(to_dict=lambda: {"title": "t"})
    mentioned = NS(id=55, name="mentioned", display_name="Mentioned Person", display_avatar=None)
    mentioned_chan = NS(id=77, name="patch-notes", type=discord.ChannelType.text, topic=None, position=2,
                        category_id=5)
    return NS(id=mid, channel=chan, author=author, content=content, created_at=T0, edited_at=None,
              reference=NS(message_id=reply_to) if reply_to else None, pinned=False,
              attachments=[attachment], embeds=[embed], mentions=[mentioned, author],
              channel_mentions=[mentioned_chan])


def run(coro):
    return asyncio.run(coro)


async def with_store(fn):
    conn = await asyncpg.connect(URL)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    store = Store(URL)
    await store.connect()
    try:
        await store.init_schema()
        await store.init_schema()  # idempotent
        return await fn(store)
    finally:
        await store.close()


def test_save_and_refresh_post():
    async def body(s):
        await s.save_post(fake_message(1, reply_to=99), "author", author_tracked=True, notify=True)
        await s.save_post(fake_message(1, content="refreshed"), "reply_target", author_tracked=False)
        row = await s.pool.fetchrow("SELECT * FROM posts WHERE post_id = 1")
        assert row["content"] == "refreshed" and row["reason"] == "author"
        assert await s.pool.fetchval("SELECT is_tracked FROM members WHERE member_id = 7")
        assert await s.pool.fetchval("SELECT count(*) FROM attachments WHERE post_id = 1") == 1
        assert await s.pool.fetchval("SELECT count(*) FROM embeds WHERE post_id = 1") == 1
        assert await s.pool.fetchval("SELECT parent_id FROM channels WHERE chan_id = 10") == 5
        assert await s.post_exists(1) and not await s.post_exists(2)
    run(with_store(body))


def test_mentioned_users_and_channels_get_names():
    async def body(s):
        await s.save_post(fake_message(1), "author", author_tracked=True)
        row = await s.pool.fetchrow("SELECT display_name, is_tracked FROM members WHERE member_id = 55")
        assert row["display_name"] == "Mentioned Person" and not row["is_tracked"]
        assert await s.pool.fetchval("SELECT name FROM channels WHERE chan_id = 77") == "patch-notes"
    run(with_store(body))


def test_roles_sync_replaces_list():
    async def body(s):
        role = lambda i, n: NS(id=i, name=n, color=NS(value=0))
        await s.sync_roles([role(1, "GameMaster"), role(2, "Old")])
        await s.sync_roles([role(1, "Senior GM"), role(3, "New")])
        rows = await s.pool.fetch("SELECT role_id, name FROM roles ORDER BY role_id")
        assert [(r["role_id"], r["name"]) for r in rows] == [(1, "Senior GM"), (3, "New")]
        await s.delete_role(3)
        assert await s.pool.fetchval("SELECT count(*) FROM roles") == 1
    run(with_store(body))


def test_notify_sent_for_visible_posts():
    async def body(s):
        got = []
        listener = await asyncpg.connect(URL)
        await listener.add_listener("new_post", lambda *a: got.append(a[3]))
        await s.save_post(fake_message(1), "author", author_tracked=True, notify=True)
        await s.save_post(fake_message(2), "author", author_tracked=True, notify=False)
        await asyncio.sleep(0.2)
        await listener.close()
        assert got == ["1"]
    run(with_store(body))


def test_edit_keeps_previous_content():
    async def body(s):
        await s.save_post(fake_message(1, content="v1"), "author", author_tracked=True)
        assert await s.record_edit(1, "v2", None)
        assert not await s.record_edit(1, "v2", None)       # unchanged content
        assert not await s.record_edit(404, "x", None)      # not stored
        assert await s.pool.fetchval("SELECT content FROM posts WHERE post_id = 1") == "v2"
        assert await s.pool.fetchval("SELECT content FROM post_revisions WHERE post_id = 1") == "v1"
    run(with_store(body))


def test_delete_removes_post_and_everything_attached():
    async def body(s):
        await s.save_post(fake_message(1, content="v1"), "author", author_tracked=True)
        await s.record_edit(1, "v2", None)
        assert await s.record_delete(1)
        assert not await s.record_delete(1)
        assert not await s.record_delete(404)
        for table in ("posts", "post_revisions", "attachments", "embeds"):
            assert await s.pool.fetchval(f"SELECT count(*) FROM {table}") == 0, table
    run(with_store(body))


def test_tracked_flag_is_sticky_and_seeded():
    async def body(s):
        await s.seed_tracked_users({7: "Seven"})
        await s.save_post(fake_message(1, author_id=7), "author", author_tracked=False)
        row = await s.pool.fetchrow("SELECT is_tracked, tracked_name FROM members WHERE member_id = 7")
        assert row["is_tracked"] and row["tracked_name"] == "Seven"
        assert await s.tracked_ids() == {7}
    run(with_store(body))


def test_crawl_state_only_moves_forward():
    async def body(s):
        assert await s.last_seen(10) is None
        await s.set_last_seen(10, 500)
        await s.set_last_seen(10, 300)
        assert await s.last_seen(10) == 500
        assert await s.oldest_crawl_time() is not None
    run(with_store(body))
