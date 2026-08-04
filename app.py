import hmac
import json
import os
import re
import secrets
import sqlite3
import string
import time
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

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


ALLOWED_MARKDOWN_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
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
login_attempts: dict[str, list[float]] = {}


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
             about_title, about_body, x_url, youtube_url)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            homepage["hero_title"],
            homepage["hero_subtitle"],
            homepage.get("hero_image_path", ""),
            homepage["about_title"],
            homepage["about_body"],
            homepage.get("x_url", ""),
            homepage.get("youtube_url", ""),
        ),
    )

    conn.executemany(
        """
        INSERT OR IGNORE INTO articles
            (title, slug, summary, body, image_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                article["title"],
                article["slug"],
                article.get("summary", ""),
                article["body"],
                article.get("image_path", ""),
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


def generate_unique_slug(title: str, conn: sqlite3.Connection) -> str:
    base_slug = slugify(title)
    slug = base_slug
    counter = 2
    while conn.execute("SELECT 1 FROM articles WHERE slug = ?", (slug,)).fetchone():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


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


@app.context_processor
def inject_site_context() -> dict[str, object]:
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
        "social_links": social_links,
        "site_url": site_url,
        "current_year": datetime.now().year,
        "static_asset_url": static_asset_url,
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
        return dt.strftime("%b %d, %Y")
    except ValueError:
        return value


@app.route("/")
def homepage():
    with closing(get_db_connection()) as conn:
        home_content = conn.execute(
            "SELECT * FROM homepage_content WHERE id = 1"
        ).fetchone()
        articles = conn.execute(
            "SELECT * FROM articles ORDER BY created_at DESC LIMIT 4"
        ).fetchall()
        videos = conn.execute(
            "SELECT * FROM videos ORDER BY id DESC LIMIT 4"
        ).fetchall()
    return render_template(
        "homepage.html",
        home_content=home_content,
        articles=articles,
        videos=videos,
        meta_title="Andy Jing Liu · Insights & Analysis",
        meta_description=(home_content["hero_subtitle"] if home_content else ""),
    )


@app.route("/articles")
def articles():
    with closing(get_db_connection()) as conn:
        article_rows = conn.execute("""
            SELECT id, title, summary, slug, image_path, created_at
            FROM articles
            ORDER BY created_at DESC
            """).fetchall()
    return render_template(
        "articles.html",
        articles=article_rows,
        meta_title="Articles · Andy Jing Liu",
        meta_description="Long-form articles and commentary by Andy Jing Liu.",
    )


@app.route("/articles/<slug>")
def article_detail(slug):
    with closing(get_db_connection()) as conn:
        article = conn.execute(
            "SELECT * FROM articles WHERE slug = ?", (slug,)
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
        meta_title=f"{article['title']} · Andy Jing Liu",
        meta_description=article["summary"] or auto_summary(article["body"]),
        og_image=og_image,
    )


@app.route("/videos")
def videos():
    with closing(get_db_connection()) as conn:
        video_rows = conn.execute(
            "SELECT id, title, youtube_id, description FROM videos ORDER BY id DESC"
        ).fetchall()
    return render_template(
        "videos.html",
        videos=video_rows,
        meta_title="Videos · Andy Jing Liu",
        meta_description="Videos and visual explainers from Andy Jing Liu.",
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
        meta_title="Admin Login · Andy Jing Liu",
        meta_robots="noindex, nofollow",
    )


@app.route("/admin/logout", methods=["POST"])
@login_required
def admin_logout():
    session.clear()
    return redirect(url_for("homepage"))


@app.route("/admin")
@login_required
def admin_dashboard():
    with closing(get_db_connection()) as conn:
        article_rows = conn.execute("""
            SELECT id, title, created_at
            FROM articles
            ORDER BY created_at DESC
            LIMIT 5
            """).fetchall()
        video_rows = conn.execute(
            "SELECT id, title FROM videos ORDER BY id DESC LIMIT 5"
        ).fetchall()
    return render_template(
        "admin_dashboard.html",
        articles=article_rows,
        videos=video_rows,
        meta_title="Admin Dashboard · Andy Jing Liu",
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
                "x_url": request.form.get("x_url", "").strip(),
                "youtube_url": request.form.get("youtube_url", "").strip(),
            }
            if not all(
                form_data[key]
                for key in ("hero_title", "hero_subtitle", "about_title", "about_body")
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
                        about_title = ?, about_body = ?, x_url = ?, youtube_url = ?
                    WHERE id = 1
                    """,
                    tuple(form_data.values()),
                )
                conn.commit()
                flash("Homepage updated.", "success")
                return redirect(url_for("homepage"))

        homepage_row = conn.execute(
            "SELECT * FROM homepage_content WHERE id = 1"
        ).fetchone()

    return render_template(
        "admin_homepage.html",
        homepage=homepage_row,
        error=error,
        meta_title="Edit Homepage · Andy Jing Liu",
        meta_robots="noindex, nofollow",
    )


@app.route("/admin/new-article", methods=["GET", "POST"])
@login_required
def new_article():
    form_data = {
        "title": "",
        "summary": "",
        "body": "",
        "image_path": "",
    }
    error = None
    if request.method == "POST":
        form_data = {key: (request.form.get(key) or "").strip() for key in form_data}
        error = validate_article_form(form_data)
        if error is None:
            try:
                with closing(get_db_connection()) as conn:
                    slug = generate_unique_slug(form_data["title"], conn)
                    summary = form_data["summary"] or auto_summary(form_data["body"])
                    conn.execute(
                        """
                        INSERT INTO articles (title, slug, summary, body, image_path)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            form_data["title"],
                            slug,
                            summary,
                            form_data["body"],
                            form_data["image_path"],
                        ),
                    )
                    conn.commit()
                flash("Article published.", "success")
                return redirect(url_for("article_detail", slug=slug))
            except sqlite3.Error:
                app.logger.exception("Unable to create article")
                error = "An error occurred while saving the article."

    return render_template(
        "admin_new_article.html",
        error=error,
        form=form_data,
        meta_title="New Article · Andy Jing Liu",
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
                                body = ?, image_path = ?
                            WHERE id = ?
                            """,
                            (
                                form_data["title"],
                                slug,
                                summary,
                                form_data["body"],
                                form_data["image_path"],
                                article_id,
                            ),
                        )
                        conn.commit()
                        flash("Article updated.", "success")
                        return redirect(url_for("article_detail", slug=slug))
                    except sqlite3.Error:
                        app.logger.exception("Unable to update article %s", article_id)
                        error = "An error occurred while updating the article."

    return render_template(
        "admin_edit_article.html",
        error=error,
        form=form_data,
        article=article,
        meta_title="Edit Article · Andy Jing Liu",
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
            "SELECT slug, created_at FROM articles ORDER BY created_at DESC"
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
            meta_title="Bad Request · Andy Jing Liu",
            meta_robots="noindex, nofollow",
        ),
        400,
    )


@app.errorhandler(404)
def page_not_found(error):
    return (
        render_template(
            "404.html",
            meta_title="Page Not Found · Andy Jing Liu",
            meta_robots="noindex, nofollow",
        ),
        404,
    )


@app.errorhandler(500)
def internal_server_error(error):
    return (
        render_template(
            "500.html",
            meta_title="Server Error · Andy Jing Liu",
            meta_robots="noindex, nofollow",
        ),
        500,
    )


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=env_bool("FLASK_DEBUG", False))
