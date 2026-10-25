"""Settings: secrets from the environment (.env), server rules from a TOML file.

ARCHIVER_CONFIG names the TOML file (default: config.toml in the working directory).
"""
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    token: str | None
    database_url: str
    guild_id: int
    server_name: str
    label: str
    role_ids: frozenset[int]
    users: dict[int, str]                 # tracked user id -> display name
    save_reply_targets: bool
    hidden_channels: frozenset[int]       # archived, but not shown in the viewer
    exclude_channels: frozenset[int]      # never archived
    skip_crawl: frozenset[int]
    featured_channels: dict[int, str] = field(default_factory=dict)
    backfill_days: float = 7
    request_pause: float = 1.5
    crawl_on_startup: bool = True


def _ids(values) -> frozenset[int]:
    return frozenset(int(v) for v in values or ())


def load(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.getenv("ARCHIVER_CONFIG", "config.toml"))
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")

    server = cfg["server"]
    tracking = cfg.get("tracking", {})
    channels = cfg.get("channels", {})
    crawler = cfg.get("crawler", {})
    return Settings(
        token=os.getenv("DISCORD_TOKEN"),
        database_url=database_url,
        guild_id=int(server["guild_id"]),
        server_name=server.get("name", ""),
        label=server.get("label", "Staff"),
        role_ids=_ids(tracking.get("role_ids")),
        users={int(k): v for k, v in tracking.get("users", {}).items()},
        save_reply_targets=tracking.get("save_reply_targets", True),
        hidden_channels=_ids(channels.get("hidden")),
        exclude_channels=_ids(channels.get("exclude")),
        skip_crawl=_ids(channels.get("skip_crawl")),
        featured_channels={int(k): v for k, v in channels.get("featured", {}).items()},
        backfill_days=float(crawler.get("backfill_days", 7)),
        request_pause=float(crawler.get("request_pause", 1.5)),
        crawl_on_startup=os.getenv("CRAWL_ON_STARTUP", "1") != "0",
    )
