import sqlite3
from io import BytesIO

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
    assert homepage.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in homepage.headers["Content-Security-Policy"]


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
            "hero_image_path": "images/IMG_1761.webp",
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
