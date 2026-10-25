# Discord Archiver

A read-only Discord bot that archives posts by chosen authors, picked by role or user ID, into PostgreSQL. It also saves the message each of those posts replies to, for context. A web viewer searches and browses the archive.

It works on any server. Everything server-specific lives in one TOML file, such as `config.gemstone.toml` for the GemStone IV GM archive.

- **What's stored:** posts by tracked authors, the message each one replies to (one level), and edits of stored posts. A message deleted on Discord is deleted from the archive too. Nothing else.
- **What the bot sees:** only the channels the server's admins grant it. `[channels] exclude` blacklists more on top of that.
- **Permissions:** View Channels and Read Message History. It never posts.

[Terms of Service](docs/TERMS.md) · [Privacy Policy](docs/PRIVACY.md)

## Setup

### 1. Create the Discord bot

1. Go to the [Developer Portal](https://discord.com/developers/applications), create an application, and open **Bot**.
2. Reset and copy the **token**.
3. Turn on **Message Content Intent**. Leave the Server Members and Presence intents off.
4. Under **Installation**, choose **Guild Install** only, set the Install Link to **Discord Provided Link**, and give it scope `bot` with **View Channels** and **Read Message History**. Leave **Public Bot** on; verification requires it. The bot leaves any server that isn't in its config.
5. Under **OAuth2 → URL Generator**, tick scope `bot` with the permissions **View Channels** and **Read Message History**. A server admin opens that URL to invite the bot.

### 2. Install

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # fill in DISCORD_TOKEN, DATABASE_URL, ARCHIVER_CONFIG
cp config.example.toml config.toml   # or use an existing config such as config.gemstone.toml
createdb discord_archive
python -m archiver.bot --init-db
```

### 3. Choose who to track

Start the bot once. It logs every role in the server with its ID, and marks the tracked ones with `*`. Put the role IDs into `[tracking] role_ids`. List specific people under `[tracking.users]`, including anyone who has left the server: people who leave lose their roles.

Anyone who has once been tracked stays tracked, so their posts keep being archived after a role change.

## Running

```bash
python -m archiver.bot                    # live capture + catch-up crawl
python -m archiver.bot --no-crawl         # live capture only
python -m archiver.bot --backfill-days 30 # first crawl reaches 30 days back
flask --app archiver.web.app run --port 8080   # development only
```

### Viewer in production

Use a threaded server. Every browser with live updates on keeps a server thread open for as long as the tab is open, so a server with one thread per worker (gunicorn's default) stops answering searches after a few visitors:

```bash
gunicorn -k gthread --workers 2 --threads 64 --bind 127.0.0.1:8080 "archiver.web.app:create_app()"
```

Each worker accepts up to `MAX_STREAM_CLIENTS` live streams (env, default 40) and leaves the remaining threads for searches. Beyond that, extra browsers simply don't get live updates. Keep `MAX_STREAM_CLIENTS` below `--threads`. If you run behind nginx, turn off buffering for `/stream` (the viewer already sends `X-Accel-Buffering: no`).

The crawler resumes each channel and thread after the newest message it has already read, so restarts and outages leave no gaps. `backfill_days` only limits how far back a channel goes the first time it's crawled.

When the bot saves a visible post it sends a Postgres `NOTIFY new_post`, and the viewer streams it to open browsers.

## Configuration

| Where | What |
| --- | --- |
| `.env` | `DISCORD_TOKEN`, `DATABASE_URL`, `ARCHIVER_CONFIG` (TOML path), `CRAWL_ON_STARTUP` |
| `[server]` | `guild_id`, `name`, `label` (what tracked authors are called in the viewer, such as "GM") |
| `[tracking]` | `role_ids`, `save_reply_targets`, `[tracking.users]` (id = display name) |
| `[channels]` | `exclude` (never archived), `hidden` (archived, not shown), `skip_crawl` (forums), `[channels.featured]` |
| `[crawler]` | `backfill_days`, `request_pause` |

## Migrating from the legacy archiver

```bash
LEGACY_DATABASE_URL=postgresql://.../discord_archiver python scripts/migrate_legacy.py --dry-run
LEGACY_DATABASE_URL=postgresql://.../discord_archiver python scripts/migrate_legacy.py
```

This copies GM posts, their reply targets, channels, members, attachments, embeds and edit/delete history. Community messages are not copied. It's safe to run again. See the script's docstring for the details of the conversion.

## Backups

```bash
bash scripts/backup.sh [backup_dir] [keep]   # default: backups/, keep 3
```

## Tests

```bash
pytest                                        # unit tests
TEST_DATABASE_URL=postgresql://.../scratch \
TEST_LEGACY_DATABASE_URL=postgresql://.../scratch_legacy pytest   # + database tests (wipes both)
```
