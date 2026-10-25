-- Role-based Discord archive. One database per archived server.
-- Discord IDs are BIGINT snowflakes; the API serializes them as strings.

CREATE TABLE IF NOT EXISTS channels (
    chan_id     BIGINT PRIMARY KEY,
    parent_id   BIGINT,                     -- category for channels, parent channel for threads
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,              -- discord.ChannelType name: text, forum, public_thread, ...
    topic       TEXT,
    position    INTEGER,
    accessible  BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS members (
    member_id     BIGINT PRIMARY KEY,
    username      TEXT,
    display_name  TEXT,
    avatar_url    TEXT,
    is_tracked    BOOLEAN NOT NULL DEFAULT FALSE,   -- once set, stays set (people lose roles)
    tracked_name  TEXT,                             -- display name override from config
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS posts (
    post_id      BIGINT PRIMARY KEY,
    chan_id      BIGINT NOT NULL REFERENCES channels(chan_id),
    author_id    BIGINT NOT NULL REFERENCES members(member_id),
    content      TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL,
    edited_at    TIMESTAMPTZ,
    reply_to_id  BIGINT,
    reason       TEXT NOT NULL CHECK (reason IN ('author', 'reply_target')),
    pinned       BOOLEAN NOT NULL DEFAULT FALSE,
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
);

CREATE TABLE IF NOT EXISTS post_revisions (
    rev_id       BIGSERIAL PRIMARY KEY,
    post_id      BIGINT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    content      TEXT,                      -- content before the edit
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS attachments (
    attach_id     BIGINT PRIMARY KEY,
    post_id       BIGINT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    filename      TEXT,
    url           TEXT,
    content_type  TEXT,
    size          INTEGER,
    width         INTEGER,
    height        INTEGER
);

CREATE TABLE IF NOT EXISTS embeds (
    embed_id  BIGSERIAL PRIMARY KEY,
    post_id   BIGINT NOT NULL REFERENCES posts(post_id) ON DELETE CASCADE,
    data      JSONB NOT NULL
);

-- Newest message the crawler has seen per channel/thread; crawls resume after it
CREATE TABLE IF NOT EXISTS crawl_state (
    chan_id       BIGINT PRIMARY KEY,
    last_seen_id  BIGINT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_posts_created     ON posts (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_chan        ON posts (chan_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author      ON posts (author_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_reply       ON posts (reply_to_id) WHERE reply_to_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_posts_tsv         ON posts USING gin (tsv);
CREATE INDEX IF NOT EXISTS idx_revisions_post    ON post_revisions (post_id);
CREATE INDEX IF NOT EXISTS idx_channels_parent   ON channels (parent_id);

-- Posts shown in the viewer by default: by tracked authors (reply targets are context only).
-- Deleted messages are removed outright, together with their revisions and attachments.
CREATE OR REPLACE VIEW visible_posts AS
SELECT p.*
FROM posts p
WHERE p.reason = 'author';
