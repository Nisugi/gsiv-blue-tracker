"""The Discord client: live capture, edit/delete tracking, and the startup crawl."""
import asyncio
import logging

import discord

from .crawler import Crawler
from .store import Store
from .tracker import Tracker

log = logging.getLogger(__name__)


def make_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # privileged: enable it for the bot in the Developer Portal
    return intents


class ArchiverClient(discord.Client):
    def __init__(self, settings, *, crawl=True, backfill_days=None):
        super().__init__(intents=make_intents())
        self.settings = settings
        self.store = Store(settings.database_url)
        self.tracker = Tracker(settings, self.store)
        self.crawl_on_ready = crawl
        self.backfill_days = backfill_days
        self._crawl_task: asyncio.Task | None = None

    async def setup_hook(self):
        await self.store.connect()
        await self.tracker.load()
        log.info("Database ready; %d tracked users", len(self.tracker.tracked))

    async def close(self):
        if self._crawl_task:
            self._crawl_task.cancel()
        await super().close()
        await self.store.close()

    @property
    def guild(self) -> discord.Guild | None:
        return self.get_guild(self.settings.guild_id)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        for g in self.guilds:
            if g.id != self.settings.guild_id:
                log.warning("Leaving non-configured guild %s (%s)", g.name, g.id)
                await g.leave()

        guild = self.guild
        if guild is None:
            log.error("Bot is not in guild %s; invite it with the OAuth2 URL", self.settings.guild_id)
            return
        log.info("Archiving %s", guild.name)
        self._log_roles(guild)

        # on_ready fires again after reconnects; crawl only once per process
        if self.crawl_on_ready and self._crawl_task is None:
            crawler = Crawler(guild, self.tracker, self.store, self.settings, self.backfill_days)
            self._crawl_task = asyncio.create_task(crawler.run())
            self._crawl_task.add_done_callback(_log_task_error)

    def _log_roles(self, guild):
        tracked = self.settings.role_ids
        log.info("Roles (* = tracked):")
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True):
            log.info("  %s %s  %s", "*" if r.id in tracked else " ", r.id, r.name)
        missing = tracked - {r.id for r in guild.roles}
        if missing:
            log.warning("Tracked role IDs not found in guild: %s", sorted(missing))

    async def on_guild_join(self, guild):
        if guild.id != self.settings.guild_id:
            log.warning("Leaving non-configured guild %s (%s)", guild.name, guild.id)
            await guild.leave()

    async def on_message(self, msg: discord.Message):
        if await self.tracker.handle(msg):
            log.info("Saved post %s by %s in #%s", msg.id, msg.author.display_name, msg.channel.name)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id != self.settings.guild_id or "content" not in payload.data:
            return  # embed-only updates carry no content
        edited = payload.data.get("edited_timestamp")
        edited_at = discord.utils.parse_time(edited) if edited else None
        if await self.store.record_edit(payload.message_id, payload.data["content"], edited_at):
            log.info("Recorded edit of post %s", payload.message_id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id == self.settings.guild_id and await self.store.record_delete(payload.message_id):
            log.info("Recorded delete of post %s", payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id != self.settings.guild_id:
            return
        for message_id in payload.message_ids:
            await self.store.record_delete(message_id)

    async def on_guild_channel_update(self, before, after):
        if after.guild.id == self.settings.guild_id:
            await self.store.upsert_channel(after)


def _log_task_error(task: asyncio.Task):
    if not task.cancelled() and task.exception():
        log.error("Crawl failed", exc_info=task.exception())
