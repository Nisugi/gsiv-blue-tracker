"""History crawler. Each channel/thread resumes after the newest message it has seen,
so restarts never leave gaps. Channels never crawled before go back `backfill_days`."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

log = logging.getLogger(__name__)

MESSAGEABLE = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)
CHECKPOINT_EVERY = 100  # messages; one history page


class Crawler:
    def __init__(self, guild: discord.Guild, tracker, store, settings, backfill_days: float | None = None):
        self.guild = guild
        self.tracker = tracker
        self.store = store
        self.settings = settings
        days = settings.backfill_days if backfill_days is None else backfill_days
        self.started = datetime.now(timezone.utc)
        self.cutoff = self.started - timedelta(days=days)
        self.saved = 0

    async def run(self):
        log.info("Crawl started (new channels back to %s)", self.cutoff.isoformat(timespec="minutes"))
        # Archived threads last active before both the cutoff and the last crawl can't hold unseen messages
        last_crawl = await self.store.oldest_crawl_time()
        self.thread_horizon = min(self.cutoff, last_crawl) if last_crawl else self.cutoff

        for channel in self.guild.channels:
            if self.tracker.is_excluded(channel):
                continue
            if isinstance(channel, MESSAGEABLE):
                await self.crawl(channel)
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel)) and \
                    channel.id not in self.settings.skip_crawl:
                async for thread in self._threads(channel):
                    await self.crawl(thread)
        log.info("Crawl finished: %d messages saved", self.saved)

    async def _threads(self, parent):
        for thread in parent.threads:            # active threads, from the gateway cache
            yield thread
        if not parent.permissions_for(self.guild.me).read_message_history:
            return
        try:
            async for thread in parent.archived_threads(limit=None):  # newest archive first
                if thread.archive_timestamp < self.thread_horizon:
                    break
                yield thread
        except discord.HTTPException as e:
            log.warning("Archived threads unavailable in #%s: %s", parent.name, e)

    async def crawl(self, channel):
        perms = channel.permissions_for(self.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            await self.store.upsert_channel(channel, accessible=False)
            return

        last_seen = await self.store.last_seen(channel.id)
        after = discord.Object(id=last_seen) if last_seen else self.cutoff
        newest, count = None, 0
        try:
            async for msg in channel.history(limit=None, after=after, oldest_first=True):
                if await self.tracker.handle(msg):
                    self.saved += 1
                newest, count = msg.id, count + 1
                if count % CHECKPOINT_EVERY == 0:
                    await self.store.set_last_seen(channel.id, newest)
                    await asyncio.sleep(self.settings.request_pause)
        except discord.Forbidden:
            await self.store.upsert_channel(channel, accessible=False)
            return
        except discord.HTTPException as e:
            log.warning("Stopped crawling #%s: %s", channel.name, e)
            if newest:
                await self.store.set_last_seen(channel.id, newest)
            return
        # Everything up to the crawl start has been read; later messages arrive live
        await self.store.set_last_seen(channel.id, max(newest or 0, discord.utils.time_snowflake(self.started)))
        if count:
            log.info("#%s: %d messages read", channel.name, count)
