"""asyncpg persistence for the bot."""
import json
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import discord

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


class Store:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def init_schema(self):
        await self.pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    # ── Channels and members ─────────────────────────────────────────

    async def upsert_channel(self, channel, *, accessible=True, conn=None):
        if isinstance(channel, discord.Thread):
            parent_id = channel.parent_id
        else:
            parent_id = getattr(channel, "category_id", None)
        await (conn or self.pool).execute(
            """
            INSERT INTO channels (chan_id, parent_id, name, type, topic, position, accessible)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (chan_id) DO UPDATE SET
                parent_id = EXCLUDED.parent_id, name = EXCLUDED.name, type = EXCLUDED.type,
                topic = EXCLUDED.topic, position = EXCLUDED.position,
                accessible = EXCLUDED.accessible, updated_at = now()
            """,
            channel.id, parent_id, channel.name, channel.type.name,
            getattr(channel, "topic", None), getattr(channel, "position", None), accessible,
        )

    async def upsert_member(self, user, *, tracked=False, conn=None):
        avatar = getattr(user, "display_avatar", None)
        await (conn or self.pool).execute(
            """
            INSERT INTO members (member_id, username, display_name, avatar_url, is_tracked)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (member_id) DO UPDATE SET
                username = EXCLUDED.username, display_name = EXCLUDED.display_name,
                avatar_url = EXCLUDED.avatar_url,
                is_tracked = members.is_tracked OR EXCLUDED.is_tracked,
                updated_at = now()
            """,
            user.id, user.name, getattr(user, "display_name", None),
            str(avatar.url) if avatar else None, tracked,
        )

    async def seed_tracked_users(self, users: dict[int, str]):
        """Mark configured users as tracked with their display-name overrides."""
        await self.pool.executemany(
            """
            INSERT INTO members (member_id, is_tracked, tracked_name) VALUES ($1, TRUE, $2)
            ON CONFLICT (member_id) DO UPDATE SET is_tracked = TRUE, tracked_name = EXCLUDED.tracked_name
            """,
            list(users.items()),
        )

    async def tracked_ids(self) -> set[int]:
        rows = await self.pool.fetch("SELECT member_id FROM members WHERE is_tracked")
        return {r["member_id"] for r in rows}

    # ── Posts ────────────────────────────────────────────────────────

    async def post_exists(self, post_id: int) -> bool:
        return await self.pool.fetchval("SELECT 1 FROM posts WHERE post_id = $1", post_id) is not None

    async def save_post(self, msg: discord.Message, reason: str, *, author_tracked: bool, notify: bool = False):
        """Insert or refresh a message. An 'author' post is never downgraded to 'reply_target'."""
        reply_to = msg.reference.message_id if msg.reference else None
        async with self.pool.acquire() as conn, conn.transaction():
            await self.upsert_channel(msg.channel, conn=conn)
            await self.upsert_member(msg.author, tracked=author_tracked, conn=conn)
            await conn.execute(
                """
                INSERT INTO posts (post_id, chan_id, author_id, content, created_at, edited_at,
                                   reply_to_id, reason, pinned)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (post_id) DO UPDATE SET
                    content = EXCLUDED.content, edited_at = EXCLUDED.edited_at, pinned = EXCLUDED.pinned,
                    reason = CASE WHEN posts.reason = 'author' THEN 'author' ELSE EXCLUDED.reason END
                """,
                msg.id, msg.channel.id, msg.author.id, msg.content or "", msg.created_at,
                msg.edited_at, reply_to, reason, msg.pinned,
            )
            for a in msg.attachments:
                await conn.execute(
                    """
                    INSERT INTO attachments (attach_id, post_id, filename, url, content_type, size, width, height)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (attach_id) DO UPDATE SET url = EXCLUDED.url
                    """,
                    a.id, msg.id, a.filename, a.url, a.content_type, a.size, a.width, a.height,
                )
            await conn.execute("DELETE FROM embeds WHERE post_id = $1", msg.id)
            for e in msg.embeds:
                await conn.execute(
                    "INSERT INTO embeds (post_id, data) VALUES ($1, $2::jsonb)",
                    msg.id, json.dumps(e.to_dict()),
                )
            if notify:
                await conn.execute("SELECT pg_notify('new_post', $1)", str(msg.id))

    async def record_edit(self, post_id: int, content: str, edited_at: datetime | None) -> bool:
        """Apply an edit to a stored post, keeping the old content as a revision."""
        async with self.pool.acquire() as conn, conn.transaction():
            old = await conn.fetchval("SELECT content FROM posts WHERE post_id = $1 FOR UPDATE", post_id)
            if old is None or old == content:
                return False
            await conn.execute(
                "INSERT INTO post_revisions (post_id, content) VALUES ($1, $2)", post_id, old
            )
            await conn.execute(
                "UPDATE posts SET content = $2, edited_at = $3 WHERE post_id = $1",
                post_id, content, edited_at or datetime.now(timezone.utc),
            )
            return True

    async def record_delete(self, post_id: int) -> bool:
        """Remove a deleted message; revisions, attachments and embeds cascade."""
        return await self.pool.fetchval("DELETE FROM posts WHERE post_id = $1 RETURNING 1", post_id) is not None

    # ── Crawl state ──────────────────────────────────────────────────

    async def last_seen(self, chan_id: int) -> int | None:
        return await self.pool.fetchval("SELECT last_seen_id FROM crawl_state WHERE chan_id = $1", chan_id)

    async def set_last_seen(self, chan_id: int, message_id: int):
        await self.pool.execute(
            """
            INSERT INTO crawl_state (chan_id, last_seen_id) VALUES ($1, $2)
            ON CONFLICT (chan_id) DO UPDATE SET
                last_seen_id = GREATEST(crawl_state.last_seen_id, EXCLUDED.last_seen_id), updated_at = now()
            """,
            chan_id, message_id,
        )

    async def oldest_crawl_time(self) -> datetime | None:
        return await self.pool.fetchval("SELECT min(updated_at) FROM crawl_state")
