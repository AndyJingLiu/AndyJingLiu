import hashlib
import hmac
import json
import math
import mimetypes
import os
import random
import re
import secrets
import sqlite3
import string
import threading
import time
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import bleach
import click
import markdown2
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
mimetypes.add_type("font/woff2", ".woff2")
load_dotenv(BASE_DIR / ".env")
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


app = Flask(__name__, instance_relative_config=True)
Path(app.instance_path).mkdir(parents=True, exist_ok=True)

secret_key = os.getenv("SECRET_KEY")
if APP_ENV == "production" and not secret_key:
    raise RuntimeError("SECRET_KEY must be set when APP_ENV=production.")

legacy_database = BASE_DIR / "database.db"
default_database = (
    legacy_database
    if legacy_database.exists()
    else Path(app.instance_path) / "database.db"
)

app.config.update(
    APP_ENV=APP_ENV,
    DATABASE=os.getenv("DATABASE_PATH", str(default_database)),
    SECRET_KEY=secret_key or secrets.token_hex(32),
    SITE_URL=os.getenv("SITE_URL", "").rstrip("/"),
    ADMIN_USERNAME=os.getenv("ADMIN_USERNAME", ""),
    ADMIN_PASSWORD_HASH=os.getenv("ADMIN_PASSWORD_HASH", ""),
    YOUTUBE_CHANNEL_ID=os.getenv(
        "YOUTUBE_CHANNEL_ID", "UCL6USkBdRjEeOpeLo2Lq9aA"
    ).strip(),
    YOUTUBE_CHANNEL_URL=os.getenv(
        "YOUTUBE_CHANNEL_URL", "https://www.youtube.com/@AndyJingLiu"
    ).rstrip("/"),
    YOUTUBE_FEED_CACHE_SECONDS=900,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=APP_ENV == "production",
    SEND_FILE_MAX_AGE_DEFAULT=31536000 if APP_ENV == "production" else 0,
)

trusted_hosts = [
    host.strip() for host in os.getenv("TRUSTED_HOSTS", "").split(",") if host.strip()
]
if trusted_hosts:
    app.config["TRUSTED_HOSTS"] = trusted_hosts

if env_bool("TRUST_PROXY", APP_ENV == "production"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def warn_about_missing_config() -> None:
    """Say out loud when the app is running on built-in defaults.

    Without this the app starts up looking healthy while the admin login is
    unusable and sessions are dropped on every restart, which is impossible to
    diagnose from the outside.
    """
    if not (BASE_DIR / ".env").exists():
        app.logger.warning(
            "No .env file found — running on the defaults baked into app.py. "
            "Copy .env.example to .env and fill it in."
        )
    if not app.config["ADMIN_USERNAME"] or not app.config["ADMIN_PASSWORD_HASH"]:
        app.logger.warning(
            "ADMIN_USERNAME / ADMIN_PASSWORD_HASH are unset — admin login is "
            "disabled. Generate a hash with: flask hash-password"
        )
    if not secret_key:
        app.logger.warning(
            "SECRET_KEY is unset — a random one is generated per start, so every "
            "restart logs you out."
        )


warn_about_missing_config()


ALLOWED_MARKDOWN_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
ALLOWED_IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
SUPPORTED_LOCALES = {"zh", "en"}
login_attempts: dict[str, list[float]] = {}
youtube_feed_cache: dict[str, object] = {
    "channel_id": "",
    "expires_at": 0.0,
    "videos": [],
}
youtube_feed_lock = threading.Lock()

YOUTUBE_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

LEGACY_DEMO_VIDEO_IDS = ("fkIvmfqX-t0", "KZpYtNtGxSU")

UI_COPY = {
    "zh": {
        "nav_articles": "文章",
        "nav_videos": "视频",
        "nav_about": "关于我",
        "language_label": "EN",
        "hero_kicker": "个人主页 · 文章 · 视频",
        "x_cta": "关注我的 X",
        "youtube_cta": "访问 YouTube 频道",
        "about_kicker": "关于 Andy",
        "about_link": "进一步了解我",
        "latest_articles": "最新文章",
        "all_articles": "查看全部文章",
        "latest_videos": "最新视频",
        "all_videos": "查看全部视频",
        "articles_title": "文章",
        "articles_intro": "这里收录我的长期写作、观察与思考。",
        "videos_title": "视频",
        "videos_intro": "这里会自动显示我在 YouTube 发布的最新视频。",
        "about_title": "关于我",
        "empty_articles": "中文文章正在准备中。",
        "empty_videos": "视频内容正在准备中。",
        "watch_youtube": "在 YouTube 观看",
        "back_articles": "返回文章列表",
        "social_title": "在更多平台找到我",
        "featured": "最新发布",
        "seal_label": "刘的印章",
    },
    "en": {
        "nav_articles": "Articles",
        "nav_videos": "Videos",
        "nav_about": "About",
        "language_label": "中文",
        "hero_kicker": "Personal site · Writing · Video",
        "x_cta": "Follow on X",
        "youtube_cta": "Visit my YouTube channel",
        "about_kicker": "About Andy",
        "about_link": "More about me",
        "latest_articles": "Latest articles",
        "all_articles": "View all articles",
        "latest_videos": "Latest videos",
        "all_videos": "View all videos",
        "articles_title": "Articles",
        "articles_intro": (
            "Long-form writing, observations, and ideas from AndyJingLiu."
        ),
        "videos_title": "Videos",
        "videos_intro": "My latest YouTube uploads appear here automatically.",
        "about_title": "About",
        "empty_articles": "English articles are coming soon.",
        "empty_videos": "Videos are coming soon.",
        "watch_youtube": "Watch on YouTube",
        "back_articles": "Back to articles",
        "social_title": "Find me elsewhere",
        "featured": "Latest release",
        "seal_label": "Seal of Liu",
    },
}


def get_db_connection() -> sqlite3.Connection:
    database_path = Path(app.config["DATABASE"]).expanduser()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_homepage_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(homepage_content)")
    }
    if "x_url" not in columns:
        conn.execute("ALTER TABLE homepage_content ADD COLUMN x_url TEXT")
    if "youtube_url" not in columns:
        conn.execute("ALTER TABLE homepage_content ADD COLUMN youtube_url TEXT")
    conn.execute(
        """
        UPDATE homepage_content
        SET youtube_url = ?
        WHERE youtube_url IS NULL OR TRIM(youtube_url) = ''
        """,
        (app.config["YOUTUBE_CHANNEL_URL"],),
    )
    localized_defaults = {
        "hero_title_zh": "AndyJingLiu",
        "hero_subtitle_zh": "这是我的个人网站，收录文章、思考与视频创作。",
        "about_title_zh": "关于我",
        "about_body_zh": (
            "我在这里分享自己的文章、思考与视频，并把发布在 X 和 YouTube "
            "上的内容集中到同一个独立空间。"
        ),
    }
    for column, default in localized_defaults.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE homepage_content ADD COLUMN {column} TEXT")
        conn.execute(
            f"UPDATE homepage_content SET {column} = ? "
            f"WHERE {column} IS NULL OR TRIM({column}) = ''",
            (default,),
        )

    updated_english_about = (
        "I use this independent site to share my writing, ideas, and video work, "
        "and to connect everything I publish on X and YouTube."
    )
    conn.execute(
        """
        UPDATE homepage_content
        SET hero_title = ?, hero_subtitle = ?, about_body = ?
        WHERE hero_title = 'Andy Jing Liu · Insights & Analysis'
          AND hero_subtitle LIKE '%immigration%'
        """,
        (
            "AndyJingLiu",
            "My independent home for articles, ideas, and video work.",
            updated_english_about,
        ),
    )


