CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    summary TEXT,
    body TEXT NOT NULL,
    image_path TEXT,
    language TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('zh', 'en')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    youtube_id TEXT NOT NULL UNIQUE,
    description TEXT
);

CREATE TABLE IF NOT EXISTS homepage_content (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    hero_title TEXT NOT NULL,
    hero_subtitle TEXT NOT NULL,
    hero_image_path TEXT,
    about_title TEXT NOT NULL,
    about_body TEXT NOT NULL,
    hero_title_zh TEXT,
    hero_subtitle_zh TEXT,
    about_title_zh TEXT,
    about_body_zh TEXT,
    x_url TEXT,
    youtube_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_created_at
ON articles(created_at DESC);
