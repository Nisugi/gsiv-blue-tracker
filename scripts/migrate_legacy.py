"""Copy tracked-author posts (and the posts they reply to) from the legacy discord-archiver
database into a new archive database.

    LEGACY_DATABASE_URL=postgresql://...old  DATABASE_URL=postgresql://...new \
        python scripts/migrate_legacy.py [--dry-run]

The new database must already have the schema (python -m archiver.bot --init-db).
Safe to re-run: every insert is ON CONFLICT DO NOTHING / UPDATE.

Conversions from the legacy schema:
- TEXT ids -> BIGINT, epoch-millisecond timestamps -> timestamptz
- members.is_gm + gm_names -> members.is_tracked + tracked_name
- legacy edit revisions stored the content AFTER each edit; the new schema stores the
  content BEFORE it, so edit N becomes a revision holding version N-1 (the pre-first-edit
  original was never captured and is lost)
- posts deleted on Discord (legacy deleted flag or '[[DELETED]]' revision) are not migrated
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv

TRUE = "IN ('1','t','true')"
NUMERIC = "~ '^[0-9]+$'"
NOT_DELETED = f"""NOT COALESCE(p.deleted::text, '0') {TRUE}
    AND NOT EXISTS (SELECT 1 FROM post_revisions r WHERE r.post_id = p.post_id AND r.content = '[[DELETED]]')"""
BATCH = 1000


def ts(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def channel_type(raw):
    # The legacy bot wrote "text", the legacy viewer copy wrote "ChannelType.text"
    return (raw or "unknown").removeprefix("ChannelType.")


async def batched(conn, sql, rows):
    for i in range(0, len(rows), BATCH):
        await conn.executemany(sql, rows[i:i + BATCH])


async def migrate(old: asyncpg.Connection, new: asyncpg.Connection, dry_run: bool):
    # Tracked-author posts, plus the posts they reply to
    post_rows = await old.fetch(f"""
        WITH gm_posts AS (
            SELECT p.* FROM posts p JOIN members m ON m.member_id = p.author_id
            WHERE COALESCE(m.is_gm::text, '0') {TRUE} AND p.post_id {NUMERIC} AND {NOT_DELETED}
        ), targets AS (
            SELECT p.* FROM posts p
            WHERE p.post_id IN (SELECT reply_to_id FROM gm_posts WHERE reply_to_id {NUMERIC})
              AND p.post_id NOT IN (SELECT post_id FROM gm_posts) AND {NOT_DELETED}
        )
        SELECT *, 'author' AS reason FROM gm_posts
        UNION ALL
        SELECT *, 'reply_target' AS reason FROM targets
    """)
    post_ids = [r["post_id"] for r in post_rows]
    print(f"Posts: {sum(r['reason'] == 'author' for r in post_rows)} tracked, "
          f"{sum(r['reason'] == 'reply_target' for r in post_rows)} reply targets")

    chan_ids = {r["chan_id"] for r in post_rows}
    channel_rows = await old.fetch("SELECT * FROM channels WHERE chan_id = ANY($1::text[])", list(chan_ids))
    # Parent channels and categories too, so the hierarchy survives
    parents = {r["parent_id"] for r in channel_rows if r["parent_id"]} - chan_ids
    if parents:
        channel_rows += await old.fetch("SELECT * FROM channels WHERE chan_id = ANY($1::text[])", list(parents))

    author_ids = list({r["author_id"] for r in post_rows})
    member_rows = await old.fetch(f"""
        SELECT m.*, g.gm_name FROM members m LEFT JOIN gm_names g ON g.author_id = m.member_id
        WHERE m.member_id = ANY($1::text[]) OR COALESCE(m.is_gm::text, '0') {TRUE}
    """, author_ids)
    attachment_rows = await old.fetch("SELECT * FROM attachments WHERE post_id = ANY($1::text[])", post_ids)
    embed_rows = await old.fetch("SELECT post_id, data_json FROM embeds WHERE post_id = ANY($1::text[])", post_ids)
    revision_rows = await old.fetch(
        "SELECT post_id, content, captured_ts FROM post_revisions WHERE post_id = ANY($1::text[]) "
        "ORDER BY post_id, captured_ts, rev_id", post_ids)
    print(f"Channels: {len(channel_rows)}, members: {len(member_rows)}, attachments: {len(attachment_rows)}, "
          f"embeds: {len(embed_rows)}, legacy revisions: {len(revision_rows)}")

    # Legacy revisions hold the content after each edit; store the version before it instead.
    # The legacy bot also logged an "edit" whenever Discord updated a message (e.g. a link
    # preview loading) with unchanged text, so consecutive identical versions are collapsed.
    current = {p["post_id"]: (p["content"] or "", p["edited_ts"]) for p in post_rows}
    versions: dict[str, list] = {}
    for r in revision_rows:
        versions.setdefault(r["post_id"], []).append((r["content"] or "", r["captured_ts"]))
    edits = []
    for pid, vs in versions.items():
        chain = []
        for content, captured in vs + [current[pid]]:
            if not chain or content != chain[-1][0]:
                chain.append((content, captured))
        for (prev, prev_at), (_, replaced_at) in zip(chain, chain[1:]):
            when = ts(replaced_at) or ts(prev_at) or datetime.now(timezone.utc)
            edits.append((int(pid), prev, when))

    if dry_run:
        print("Dry run: nothing written.")
        return

    async with new.transaction():
        await batched(new, """
            INSERT INTO channels (chan_id, parent_id, name, type, topic, accessible)
            VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (chan_id) DO NOTHING
        """, [(int(c["chan_id"]), int(c["parent_id"]) if c["parent_id"] else None, c["name"] or "",
               channel_type(c["type"]), c["topic"], bool(c["accessible"]) if c["accessible"] is not None else True)
              for c in channel_rows if c["chan_id"].isdigit()])
        # Posts whose channel row is missing in the legacy DB still need a channel
        await batched(new, """
            INSERT INTO channels (chan_id, name, type) VALUES ($1, '', 'unknown') ON CONFLICT DO NOTHING
        """, [(int(c),) for c in chan_ids])

        await batched(new, """
            INSERT INTO members (member_id, username, display_name, avatar_url, is_tracked, tracked_name)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (member_id) DO UPDATE SET
                is_tracked = members.is_tracked OR EXCLUDED.is_tracked,
                tracked_name = COALESCE(members.tracked_name, EXCLUDED.tracked_name)
        """, [(int(m["member_id"]), m["username"], m["display_name"], m["avatar_url"],
               str(m["is_gm"]).lower() in ("1", "t", "true"), m["gm_name"])
              for m in member_rows if m["member_id"].isdigit()])

        await batched(new, """
            INSERT INTO posts (post_id, chan_id, author_id, content, created_at, edited_at,
                               reply_to_id, reason, pinned)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (post_id) DO NOTHING
        """, [(int(p["post_id"]), int(p["chan_id"]), int(p["author_id"]), p["content"] or "",
               ts(p["created_ts"]), ts(p["edited_ts"]),
               int(p["reply_to_id"]) if p["reply_to_id"] and p["reply_to_id"].isdigit() else None,
               p["reason"], bool(p["pinned"]))
              for p in post_rows])

        await batched(new, """
            INSERT INTO attachments (attach_id, post_id, filename, url, content_type, size, width, height)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (attach_id) DO NOTHING
        """, [(int(a["attach_id"]), int(a["post_id"]), a["filename"], a["url"], a["content_type"],
               a["size"], a["width"], a["height"]) for a in attachment_rows if a["attach_id"].isdigit()])

        # Embeds and revisions have no natural key; clear any from a previous run first
        migrated = [int(p) for p in post_ids]
        await new.execute("DELETE FROM embeds WHERE post_id = ANY($1::bigint[])", migrated)
        await new.execute("DELETE FROM post_revisions WHERE post_id = ANY($1::bigint[])", migrated)
        await batched(new, "INSERT INTO embeds (post_id, data) VALUES ($1, $2::jsonb)",
                      [(int(e["post_id"]), e["data_json"]) for e in embed_rows if e["data_json"]])
        await batched(new, "INSERT INTO post_revisions (post_id, content, captured_at) VALUES ($1, $2, $3)",
                      edits)
    print("Migration complete.")


async def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args()

    old_url, new_url = os.getenv("LEGACY_DATABASE_URL"), os.getenv("DATABASE_URL")
    if not old_url or not new_url:
        sys.exit("Set LEGACY_DATABASE_URL (old archive) and DATABASE_URL (new archive)")
    if old_url == new_url:
        sys.exit("LEGACY_DATABASE_URL and DATABASE_URL must be different databases")

    old, new = await asyncpg.connect(old_url), await asyncpg.connect(new_url)
    try:
        await migrate(old, new, args.dry_run)
    finally:
        await old.close()
        await new.close()


if __name__ == "__main__":
    asyncio.run(main())