def ensure_article_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    if "language" not in columns:
        conn.execute(
            "ALTER TABLE articles ADD COLUMN language TEXT NOT NULL DEFAULT 'en'"
        )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_articles_language_created_at
        ON articles(language, created_at DESC)
        """)
    updated_summary = (
        "Some thoughts on China, Canada, technology, culture, and a changing "
        "global landscape."
    )
    updated_body = (
        "Some notes on technology, culture, and a changing global landscape.\n\n"
        "## Key Points\n\n"
        "1. How technology changes the way we communicate\n"
        "2. Why culture shapes how we understand the world\n"
        "3. The value of long-term thinking\n\n"
        "More details coming soon."
    )
    conn.execute(
        """
        UPDATE articles
        SET summary = ?, body = ?
        WHERE slug = 'china-canada-future'
          AND (summary LIKE '%immigration%' OR body LIKE '%immigration%')
        """,
        (updated_summary, updated_body),
    )
    for article in conn.execute("SELECT id, title, body FROM articles").fetchall():
        normalized_body = normalize_article_markdown(article["body"], article["title"])
        if normalized_body != article["body"]:
            conn.execute(
                "UPDATE articles SET body = ? WHERE id = ?",
                (normalized_body, article["id"]),
            )


def remove_legacy_demo_videos(conn: sqlite3.Connection) -> None:
    """Remove the two unrelated videos shipped with the original starter."""
    placeholders = ", ".join("?" for _ in LEGACY_DEMO_VIDEO_IDS)
    conn.execute(
        f"DELETE FROM videos WHERE youtube_id IN ({placeholders})",
        LEGACY_DEMO_VIDEO_IDS,
    )


def seed_database(conn: sqlite3.Connection) -> None:
    seed_path = BASE_DIR / "seed_data.json"
    if not seed_path.exists():
        return

    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    homepage = seed["homepage_content"]
    conn.execute(
        """
        INSERT OR IGNORE INTO homepage_content
            (id, hero_title, hero_subtitle, hero_image_path,
             about_title, about_body, hero_title_zh, hero_subtitle_zh,
             about_title_zh, about_body_zh, x_url, youtube_url)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            homepage["hero_title"],
            homepage["hero_subtitle"],
            homepage.get("hero_image_path", ""),
            homepage["about_title"],
            homepage["about_body"],
            homepage["hero_title_zh"],
            homepage["hero_subtitle_zh"],
            homepage["about_title_zh"],
            homepage["about_body_zh"],
            homepage.get("x_url", ""),
            homepage.get("youtube_url", ""),
        ),
    )

    conn.executemany(
        """
        INSERT OR IGNORE INTO articles
            (title, slug, summary, body, image_path, language, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                article["title"],
                article["slug"],
                article.get("summary", ""),
                article["body"],
                article.get("image_path", ""),
                article.get("language", "en"),
                article.get("created_at"),
            )
            for article in seed.get("articles", [])
        ],
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO videos (title, youtube_id, description)
        VALUES (?, ?, ?)
        """,
        [
            (video["title"], video["youtube_id"], video.get("description", ""))
            for video in seed.get("videos", [])
        ],
    )


def init_db(seed: bool | None = None) -> None:
    schema_path = BASE_DIR / "schema.sql"
    with closing(get_db_connection()) as conn:
        initialized = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'articles'"
        ).fetchone()
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        ensure_homepage_columns(conn)
        ensure_article_columns(conn)
        remove_legacy_demo_videos(conn)
        if seed is True or (seed is None and initialized is None):
            seed_database(conn)
        conn.commit()


@app.cli.command("init-db")
@click.option("--seed/--no-seed", default=True, help="Load the starter content.")
def init_db_command(seed: bool) -> None:
    """Create or update the database schema."""
    init_db(seed=seed)
    click.echo("Database initialized.")


@app.cli.command("hash-password")
@click.password_option(confirmation_prompt=True)
def hash_password_command(password: str) -> None:
    """Generate a password hash for ADMIN_PASSWORD_HASH."""
    click.echo(generate_password_hash(password))


