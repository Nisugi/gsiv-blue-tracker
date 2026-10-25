# CLAUDE.md

Guidance for Claude Code in this repository. See README.md for setup and usage.

## What this is

A role-based Discord archiver: a registered, read-only bot (`archiver/bot`) stores posts by tracked authors (by role or user ID) plus the message each replies to, and a Flask viewer (`archiver/web`) searches them. It replaced the legacy `discord-archiver` self-bot in October 2026, after the self-bot account was flagged.

## Rules

- **Official bot only.** Standard `discord.py`, bot token, `Intents.default()` + `message_content`. Never use `discord.py-self`, user tokens, or client-spoofing patches. Self-bots violate Discord's Terms of Service.
- **Read-only.** The bot needs only View Channels and Read Message History. Don't add features that post, react, or create channels/webhooks.
- **Store only what's tracked.** Tracked-author posts (`reason='author'`) and their reply targets (`reason='reply_target'`). Don't widen collection without the user asking; the admin approval was given for this scope.
- **Server-agnostic code.** Nothing GemStone-specific in Python; it belongs in a `config.*.toml`. Tracked authors are "tracked", labelled in the UI by `settings.label`.
- **One settings source.** `archiver/settings.py`: secrets from `.env`, server rules from TOML.

## Layout

- `archiver/settings.py`: `load()` returns a frozen `Settings`
- `archiver/schema.sql`: the whole schema (idempotent); applied by `python -m archiver.bot --init-db`
- `archiver/bot/`: `tracker.py` (what to archive), `crawler.py` (resumable history crawl), `store.py` (asyncpg), `client.py` (events)
- `archiver/web/`: Flask viewer (psycopg 3)
- `scripts/`: `migrate_legacy.py`, `backup.sh`
- `tests/`: `test_bot.py` (fakes); `test_store.py` and `test_migrate.py` need `TEST_DATABASE_URL` / `TEST_LEGACY_DATABASE_URL` (wiped)

## Conventions

- Discord IDs are BIGINT in Postgres and strings in JSON (JavaScript can't hold snowflakes exactly).
- Times are `timestamptz`.
- `members.is_tracked` never goes back to false.
- `post_revisions.content` is the content *before* the change.
- Schema changes: edit `schema.sql` so it stays idempotent (`IF NOT EXISTS`). Existing databases need a migration step written for them.
