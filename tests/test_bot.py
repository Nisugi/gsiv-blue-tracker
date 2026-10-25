"""Bot logic tests with fake Discord objects and an in-memory store (no Discord, no Postgres)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import discord
import pytest

from archiver.bot.crawler import Crawler
from archiver.bot.tracker import Tracker
from archiver.settings import Settings

GUILD = 1000
TRACKED_ROLE = 42


def make_settings(**kw):
    base = dict(
        token=None, database_url="x", guild_id=GUILD, server_name="Test", label="GM",
        role_ids=frozenset({TRACKED_ROLE}), users={7: "Seeded GM"}, save_reply_targets=True,
        hidden_channels=frozenset({30}), exclude_channels=frozenset({20}), skip_crawl=frozenset(),
        featured_channels={}, backfill_days=7, request_pause=0,
    )
    base.update(kw)
    return Settings(**base)


class FakeStore:
    def __init__(self):
        self.posts = {}        # id -> (reason, notify)
        self.flagged = set()
        self.last = {}
        self.inaccessible = set()

    async def seed_tracked_users(self, users): self.flagged |= set(users)
    async def tracked_ids(self): return set(self.flagged)
    async def post_exists(self, pid): return pid in self.posts
    async def last_seen(self, cid): return self.last.get(cid)
    async def set_last_seen(self, cid, mid): self.last[cid] = max(self.last.get(cid, 0), mid)
    async def oldest_crawl_time(self): return None
    async def upsert_channel(self, ch, accessible=True):
        if not accessible:
            self.inaccessible.add(ch.id)

    async def save_post(self, msg, reason, *, author_tracked, notify=False):
        old = self.posts.get(msg.id, (None,))[0]
        self.posts[msg.id] = ("author" if old == "author" else reason, notify)
        if author_tracked:
            self.flagged.add(msg.author.id)


def user(uid, roles=(), bot=False, member=True):
    u = NS(id=uid, bot=bot, display_name=f"user{uid}", name=f"user{uid}")
    if member:
        u.roles = [NS(id=r) for r in roles]
    return u


class Chan:
    def __init__(self, cid=10, parent_id=None, messages=None):
        self.id, self.parent_id, self.name = cid, parent_id, f"chan{cid}"
        self.messages = {m.id: m for m in messages or []}

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(NS(status=404, reason="Not Found"), "Unknown Message")
        return self.messages[mid]


def message(mid, author, chan=None, ref=None, guild=GUILD):
    m = discord.Message.__new__(discord.Message)
    m.id, m.author, m.reference = mid, author, ref
    m.channel = chan or Chan()
    m.guild = NS(id=guild) if guild else None
    return m


def reply(mid, resolved=None, chan_id=10, kind=discord.MessageReferenceType.reply):
    return NS(message_id=mid, channel_id=chan_id, resolved=resolved, type=kind)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tracker():
    t = Tracker(make_settings(), FakeStore())
    run(t.load())
    return t


# ── Who is tracked ───────────────────────────────────────────────────

def test_tracked_by_configured_user(tracker):
    assert tracker.is_tracked(user(7))


def test_tracked_by_role(tracker):
    assert tracker.is_tracked(user(1, roles=[TRACKED_ROLE]))


def test_untracked_role_and_bots(tracker):
    assert not tracker.is_tracked(user(2, roles=[99]))
    assert not tracker.is_tracked(user(7, bot=True))
    assert not tracker.is_tracked(None)


def test_former_member_tracked_only_if_known(tracker):
    assert not tracker.is_tracked(user(3, member=False))
    assert tracker.is_tracked(user(7, member=False))


def test_role_match_is_remembered_after_role_is_lost(tracker):
    run(tracker.handle(message(100, user(5, roles=[TRACKED_ROLE]))))
    assert tracker.is_tracked(user(5, roles=[]))
    assert 5 in tracker.store.flagged


# ── What gets saved ──────────────────────────────────────────────────

def test_untracked_message_not_saved(tracker):
    assert not run(tracker.handle(message(100, user(2))))
    assert tracker.store.posts == {}


def test_other_guild_and_dm_ignored(tracker):
    assert not run(tracker.handle(message(100, user(7), guild=999)))
    assert not run(tracker.handle(message(101, user(7), guild=None)))


def test_excluded_channel_and_its_threads(tracker):
    assert not run(tracker.handle(message(100, user(7), chan=Chan(20))))
    assert not run(tracker.handle(message(101, user(7), chan=Chan(21, parent_id=20))))


def test_hidden_channel_saved_without_notify(tracker):
    run(tracker.handle(message(100, user(7), chan=Chan(30))))
    run(tracker.handle(message(101, user(7), chan=Chan(31, parent_id=30))))
    run(tracker.handle(message(102, user(7))))
    assert tracker.store.posts == {100: ("author", False), 101: ("author", False), 102: ("author", True)}


# ── Reply targets ────────────────────────────────────────────────────

def test_resolved_reply_target_saved(tracker):
    target = message(200, user(2))
    run(tracker.handle(message(100, user(7), ref=reply(200, target))))
    assert tracker.store.posts[200] == ("reply_target", False)


def test_unresolved_reply_target_fetched(tracker):
    chan = Chan(messages=[message(201, user(2))])
    run(tracker.handle(message(100, user(7), chan=chan, ref=reply(201))))
    assert 201 in tracker.store.posts


def test_reply_target_by_tracked_author_saved_as_author(tracker):
    run(tracker.handle(message(100, user(7), ref=reply(200, message(200, user(1, roles=[TRACKED_ROLE]))))))
    assert tracker.store.posts[200][0] == "author"


def test_stored_author_post_not_downgraded(tracker):
    run(tracker.handle(message(200, user(7))))
    run(tracker.handle(message(100, user(7), ref=reply(200, message(200, user(7))))))
    assert tracker.store.posts[200][0] == "author"


@pytest.mark.parametrize("ref", [
    reply(404),                                               # fetch fails
    reply(202, chan_id=99),                                   # other channel
    reply(203, kind=discord.MessageReferenceType.forward),    # forward, not a reply
    reply(204, discord.DeletedReferencedMessage.__new__(discord.DeletedReferencedMessage)),
])
def test_reply_targets_skipped(tracker, ref):
    run(tracker.handle(message(100, user(7), ref=ref)))
    assert list(tracker.store.posts) == [100]


def test_reply_targets_disabled():
    t = Tracker(make_settings(save_reply_targets=False), FakeStore())
    run(t.load())
    run(t.handle(message(100, user(7), ref=reply(200, message(200, user(2))))))
    assert list(t.store.posts) == [100]


# ── Crawler ──────────────────────────────────────────────────────────

class CrawlChan(Chan):
    def __init__(self, cid, history, can_read=True):
        super().__init__(cid)
        self.history_msgs, self.can_read = history, can_read
        self.after_arg = None

    def permissions_for(self, _me):
        return NS(view_channel=self.can_read, read_message_history=self.can_read)

    async def history(self, limit, after, oldest_first):
        self.after_arg = after
        for m in self.history_msgs:
            yield m


def crawler_for(tracker):
    return Crawler(NS(me=None, channels=[]), tracker, tracker.store, tracker.settings)


def test_crawl_saves_tracked_and_checkpoints(tracker):
    ch = CrawlChan(10, [])
    ch.history_msgs = [message(i, user(7 if i % 2 else 2), chan=ch) for i in range(1, 6)]
    c = crawler_for(tracker)
    run(c.crawl(ch))
    assert sorted(tracker.store.posts) == [1, 3, 5]
    assert tracker.store.last[10] >= discord.utils.time_snowflake(c.started)


def test_crawl_new_channel_starts_at_cutoff(tracker):
    ch = CrawlChan(10, [])
    c = crawler_for(tracker)
    run(c.crawl(ch))
    assert ch.after_arg == c.cutoff
    assert c.cutoff < datetime.now(timezone.utc) - timedelta(days=6)


def test_crawl_resumes_after_last_seen(tracker):
    tracker.store.last[10] = 555
    ch = CrawlChan(10, [])
    run(crawler_for(tracker).crawl(ch))
    assert ch.after_arg.id == 555


def test_crawl_marks_unreadable_channel(tracker):
    ch = CrawlChan(10, [message(1, user(7))], can_read=False)
    run(crawler_for(tracker).crawl(ch))
    assert tracker.store.inaccessible == {10} and tracker.store.posts == {}