def auto_summary(text: str, max_chars: int = 200) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return clean

    snippet = clean[:max_chars]
    if clean[max_chars] != " ":
        last_space = snippet.rfind(" ")
        if last_space != -1:
            snippet = snippet[:last_space]
    return snippet.rstrip(string.punctuation + " ") + "..."


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    return text.strip("-") or "article"


def parse_youtube_feed(xml_data: bytes) -> list[dict[str, str]]:
    """Return regular public videos from YouTube's official Atom feed."""
    root = ElementTree.fromstring(xml_data)
    videos = []
    for entry in root.findall("atom:entry", YOUTUBE_NAMESPACES):
        video_id = entry.findtext(
            "yt:videoId", default="", namespaces=YOUTUBE_NAMESPACES
        )
        title = entry.findtext("atom:title", default="", namespaces=YOUTUBE_NAMESPACES)
        published = entry.findtext(
            "atom:published", default="", namespaces=YOUTUBE_NAMESPACES
        )
        link = entry.find("atom:link[@rel='alternate']", YOUTUBE_NAMESPACES)
        alternate_url = link.get("href", "") if link is not None else ""
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
            or not title
            or "/shorts/" in alternate_url
        ):
            continue
        published_at = ""
        if published:
            try:
                published_at = datetime.fromisoformat(
                    published.replace("Z", "+00:00")
                ).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                published_at = ""
        videos.append(
            {
                "title": title,
                "youtube_id": video_id,
                "description": "",
                "published_at": published_at,
                "watch_url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return videos


def youtube_text(value: object) -> str:
    """Read either of YouTube's common text object shapes."""
    if not isinstance(value, dict):
        return ""
    simple_text = value.get("simpleText")
    if isinstance(simple_text, str):
        return simple_text.strip()
    runs = value.get("runs")
    if not isinstance(runs, list):
        return ""
    return "".join(
        run.get("text", "")
        for run in runs
        if isinstance(run, dict) and isinstance(run.get("text"), str)
    ).strip()


def parse_youtube_channel_page(html_data: bytes) -> list[dict[str, str]]:
    """Extract regular videos from the channel's public Videos page."""
    html = html_data.decode("utf-8", errors="replace")
    marker = "var ytInitialData = "
    marker_at = html.find(marker)
    if marker_at == -1:
        return []

    json_at = html.find("{", marker_at + len(marker))
    if json_at == -1:
        return []
    try:
        initial_data, _ = json.JSONDecoder().raw_decode(html[json_at:])
    except json.JSONDecodeError:
        return []

    videos: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    def add_video(video_id: object, title: str) -> None:
        if (
            isinstance(video_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
            and video_id not in seen_ids
            and title
        ):
            seen_ids.add(video_id)
            videos.append(
                {
                    "title": title,
                    "youtube_id": video_id,
                    "description": "",
                    "published_at": "",
                    "watch_url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )

    def visit(value: object) -> None:
        if len(videos) >= 15:
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return

        renderer = value.get("videoRenderer")
        if isinstance(renderer, dict):
            add_video(renderer.get("videoId"), youtube_text(renderer.get("title")))

        lockup = value.get("lockupViewModel")
        if (
            isinstance(lockup, dict)
            and lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO"
        ):
            metadata = lockup.get("metadata")
            metadata_view = (
                metadata.get("lockupMetadataViewModel", {})
                if isinstance(metadata, dict)
                else {}
            )
            title_data = (
                metadata_view.get("title", {})
                if isinstance(metadata_view, dict)
                else {}
            )
            title = (
                title_data.get("content", "") if isinstance(title_data, dict) else ""
            )
            add_video(lockup.get("contentId"), title.strip())

        for child in value.values():
            visit(child)

    visit(initial_data)
    return videos


def fetch_youtube_channel_page(channel_id: str) -> list[dict[str, str]]:
    """Fetch the public Videos page when YouTube's Atom feed is unavailable."""
    page_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    request_headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "User-Agent": "Mozilla/5.0 AndyJingLiu.com/1.0",
    }
    with urlopen(Request(page_url, headers=request_headers), timeout=8) as response:
        return parse_youtube_channel_page(response.read())


def fetch_youtube_videos() -> list[dict[str, str]]:
    """Fetch and briefly cache the channel's latest regular public videos."""
    channel_id = app.config["YOUTUBE_CHANNEL_ID"]
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        return []

    now = time.monotonic()
    if (
        youtube_feed_cache["channel_id"] == channel_id
        and now < youtube_feed_cache["expires_at"]
    ):
        return list(youtube_feed_cache["videos"])

    with youtube_feed_lock:
        now = time.monotonic()
        if (
            youtube_feed_cache["channel_id"] == channel_id
            and now < youtube_feed_cache["expires_at"]
        ):
            return list(youtube_feed_cache["videos"])

        feed_url = "https://www.youtube.com/feeds/videos.xml?" + urlencode(
            {"channel_id": channel_id}
        )
        request_headers = {
            "Accept": "application/atom+xml, application/xml;q=0.9",
            "User-Agent": "AndyJingLiu.com/1.0",
        }
        try:
            with urlopen(
                Request(feed_url, headers=request_headers), timeout=5
            ) as response:
                videos = parse_youtube_feed(response.read())
        except (ElementTree.ParseError, OSError, TimeoutError, URLError) as error:
            app.logger.info(
                "YouTube feed unavailable (%s); using the channel page", error
            )
            videos = []

        if not videos:
            try:
                videos = fetch_youtube_channel_page(channel_id)
            except (OSError, TimeoutError, URLError):
                app.logger.warning(
                    "Unable to refresh YouTube channel page", exc_info=True
                )

        if not videos and youtube_feed_cache["channel_id"] == channel_id:
            return list(youtube_feed_cache["videos"])

        youtube_feed_cache.update(
            {
                "channel_id": channel_id,
                "expires_at": now + int(app.config["YOUTUBE_FEED_CACHE_SECONDS"]),
                "videos": videos,
            }
        )
        return list(videos)


def database_videos(limit: int | None = None) -> list[dict[str, object]]:
    query = "SELECT id, title, youtube_id, description FROM videos ORDER BY id DESC"
    params: tuple[int, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    with closing(get_db_connection()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            **dict(row),
            "published_at": "",
            "watch_url": f"https://www.youtube.com/watch?v={row['youtube_id']}",
        }
        for row in rows
    ]


def latest_videos(limit: int) -> list[dict[str, object]]:
    videos = fetch_youtube_videos()
    if videos:
        return videos[:limit]
    return database_videos(limit)


def generate_unique_slug(title: str, conn: sqlite3.Connection) -> str:
    base_slug = slugify(title)
    slug = base_slug
    counter = 2
    while conn.execute("SELECT 1 FROM articles WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


MARKDOWN_HEADING_PATTERN = re.compile(r"^(\s*)(#{1,6})\s+(.+?)\s*$")


def normalize_article_markdown(text: str, title: str) -> str:
    """Keep the template title as the article's only level-one heading."""
    lines = text.strip().splitlines()
    first_content = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if first_content is not None:
        match = MARKDOWN_HEADING_PATTERN.match(lines[first_content])
        if (
            match
            and len(match.group(2)) == 1
            and " ".join(match.group(3).split()).casefold()
            == " ".join(title.split()).casefold()
        ):
            del lines[first_content]
            if first_content < len(lines) and not lines[first_content].strip():
                del lines[first_content]

    fence = None
    previous_heading_level = 1
    normalized_lines = []
    for line in lines:
        stripped = line.lstrip()
        marker = stripped[:3] if stripped.startswith(("```", "~~~")) else None
        if marker:
            fence = None if fence == marker else marker if fence is None else fence
            normalized_lines.append(line)
            continue
        if fence is None:
            heading = MARKDOWN_HEADING_PATTERN.match(line)
            if heading:
                level = max(2, len(heading.group(2)))
                level = min(level, previous_heading_level + 1)
                line = f"{heading.group(1)}{'#' * level} {heading.group(3)}"
                previous_heading_level = level
        normalized_lines.append(line)
    return "\n".join(normalized_lines).strip()


def render_markdown(text: str) -> str:
    raw_html = markdown2.markdown(
        text,
        extras=["fenced-code-blocks", "tables", "strike", "smarty"],
    )
    return bleach.clean(
        raw_html,
        tags=ALLOWED_MARKDOWN_TAGS,
        attributes={"a": ["href", "title"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )


def validate_image_path(value: str) -> bool:
    if not value:
        return True
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) > 1
        and path.parts[0] == "images"
        and path.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS
    )


def validate_social_url(value: str, allowed_hosts: set[str]) -> bool:
    if not value:
        return True
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    return any(
        hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts
    )


def validate_article_form(form_data: dict[str, str]) -> str | None:
    if not form_data["title"] or not form_data["body"]:
        return "Title and body are required."
    if len(form_data["title"]) > 200:
        return "Title must be 200 characters or fewer."
    if len(form_data["summary"]) > 500:
        return "Summary must be 500 characters or fewer."
    if len(form_data["body"]) > 100_000:
        return "Article body is too long."
    if not validate_image_path(form_data["image_path"]):
        return "Image path must point to an image inside static/images."
    if form_data.get("language") not in SUPPORTED_LOCALES:
        return "Article language must be Chinese or English."
    return None


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def protect_against_csrf() -> None:
    if request.method != "POST":
        return
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    expected = session.get("csrf_token")
    if not submitted or not expected or not hmac.compare_digest(submitted, expected):
        abort(400, description="The form expired or could not be verified.")


def admin_is_authenticated() -> bool:
    return session.get("admin_authenticated") is True


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not admin_is_authenticated():
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view


def safe_next_url(value: str | None) -> str:
    if not value:
        return url_for("admin_dashboard")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/"):
        return url_for("admin_dashboard")
    return value


def login_rate_limited(client_key: str) -> bool:
    now = time.monotonic()
    recent = [
        attempt
        for attempt in login_attempts.get(client_key, [])
        if now - attempt < LOGIN_WINDOW_SECONDS
    ]
    login_attempts[client_key] = recent
    return len(recent) >= LOGIN_MAX_ATTEMPTS


def static_asset_url(filename: str) -> str:
    """Return a cache-busted URL for a file inside the static directory."""
    static_root = Path(app.static_folder).resolve()
    asset_path = (static_root / filename).resolve()
    version = None
    try:
        asset_path.relative_to(static_root)
        version = asset_path.stat().st_mtime_ns
    except (OSError, ValueError):
        pass
    return url_for("static", filename=filename, v=version)


# ---------------------------------------------------------------------------
# Generated article covers
#
# Every article gets a cover whether or not one was uploaded. The artwork is a
# deterministic function of the slug, so a given article always looks the same,
# nothing is stored on disk, and a new post is never published bare. The four
# compositions are line work only — concentric arcs, a ruled field, nested
# rectangles, a fan — in the site's own burgundy with a single gold element,
# echoing the seal. The background is left transparent so the surrounding
# --paper-sunk shows through and the covers follow light and dark mode without
# needing a second version.
# ---------------------------------------------------------------------------

COVER_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COVER_WIDTH = 1000
COVER_HEIGHT = 800
# One cover serves both colour schemes, so these are picked at the value where
# they clear 3:1 against --paper-sunk in *both* directions rather than looking
# ideal in either. Hence a dusty burgundy and a muted gold, not the saturated
# --accent and --seal the tokens use.
COVER_BURGUNDY = "#a9677a"
COVER_GOLD = "#9d7538"
# Stroke weights are in viewBox units. A card renders the cover about 360px
# wide, so the viewBox is scaled down roughly 2.8x and a nominal 2.8 lands at
# about one device pixel — thin, but still a line rather than a rumour. The
# earlier 1.1 disappeared entirely at card size.
COVER_HAIRLINE = 2.8
COVER_ACCENT_LINE = 6.5
# Long enough to cross the frame from any origin the compositions can pick.
COVER_REACH = 2200.0


def _cover_rng(slug: str) -> random.Random:
    digest = hashlib.sha256(slug.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _cover_accent_index(rng: random.Random, count: int) -> int:
    """Pick which element is gold, from the middle half of the set.

    Every composition runs off the frame, so an accent drawn from the ends
    often landed outside the crop and the cover came back all burgundy. The middle
    half is reliably inside the frame.
    """
    low = count // 4
    high = max(low + 1, count - count // 4)
    return rng.randrange(low, high)


def _cover_stroke(index: int, accent_at: int) -> tuple[str, float]:
    if index == accent_at:
        return COVER_GOLD, COVER_ACCENT_LINE
    return COVER_BURGUNDY, COVER_HAIRLINE


def _cover_arcs(rng: random.Random) -> list[str]:
    # The centre is allowed outside the frame, but the ring stack always
    # reaches past the far corner, so no crop can come back empty.
    cx = rng.uniform(-0.25, 0.75) * COVER_WIDTH
    cy = rng.uniform(-0.15, 1.15) * COVER_HEIGHT
    count = rng.randint(13, 20)
    reach = math.hypot(
        max(abs(cx), abs(COVER_WIDTH - cx)), max(abs(cy), abs(COVER_HEIGHT - cy))
    )
    step = reach / count
    accent_at = _cover_accent_index(rng, count)
    shapes = []
    for index in range(count):
        stroke, width = _cover_stroke(index, accent_at)
        radius = step * (index + 1)
        shapes.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{width}"/>'
        )
    return shapes


def _cover_rules(rng: random.Random) -> list[str]:
    angle = rng.uniform(-78, 78)
    count = rng.randint(14, 26)
    accent_at = _cover_accent_index(rng, count)
    span = 1800.0
    gap = span / count
    lines = []
    for index in range(count):
        stroke, width = _cover_stroke(index, accent_at)
        x = -span / 2 + gap * index + rng.uniform(-gap * 0.22, gap * 0.22)
        lines.append(
            f'<line x1="{x:.1f}" y1="{-COVER_REACH / 2:.0f}" '
            f'x2="{x:.1f}" y2="{COVER_REACH / 2:.0f}" '
            f'stroke="{stroke}" stroke-width="{width}"/>'
        )
    group = "".join(lines)
    return [
        f'<g transform="translate({COVER_WIDTH / 2:.0f} {COVER_HEIGHT / 2:.0f}) '
        f'rotate({angle:.1f})">{group}</g>'
    ]


def _cover_nested(rng: random.Random) -> list[str]:
    count = rng.randint(12, 20)
    # A strong drift is what keeps this from reading as a bullseye.
    drift_x = rng.choice([-1, 1]) * rng.uniform(34, 62)
    drift_y = rng.choice([-1, 1]) * rng.uniform(26, 50)
    accent_at = _cover_accent_index(rng, count)
    # The outermost rectangles run off the frame, so the composition is a crop
    # of something larger rather than a diagram centred in a box.
    base_w, base_h = 1500.0, 1200.0
    shapes = []
    for index in range(count):
        stroke, width = _cover_stroke(index, accent_at)
        w = base_w - index * (base_w / (count + 1))
        h = base_h - index * (base_h / (count + 1))
        x = COVER_WIDTH / 2 + drift_x * index - w / 2
        y = COVER_HEIGHT / 2 + drift_y * index - h / 2
        shapes.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{width}"/>'
        )
    return shapes


def _cover_fan(rng: random.Random) -> list[str]:
    ox = rng.uniform(-0.3, 1.3) * COVER_WIDTH
    # The vanishing point sits well outside the frame. Placed close to the edge
    # this composition became a sunburst, which is a loud, decorative shape; at
    # this distance the rays cross the frame almost parallel and only lean.
    oy = rng.choice([-1, 1]) * rng.uniform(0.7, 1.6) * COVER_HEIGHT
    if oy > 0:
        oy += COVER_HEIGHT
    count = rng.randint(11, 20)
    # Aim at the middle of the frame and open symmetrically, so the fan always
    # sweeps across rather than off into the margin.
    aim = math.degrees(math.atan2(COVER_HEIGHT / 2 - oy, COVER_WIDTH / 2 - ox))
    spread = rng.uniform(28, 62)
    start = aim - spread / 2
    accent_at = _cover_accent_index(rng, count)
    shapes = []
    for index in range(count):
        stroke, width = _cover_stroke(index, accent_at)
        theta = math.radians(start + spread * index / max(count - 1, 1))
        x = ox + math.cos(theta) * COVER_REACH
        y = oy + math.sin(theta) * COVER_REACH
        shapes.append(
            f'<line x1="{ox:.1f}" y1="{oy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}"/>'
        )
    return shapes


COVER_COMPOSITIONS = (_cover_arcs, _cover_rules, _cover_nested, _cover_fan)


def render_cover_svg(slug: str) -> str:
    rng = _cover_rng(slug)
    compose = COVER_COMPOSITIONS[rng.randrange(len(COVER_COMPOSITIONS))]
    shapes = "".join(compose(rng))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {COVER_WIDTH} {COVER_HEIGHT}" '
        f'width="{COVER_WIDTH}" height="{COVER_HEIGHT}" '
        f'preserveAspectRatio="xMidYMid slice" role="presentation">'
        f'<g stroke-linecap="square">{shapes}</g>'
        f"</svg>"
    )


@app.route("/covers/<slug>.svg")
def article_cover(slug: str):
    if not COVER_SLUG_PATTERN.match(slug) or len(slug) > 120:
        abort(404)
    response = Response(render_cover_svg(slug), mimetype="image/svg+xml")
    # The artwork only ever changes if the slug changes, and a changed slug is
    # a different URL.
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def cover_url(article) -> str:
    """Return an article's uploaded cover, or its generated one."""
    try:
        image_path = (article["image_path"] or "").strip()
    except (KeyError, IndexError, TypeError):
        image_path = ""
    if image_path:
        return static_asset_url(image_path)
    try:
        slug = (article["slug"] or "").strip()
    except (KeyError, IndexError, TypeError):
        slug = ""
    if not COVER_SLUG_PATTERN.match(slug):
        return ""
    return url_for("article_cover", slug=slug)


def public_locale() -> str:
    first_segment = request.path.strip("/").split("/", 1)[0]
    return first_segment if first_segment in SUPPORTED_LOCALES else "zh"


def localized_home_content(row: sqlite3.Row, locale: str) -> dict[str, str]:
    if locale == "zh":
        return {
            "hero_title": row["hero_title_zh"] or row["hero_title"],
            "hero_subtitle": row["hero_subtitle_zh"] or row["hero_subtitle"],
            "hero_image_path": row["hero_image_path"] or "",
            "about_title": row["about_title_zh"] or row["about_title"],
            "about_body": row["about_body_zh"] or row["about_body"],
        }
    return {
        "hero_title": row["hero_title"] or row["hero_title_zh"],
        "hero_subtitle": row["hero_subtitle"] or row["hero_subtitle_zh"],
        "hero_image_path": row["hero_image_path"] or "",
        "about_title": row["about_title"] or row["about_title_zh"],
        "about_body": row["about_body"] or row["about_body_zh"],
    }


@app.context_processor
def inject_site_context() -> dict[str, object]:
    locale = public_locale()
    site_url = app.config["SITE_URL"] or request.url_root.rstrip("/")
    canonical_url = urljoin(f"{site_url}/", request.path.lstrip("/"))
    social_links = {"x_url": "", "youtube_url": ""}
    try:
        with closing(get_db_connection()) as conn:
            row = conn.execute(
                "SELECT x_url, youtube_url FROM homepage_content WHERE id = 1"
            ).fetchone()
        if row:
            social_links = {
                "x_url": row["x_url"] or "",
                "youtube_url": row["youtube_url"] or "",
            }
    except sqlite3.Error:
        app.logger.warning("Unable to load social links", exc_info=True)
    return {
        "admin_logged_in": admin_is_authenticated(),
        "canonical_url": canonical_url,
        "locale": locale,
        "ui": UI_COPY[locale],
        "social_links": social_links,
        "site_url": site_url,
        "current_year": datetime.now().year,
        "static_asset_url": static_asset_url,
        "cover_url": cover_url,
    }


@app.after_request
def add_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "; ".join(
            [
                "default-src 'self'",
                "base-uri 'self'",
                "connect-src 'self'",
                "font-src 'self'",
                "form-action 'self'",
                "frame-ancestors 'self'",
                "frame-src https://www.youtube-nocookie.com",
                "img-src 'self' data: https://img.youtube.com https://i.ytimg.com",
                "object-src 'none'",
                "script-src 'self'",
                "style-src 'self'",
            ]
        ),
    )
    if app.config["APP_ENV"] == "production" and request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.template_filter("nice_date")
def nice_date(value):
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        if public_locale() == "zh":
            return f"{dt.year}年{dt.month}月{dt.day}日"
        return dt.strftime("%b %d, %Y")
    except ValueError:
        return value


@app.route("/")
def homepage():
    return redirect(url_for("homepage_localized", locale="zh"))


def require_locale(locale: str) -> str:
    if locale not in SUPPORTED_LOCALES:
        abort(404)
    return locale


@app.route("/<locale>/")
def homepage_localized(locale):
    locale = require_locale(locale)
    with closing(get_db_connection()) as conn:
        home_row = conn.execute(
            "SELECT * FROM homepage_content WHERE id = 1"
        ).fetchone()
        articles = conn.execute(
            """
            SELECT * FROM articles
            WHERE language = ?
            ORDER BY created_at DESC
            LIMIT 4
            """,
            (locale,),
        ).fetchall()
    videos = latest_videos(4)
    home_content = localized_home_content(home_row, locale)
    meta_description = home_content["hero_subtitle"]
    meta_title = (
        "AndyJingLiu · 文章、思考与视频"
        if locale == "zh"
        else "AndyJingLiu · Writing, ideas, and video"
    )
    alternate_url = url_for(
        "homepage_localized", locale="en" if locale == "zh" else "zh"
    )
    return render_template(
        "homepage.html",
        home_content=home_content,
        articles=articles,
        videos=videos,
        alternate_url=alternate_url,
        hreflang_url=alternate_url,
        meta_title=meta_title,
        meta_description=meta_description,
    )


@app.route("/articles")
def articles():
    return redirect(url_for("articles_localized", locale="zh"))


@app.route("/<locale>/articles")
def articles_localized(locale):
    locale = require_locale(locale)
    with closing(get_db_connection()) as conn:
        article_rows = conn.execute(
            """
            SELECT id, title, summary, slug, image_path, language, created_at
            FROM articles
            WHERE language = ?
            ORDER BY created_at DESC
            """,
            (locale,),
        ).fetchall()
    alternate_url = url_for(
        "articles_localized", locale="en" if locale == "zh" else "zh"
    )
    return render_template(
        "articles.html",
        articles=article_rows,
        alternate_url=alternate_url,
        hreflang_url=alternate_url,
        meta_title=(
            "文章 · AndyJingLiu" if locale == "zh" else "Articles · AndyJingLiu"
        ),
        meta_description=UI_COPY[locale]["articles_intro"],
    )


@app.route("/articles/<slug>")
def article_detail(slug):
    with closing(get_db_connection()) as conn:
        article = conn.execute(
            "SELECT slug, language FROM articles WHERE slug = ?", (slug,)
        ).fetchone()
    if article is None:
        abort(404)
    return redirect(
        url_for("article_detail_localized", locale=article["language"], slug=slug)
    )


@app.route("/<locale>/articles/<slug>")
def article_detail_localized(locale, slug):
    locale = require_locale(locale)
    with closing(get_db_connection()) as conn:
        article = conn.execute(
            "SELECT * FROM articles WHERE slug = ? AND language = ?", (slug, locale)
        ).fetchone()
    if article is None:
        abort(404)

    body_html = render_markdown(article["body"])
    og_image = None
    if article["image_path"]:
        og_image = urljoin(
            f"{app.config['SITE_URL'] or request.url_root.rstrip('/')}/",
            static_asset_url(article["image_path"]).lstrip("/"),
        )
    return render_template(
        "article_detail.html",
        article=article,
        body_html=body_html,
        alternate_url=url_for(
            "articles_localized", locale="en" if locale == "zh" else "zh"
        ),
        meta_title=f"{article['title']} · AndyJingLiu",
        meta_description=article["summary"] or auto_summary(article["body"]),
        og_image=og_image,
    )


@app.route("/videos")
def videos():
    return redirect(url_for("videos_localized", locale="zh"))


@app.route("/<locale>/videos")
def videos_localized(locale):
    locale = require_locale(locale)
    video_rows = latest_videos(12)
    alternate_url = url_for("videos_localized", locale="en" if locale == "zh" else "zh")
    return render_template(
        "videos.html",
        videos=video_rows,
        alternate_url=alternate_url,
        hreflang_url=alternate_url,
        meta_title=("视频 · AndyJingLiu" if locale == "zh" else "Videos · AndyJingLiu"),
        meta_description=UI_COPY[locale]["videos_intro"],
    )


@app.route("/<locale>/about")
def about_localized(locale):
    locale = require_locale(locale)
    with closing(get_db_connection()) as conn:
        home_row = conn.execute(
            "SELECT * FROM homepage_content WHERE id = 1"
        ).fetchone()
    home_content = localized_home_content(home_row, locale)
    alternate_url = url_for("about_localized", locale="en" if locale == "zh" else "zh")
    return render_template(
        "about.html",
        home_content=home_content,
        alternate_url=alternate_url,
        hreflang_url=alternate_url,
        meta_title=(
            "关于我 · AndyJingLiu" if locale == "zh" else "About · AndyJingLiu"
        ),
        meta_description=home_content["about_body"],
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if admin_is_authenticated():
        return redirect(url_for("admin_dashboard"))

    configured = bool(
        app.config["ADMIN_USERNAME"] and app.config["ADMIN_PASSWORD_HASH"]
    )
    error = None
    next_url = request.args.get("next") or request.form.get("next")

    if request.method == "POST":
        client_key = request.remote_addr or "unknown"
        if not configured:
            error = "Admin login has not been configured."
        elif login_rate_limited(client_key):
            error = "Too many login attempts. Please try again later."
        else:
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            username_ok = hmac.compare_digest(username, app.config["ADMIN_USERNAME"])
            password_ok = check_password_hash(
                app.config["ADMIN_PASSWORD_HASH"], password
            )
            if username_ok and password_ok:
                login_attempts.pop(client_key, None)
                session.clear()
                session["admin_authenticated"] = True
                session["admin_username"] = app.config["ADMIN_USERNAME"]
                session.permanent = True
                return redirect(safe_next_url(next_url))
            login_attempts.setdefault(client_key, []).append(time.monotonic())
            error = "Invalid username or password."

    return render_template(
        "admin_login.html",
        error=error,
        configured=configured,
        next_url=next_url or "",
        meta_title="Admin Login · AndyJingLiu",
        meta_robots="noindex, nofollow",
    )


@app.route("/admin/logout", methods=["POST"])
@login_required
def admin_logout():
    session.clear()
    return redirect(url_for("homepage_localized", locale="zh"))


@app.route("/admin")
@login_required
def admin_dashboard():
    with closing(get_db_connection()) as conn:
        article_rows = conn.execute("""
            SELECT id, title, language, created_at
            FROM articles
            ORDER BY created_at DESC
            LIMIT 5
            """).fetchall()
    video_rows = latest_videos(5)
    return render_template(
        "admin_dashboard.html",
        articles=article_rows,
        videos=video_rows,
        meta_title="Admin Dashboard · AndyJingLiu",
        meta_robots="noindex, nofollow",
    )


@app.route("/admin/homepage", methods=["GET", "POST"])
@login_required
def admin_homepage():
    error = None
    with closing(get_db_connection()) as conn:
        if request.method == "POST":
            form_data = {
                "hero_title": request.form.get("hero_title", "").strip(),
                "hero_subtitle": request.form.get("hero_subtitle", "").strip(),
                "hero_image_path": request.form.get("hero_image_path", "").strip(),
                "about_title": request.form.get("about_title", "").strip(),
                "about_body": request.form.get("about_body", "").strip(),
                "hero_title_zh": request.form.get("hero_title_zh", "").strip(),
                "hero_subtitle_zh": request.form.get("hero_subtitle_zh", "").strip(),
                "about_title_zh": request.form.get("about_title_zh", "").strip(),
                "about_body_zh": request.form.get("about_body_zh", "").strip(),
                "x_url": request.form.get("x_url", "").strip(),
                "youtube_url": request.form.get("youtube_url", "").strip(),
            }
            if not all(
                form_data[key]
                for key in (
                    "hero_title",
                    "hero_subtitle",
                    "about_title",
                    "about_body",
                    "hero_title_zh",
                    "hero_subtitle_zh",
                    "about_title_zh",
                    "about_body_zh",
                )
            ):
                error = "Hero and about text fields are required."
            elif not validate_image_path(form_data["hero_image_path"]):
                error = "Hero image must be inside static/images."
            elif not validate_social_url(form_data["x_url"], {"x.com", "twitter.com"}):
                error = "X profile must be a valid https://x.com URL."
            elif not validate_social_url(
                form_data["youtube_url"], {"youtube.com", "youtu.be"}
            ):
                error = "YouTube channel must be a valid HTTPS YouTube URL."
            else:
                conn.execute(
                    """
                    UPDATE homepage_content
                    SET hero_title = ?, hero_subtitle = ?, hero_image_path = ?,
                        about_title = ?, about_body = ?, hero_title_zh = ?,
                        hero_subtitle_zh = ?, about_title_zh = ?, about_body_zh = ?,
                        x_url = ?, youtube_url = ?
                    WHERE id = 1
                    """,
                    tuple(form_data.values()),
                )
                conn.commit()
                flash("Homepage updated.", "success")
                return redirect(url_for("homepage_localized", locale="zh"))

        homepage_row = conn.execute(
            "SELECT * FROM homepage_content WHERE id = 1"
        ).fetchone()

    return render_template(
        "admin_homepage.html",
        homepage=homepage_row,
        error=error,
        meta_title="Edit Homepage · AndyJingLiu",
        meta_robots="noindex, nofollow",
    )


@app.route("/admin/new-article", methods=["GET", "POST"])
@login_required
def new_article():
    form_data = {
        "language": "zh",
        "title": "",
        "summary": "",
        "body": "",
        "image_path": "",
    }
    error = None
    if request.method == "POST":
        form_data = {key: (request.form.get(key) or "").strip() for key in form_data}
        form_data["body"] = normalize_article_markdown(
            form_data["body"], form_data["title"]
        )
        error = validate_article_form(form_data)
        if error is None:
            try:
                with closing(get_db_connection()) as conn:
                    slug = generate_unique_slug(form_data["title"], conn)
                    summary = form_data["summary"] or auto_summary(form_data["body"])
                    conn.execute(
                        """
                        INSERT INTO articles
                            (title, slug, summary, body, image_path, language)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            form_data["title"],
                            slug,
                            summary,
                            form_data["body"],
                            form_data["image_path"],
                            form_data["language"],
                        ),
                    )
                    conn.commit()
                flash("Article published.", "success")
                return redirect(
                    url_for(
                        "article_detail_localized",
                        locale=form_data["language"],
                        slug=slug,
                    )
                )
            except sqlite3.Error:
                app.logger.exception("Unable to create article")
                error = "An error occurred while saving the article."

    return render_template(
        "admin_new_article.html",
        error=error,
        form=form_data,
        meta_title="New Article · AndyJingLiu",
        meta_robots="noindex, nofollow",
    )


@app.route("/admin/articles/<int:article_id>/edit", methods=["GET", "POST"])
@login_required
def edit_article(article_id):
    with closing(get_db_connection()) as conn:
        article = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if article is None:
            abort(404)

        form_data = {
            "language": article["language"],
            "title": article["title"],
            "summary": article["summary"] or "",
            "body": article["body"],
            "image_path": article["image_path"] or "",
        }
        error = None
        if request.method == "POST":
            form_data = {
                key: (request.form.get(key) or "").strip() for key in form_data
            }
            form_data["body"] = normalize_article_markdown(
                form_data["body"], form_data["title"]
            )
            error = validate_article_form(form_data)
            if error is None:
                slug = slugify(form_data["title"])
                existing = conn.execute(
                    "SELECT id FROM articles WHERE slug = ? AND id != ?",
                    (slug, article_id),
                ).fetchone()
                if existing:
                    error = "Another article already uses this title."
                else:
                    summary = form_data["summary"] or auto_summary(form_data["body"])
                    try:
                        conn.execute(
                            """
                            UPDATE articles
                            SET title = ?, slug = ?, summary = ?,
                                body = ?, image_path = ?, language = ?
                            WHERE id = ?
                            """,
                            (
                                form_data["title"],
                                slug,
                                summary,
                                form_data["body"],
                                form_data["image_path"],
                                form_data["language"],
                                article_id,
                            ),
                        )
                        conn.commit()
                        flash("Article updated.", "success")
                        return redirect(
                            url_for(
                                "article_detail_localized",
                                locale=form_data["language"],
                                slug=slug,
                            )
                        )
                    except sqlite3.Error:
                        app.logger.exception("Unable to update article %s", article_id)
                        error = "An error occurred while updating the article."

    return render_template(
        "admin_edit_article.html",
        error=error,
        form=form_data,
        article=article,
        meta_title="Edit Article · AndyJingLiu",
        meta_robots="noindex, nofollow",
    )


@app.route("/admin/articles/<int:article_id>/delete", methods=["POST"])
@login_required
def delete_article(article_id: int):
    try:
        with closing(get_db_connection()) as conn:
            cursor = conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            conn.commit()
    except sqlite3.Error:
        app.logger.exception("Unable to delete article %s", article_id)
        abort(500)

    if cursor.rowcount == 0:
        flash("Article not found.", "error")
    else:
        flash("Article deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/robots.txt")
def robots_txt():
    sitemap_url = urljoin(
        f"{app.config['SITE_URL'] or request.url_root.rstrip('/')}/", "sitemap.xml"
    )
    body = f"User-agent: *\nDisallow: /admin\nSitemap: {sitemap_url}\n"
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    with closing(get_db_connection()) as conn:
        article_rows = conn.execute(
            "SELECT slug, language, created_at FROM articles ORDER BY created_at DESC"
        ).fetchall()
    return render_template("sitemap.xml", articles=article_rows), {
        "Content-Type": "application/xml; charset=utf-8"
    }


@app.route("/healthz")
def healthcheck():
    with closing(get_db_connection()) as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.errorhandler(400)
def bad_request(error):
    return (
        render_template(
            "400.html",
            error=error,
            meta_title="Bad Request · AndyJingLiu",
            meta_robots="noindex, nofollow",
        ),
        400,
    )


@app.errorhandler(404)
def page_not_found(error):
    return (
        render_template(
            "404.html",
            meta_title="Page Not Found · AndyJingLiu",
            meta_robots="noindex, nofollow",
        ),
        404,
    )


@app.errorhandler(500)
def internal_server_error(error):
    return (
        render_template(
            "500.html",
            meta_title="Server Error · AndyJingLiu",
            meta_robots="noindex, nofollow",
        ),
        500,
    )


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=env_bool("FLASK_DEBUG", False))
