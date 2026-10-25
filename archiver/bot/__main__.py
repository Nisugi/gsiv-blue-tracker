"""Run the archiver bot: python -m archiver.bot [--no-crawl] [--backfill-days N] [--init-db]"""
import argparse
import asyncio
import logging

from .. import settings as settings_mod
from .client import ArchiverClient
from .store import Store


def main():
    parser = argparse.ArgumentParser(prog="python -m archiver.bot")
    parser.add_argument("--config", help="server TOML file (default: $ARCHIVER_CONFIG or config.toml)")
    parser.add_argument("--no-crawl", action="store_true", help="skip the startup history crawl")
    parser.add_argument("--backfill-days", type=float, help="how far back to crawl channels never crawled before")
    parser.add_argument("--init-db", action="store_true", help="create the schema and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = settings_mod.load(args.config)

    if args.init_db:
        asyncio.run(_init_db(settings))
        return
    if not settings.token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")

    client = ArchiverClient(
        settings,
        crawl=settings.crawl_on_startup and not args.no_crawl,
        backfill_days=args.backfill_days,
    )
    client.run(settings.token, log_handler=None)


async def _init_db(settings):
    store = Store(settings.database_url)
    await store.connect()
    try:
        await store.init_schema()
        print("Schema created.")
    finally:
        await store.close()


if __name__ == "__main__":
    main()
