"""Decides which messages are archived: tracked authors (by role or user ID) plus their reply targets."""
import logging

import discord

from ..settings import Settings

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self, settings: Settings, store):
        self.settings = settings
        self.store = store
        self.tracked: set[int] = set(settings.users)

    async def load(self):
        await self.store.seed_tracked_users(self.settings.users)
        self.tracked |= await self.store.tracked_ids()

    def is_excluded(self, channel) -> bool:
        """Blacklisted channels, and threads under a blacklisted channel."""
        excluded = self.settings.exclude_channels
        return channel.id in excluded or getattr(channel, "parent_id", None) in excluded

    def is_tracked(self, author) -> bool:
        """Tracked by user ID or once flagged; a tracked role flags the author (see handle)."""
        if author is None or author.bot:
            return False
        return author.id in self.tracked or self._has_tracked_role(author)

    def _has_tracked_role(self, author) -> bool:
        # Only guild Members carry roles; authors who left the server come back as plain Users
        return any(r.id in self.settings.role_ids for r in getattr(author, "roles", ()))

    async def handle(self, msg: discord.Message) -> bool:
        """Archive msg if it qualifies. Returns True when it was saved."""
        if msg.guild is None or msg.guild.id != self.settings.guild_id or self.is_excluded(msg.channel):
            return False
        if not self.is_tracked(msg.author):
            return False

        if msg.author.id not in self.tracked:
            log.info("Now tracking %s (%s) by role", msg.author.display_name, msg.author.id)
            self.tracked.add(msg.author.id)

        hidden = msg.channel.id in self.settings.hidden_channels or \
            getattr(msg.channel, "parent_id", None) in self.settings.hidden_channels
        await self.store.save_post(msg, "author", author_tracked=True, notify=not hidden)

        if self.settings.save_reply_targets:
            await self._save_reply_target(msg)
        return True

    async def _save_reply_target(self, msg: discord.Message):
        ref = msg.reference
        if not ref or not ref.message_id or ref.type != discord.MessageReferenceType.reply:
            return
        if ref.channel_id != msg.channel.id or await self.store.post_exists(ref.message_id):
            return

        target = ref.resolved
        if isinstance(target, discord.DeletedReferencedMessage):
            return
        if target is None:
            try:
                target = await msg.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return
        if isinstance(target, discord.Message):
            tracked = self.is_tracked(target.author)
            await self.store.save_post(target, "author" if tracked else "reply_target", author_tracked=tracked)
