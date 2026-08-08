import json
import sqlite3
from io import BytesIO
from urllib.error import URLError

import app as project


def session_csrf(client):
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_public_pages_and_metadata(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/zh/")
    assert client.get("/articles").headers["Location"].endswith("/zh/articles")
    assert client.get("/videos").headers["Location"].endswith("/zh/videos")

    public_paths = [
        "/zh/",
        "/en/",
        "/zh/articles",
        "/en/articles",
        "/zh/videos",
        "/en/videos",
        "/zh/about",
        "/en/about",
        "/robots.txt",
        "/sitemap.xml",
    ]
    for path in public_paths:
        response = client.get(path)
        assert response.status_code == 200

    homepage = client.get("/zh/")
    assert b'<meta name="viewport"' in homepage.data
    assert b'<link rel="canonical" href="https://andyjingliu.com/zh/"' in homepage.data
    assert b"AndyJingLiu" in homepage.data
    assert "这是我的个人网站".encode() in homepage.data
    assert b'href="/en/"' in homepage.data
    assert b'rel="alternate" hreflang="en"' in homepage.data
    assert b"portrait-480.webp" in homepage.data
    assert b"portrait-800.webp" in homepage.data
    assert b"source-serif-4-latin-wght-normal.woff2?v=" not in homepage.data
    assert b"inter-latin-wght-normal.woff2?v=" not in homepage.data
    assert homepage.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in homepage.headers["Content-Security-Policy"]

    font = client.get("/static/fonts/inter-latin-wght-normal.woff2")
    assert font.headers["Content-Type"] == "font/woff2"


def test_latest_youtube_videos_are_loaded_and_shorts_are_excluded(
    client, flask_app, monkeypatch
):
    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry><yt:videoId>regular1234</yt:videoId><title>Newest regular video</title>
        <published>2026-08-01T12:00:00+00:00</published>
        <link rel="alternate" href="https://www.youtube.com/watch?v=regular1234" />
      </entry>
      <entry><yt:videoId>short123456</yt:videoId><title>A short</title>
        <published>2026-08-01T11:00:00+00:00</published>
        <link rel="alternate" href="https://www.youtube.com/shorts/short123456" />
      </entry>
    </feed>"""

    flask_app.config["YOUTUBE_CHANNEL_ID"] = "UCL6USkBdRjEeOpeLo2Lq9aA"
    project.youtube_feed_cache.update(
        {"channel_id": "", "expires_at": 0.0, "videos": []}
    )
    monkeypatch.setattr(project, "urlopen", lambda *args, **kwargs: BytesIO(feed))

    response = client.get("/zh/videos")
    assert response.status_code == 200
    assert b"Newest regular video" in response.data
    assert b"regular1234" in response.data
    assert b"A short" not in response.data


def test_channel_page_is_used_when_youtube_feed_is_unavailable(
    client, flask_app, monkeypatch
):
    page_data = {
        "contents": [
            {
                "videoRenderer": {
                    "videoId": "realvideo01",
                    "title": {"runs": [{"text": "A real channel video"}]},
                }
            },
            {
                "lockupViewModel": {
                    "contentId": "newvideo001",
                    "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                    "metadata": {
                        "lockupMetadataViewModel": {
                            "title": {"content": "A new-format channel video"}
                        }
                    },
                }
            },
            {
                "reelItemRenderer": {
                    "videoId": "shortvid001",
                    "headline": {"simpleText": "A Short"},
                }
            },
        ]
    }
    page = (
        "<script>var ytInitialData = " + json.dumps(page_data) + ";</script>"
    ).encode()

    def fake_urlopen(request, timeout):
        if "feeds/videos.xml" in request.full_url:
            raise URLError("feed unavailable")
        assert request.full_url.endswith("/channel/UCL6USkBdRjEeOpeLo2Lq9aA/videos")
        return BytesIO(page)

    flask_app.config["YOUTUBE_CHANNEL_ID"] = "UCL6USkBdRjEeOpeLo2Lq9aA"
    project.youtube_feed_cache.update(
        {"channel_id": "", "expires_at": 0.0, "videos": []}
    )
    monkeypatch.setattr(project, "urlopen", fake_urlopen)

    response = client.get("/zh/videos")
    assert response.status_code == 200
    assert b"A real channel video" in response.data
    assert b"realvideo01" in response.data
    assert b"A new-format channel video" in response.data
    assert b"newvideo001" in response.data
    assert b"A Short" not in response.data
    assert b'width="480" height="360"' in response.data


def test_database_migration_removes_only_legacy_demo_videos(flask_app):
    with sqlite3.connect(flask_app.config["DATABASE"]) as conn:
        conn.executemany(
            "INSERT INTO videos (title, youtube_id, description) VALUES (?, ?, '')",
            [
                ("Legacy one", "fkIvmfqX-t0"),
                ("Legacy two", "KZpYtNtGxSU"),
                ("Andy's video", "realvideo01"),
            ],
        )

    with flask_app.app_context():
        project.init_db(seed=False)

    with sqlite3.connect(flask_app.config["DATABASE"]) as conn:
        remaining = conn.execute(
            "SELECT youtube_id FROM videos ORDER BY youtube_id"
        ).fetchall()
    assert remaining == [("realvideo01",)]


def test_database_migration_normalizes_existing_article_headings(flask_app):
    with sqlite3.connect(flask_app.config["DATABASE"]) as conn:
        conn.execute(
            "UPDATE articles SET body = ? WHERE id = 1",
            ("# Why I Built This Site\n\n### A skipped heading",),
        )

    with flask_app.app_context():
        project.init_db(seed=False)

    with sqlite3.connect(flask_app.config["DATABASE"]) as conn:
        body = conn.execute("SELECT body FROM articles WHERE id = 1").fetchone()[0]
    assert body == "## A skipped heading"


def test_admin_routes_require_login(client):
    response = client.get("/admin")
    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]

    response = client.get("/admin/new-article")
    assert response.status_code == 302


def test_csrf_is_required(client):
    response = client.post(
        "/admin/login",
        data={"username": "andy", "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 400


def test_login_rejects_wrong_password(client, csrf_token):
    response = client.post(
        "/admin/login",
        data={
            "username": "andy",
            "password": "wrong",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 200
    assert b"Invalid username or password" in response.data


def test_authenticated_admin_can_open_dashboard(logged_in_client):
    response = logged_in_client.get("/admin")
    assert response.status_code == 200
    assert b"Admin Dashboard" in response.data


def test_article_creation_sanitizes_html(logged_in_client, flask_app):
    response = logged_in_client.post(
        "/admin/new-article",
        data={
            "csrf_token": session_csrf(logged_in_client),
            "language": "zh",
            "title": "Security Test",
            "summary": "A safe summary",
            "body": "# Hello\n\n<script>alert('xss')</script>\n\n**Safe text**",
            "image_path": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"<script>alert" not in response.data
    assert b"<strong>Safe text</strong>" in response.data
    assert b"<h2>Hello</h2>" in response.data
    assert response.data.count(b"<h1") == 1

    with sqlite3.connect(flask_app.config["DATABASE"]) as conn:
        row = conn.execute(
            "SELECT language FROM articles WHERE slug = 'security-test'"
        ).fetchone()
        assert row == ("zh",)


def test_article_form_rejects_unsafe_image_path(logged_in_client):
    response = logged_in_client.post(
        "/admin/new-article",
        data={
            "csrf_token": session_csrf(logged_in_client),
            "language": "zh",
            "title": "Unsafe image",
            "summary": "",
            "body": "Body",
            "image_path": "../secret.txt",
        },
    )
    assert response.status_code == 200
    assert b"inside static/images" in response.data


def test_social_links_can_be_saved(logged_in_client):
    response = logged_in_client.post(
        "/admin/homepage",
        data={
            "csrf_token": session_csrf(logged_in_client),
            "hero_title": "AndyJingLiu",
            "hero_subtitle": "Insights and analysis",
            "about_title": "About Andy",
            "about_body": "About text",
            "hero_title_zh": "AndyJingLiu",
            "hero_subtitle_zh": "我的个人网站",
            "about_title_zh": "关于我",
            "about_body_zh": "这里收录我的文章和视频。",
            "hero_image_path": "images/portrait.webp",
            "x_url": "https://x.com/andy",
            "youtube_url": "https://www.youtube.com/@andy",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "关注我的 X".encode() in response.data
    assert "访问 YouTube 频道".encode() in response.data


def test_unknown_page_returns_custom_404(client):
    response = client.get("/not-a-real-page", follow_redirects=True)
    assert response.status_code == 404
    assert b"Page Not Found" in response.data


def test_healthcheck(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_generated_cover_is_served_and_deterministic(client):
    first = client.get("/covers/why-i-built-this-site.svg")
    second = client.get("/covers/why-i-built-this-site.svg")
    assert first.status_code == 200
    assert first.headers["Content-Type"].startswith("image/svg+xml")
    assert "immutable" in first.headers["Cache-Control"]
    assert first.data == second.data
    assert b"<svg" in first.data


def test_generated_covers_differ_between_articles(client):
    one = client.get("/covers/why-i-built-this-site.svg").data
    two = client.get("/covers/china-canada-future.svg").data
    assert one != two


def test_generated_cover_rejects_paths_that_are_not_slugs(client):
    for bad in ("Not-A-Slug", "has_underscore", "-leading", "trailing-"):
        assert client.get(f"/covers/{bad}.svg").status_code == 404


def test_every_cover_is_drawn_and_carries_the_accent():
    """No slug may produce a blank cover or one with no burgundy element.

    Each composition deliberately runs off the frame, so this guards the case
    where the drawing lands entirely outside the crop.
    """
    for index in range(120):
        svg = project.render_cover_svg(f"article-{index}")
        assert svg.count("<") > 10
        assert project.COVER_BURGUNDY in svg
        assert project.COVER_INK in svg


def test_article_without_an_image_falls_back_to_a_generated_cover(client):
    response = client.get("/en/articles")
    assert response.status_code == 200
    assert b"/covers/" in response.data


def test_article_has_one_h1_and_no_false_translation_alternate(client):
    response = client.get("/en/articles/why-i-built-this-site")
    assert response.status_code == 200
    assert response.data.count(b"<h1") == 1
    assert b'<link rel="alternate"' not in response.data
    assert b'href="/zh/articles" hreflang="zh-CN"' in response.data


def test_article_markdown_normalizes_h1_outside_code_fences():
    markdown = (
        "# Article title\n\n# Section\n\n#### Deep section\n\n"
        "```text\n# Code stays code\n```"
    )
    normalized = project.normalize_article_markdown(markdown, "Article title")
    assert normalized.startswith("## Section")
    assert "### Deep section" in normalized
    assert "# Code stays code" in normalized
    assert "## Code stays code" not in normalized


def test_admin_can_edit_and_delete_articles(logged_in_client, flask_app):
    edit_response = logged_in_client.post(
        "/admin/articles/1/edit",
        data={
            "csrf_token": session_csrf(logged_in_client),
            "language": "en",
            "title": "A Better Site",
            "summary": "Updated summary",
            "body": "# A Better Site\n\nUpdated body",
            "image_path": "",
        },
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    assert edit_response.data.count(b"<h1") == 1
    assert b"Updated body" in edit_response.data

    delete_response = logged_in_client.post(
        "/admin/articles/2/delete",
        data={"csrf_token": session_csrf(logged_in_client)},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert b"Article deleted" in delete_response.data

    with sqlite3.connect(flask_app.config["DATABASE"]) as conn:
        assert conn.execute("SELECT 1 FROM articles WHERE id = 2").fetchone() is None
