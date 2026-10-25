"""Legacy migration test against disposable Postgres databases.
Skipped unless TEST_DATABASE_URL and TEST_LEGACY_DATABASE_URL are set; both are wiped."""
import asyncio
import importlib.util
import os
from pathlib import Path

import asyncpg
import pytest

from archiver.bot.store import Store

NEW, OLD = os.getenv("TEST_DATABASE_URL"), os.getenv("TEST_LEGACY_DATABASE_URL")
pytestmark = pytest.mark.skipif(not (NEW and OLD), reason="test database URLs not set")

_spec = importlib.util.spec_from_file_location(
    "migrate_legacy", Path(__file__).resolve().parent.parent / "scripts" / "migrate_legacy.py")
migrate_legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_legacy)

# The parts of the legacy schema the migration reads
LEGACY_SCHEMA = """
CREATE TABLE members(member_id TEXT PRIMARY KEY, username TEXT, display_name TEXT, avatar_url TEXT,
                     is_bot BOOLEAN, is_gm BOOLEAN DEFAULT FALSE, joined_at BIGINT);
CREATE TABLE channels(chan_id TEXT PRIMARY KEY, guild_id TEXT, parent_id TEXT, name TEXT, type TEXT,
                      topic TEXT, accessible BOOLEAN);
CREATE TABLE posts(post_id TEXT PRIMARY KEY, chan_id TEXT, author_id TEXT, content TEXT, created_ts BIGINT,
                   edited_ts BIGINT, pinned BOOLEAN, deleted BOOLEAN DEFAULT FALSE, reply_to_id TEXT);
CREATE TABLE post_revisions(rev_id BIGSERIAL PRIMARY KEY, post_id TEXT, chan_id TEXT, author_id TEXT,
                            content TEXT, captured_ts BIGINT, is_edit BOOLEAN);
CREATE TABLE attachments(attach_id TEXT PRIMARY KEY, post_id TEXT, filename TEXT, url TEXT,
                         content_type TEXT, size INTEGER, width INTEGER, height INTEGER);
CREATE TABLE embeds(embed_id BIGSERIAL PRIMARY KEY, post_id TEXT, type TEXT, data_json TEXT);
CREATE TABLE gm_names(author_id TEXT PRIMARY KEY, gm_name TEXT NOT NULL);

INSERT INTO members VALUES ('7','gm','GM Seven',NULL,FALSE,TRUE,NULL), ('8','player','Player',NULL,FALSE,FALSE,NULL);
INSERT INTO gm_names VALUES ('7','Seven');
INSERT INTO channels VALUES ('5',NULL,NULL,'General','category',NULL,TRUE),
                            ('10',NULL,'5','chatter','ChannelType.text',NULL,TRUE);
INSERT INTO posts VALUES
  ('100','10','8','question?',1700000000000,NULL,FALSE,FALSE,NULL),   -- reply target
  ('101','10','7','answer v3',1700000001000,1700000300000,FALSE,FALSE,'100'),
  ('102','10','8','unrelated chatter',1700000002000,NULL,FALSE,FALSE,NULL),
  ('103','10','7','oops',1700000003000,NULL,FALSE,TRUE,NULL);
INSERT INTO post_revisions (post_id, content, captured_ts, is_edit) VALUES
  ('101','answer v2',1700000100000,TRUE), ('101','answer v3',1700000300000,TRUE),
  ('103','[[DELETED]]',1700000400000,TRUE);
INSERT INTO attachments VALUES ('1001','101','a.png','https://cdn/a.png','image/png',1,1,1);
INSERT INTO embeds (post_id, type, data_json) VALUES ('101','rich','{"title":"t"}');
"""


async def setup_and_migrate():
    for url in (OLD, NEW):
        c = await asyncpg.connect(url)
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await c.close()
    old = await asyncpg.connect(OLD)
    await old.execute(LEGACY_SCHEMA)
    store = Store(NEW)
    await store.connect()
    await store.init_schema()
    await store.close()

    new = await asyncpg.connect(NEW)
    try:
        await migrate_legacy.migrate(old, new, dry_run=False)
        await migrate_legacy.migrate(old, new, dry_run=False)  # re-runnable
        return {
            "posts": {r["post_id"]: dict(r) for r in await new.fetch("SELECT * FROM posts")},
            "revisions": [tuple(r) for r in await new.fetch(
                "SELECT post_id, kind, content FROM post_revisions ORDER BY post_id, captured_at")],
            "member7": dict(await new.fetchrow("SELECT * FROM members WHERE member_id = 7")),
            "channels": {r["chan_id"]: dict(r) for r in await new.fetch("SELECT * FROM channels")},
            "counts": [await new.fetchval(f"SELECT count(*) FROM {t}") for t in ("attachments", "embeds")],
        }
    finally:
        await old.close()
        await new.close()


@pytest.fixture(scope="module")
def result():
    return asyncio.run(setup_and_migrate())


def test_only_tracked_posts_and_reply_targets(result):
    posts = result["posts"]
    assert set(posts) == {100, 101, 103}
    assert posts[100]["reason"] == "reply_target" and posts[101]["reason"] == "author"
    assert posts[101]["reply_to_id"] == 100 and posts[101]["edited_at"] is not None


def test_delete_and_edit_history_converted(result):
    assert result["posts"][103]["deleted_at"] is not None
    assert result["revisions"] == [(101, "edit", "answer v2"), (103, "delete", "oops")]


def test_members_channels_and_children(result):
    assert result["member7"]["is_tracked"] and result["member7"]["tracked_name"] == "Seven"
    assert result["channels"][10]["type"] == "text" and result["channels"][10]["parent_id"] == 5
    assert 5 in result["channels"]
    assert result["counts"] == [1, 1]
