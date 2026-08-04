import sqlite3


def session_csrf(client):
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_public_pages_and_metadata(client):
    for path in ["/", "/articles", "/videos", "/robots.txt", "/sitemap.xml"]:
        response = client.get(path)
        assert response.status_code == 200

    homepage = client.get("/")
    assert b'<meta name="viewport"' in homepage.data
    assert b'<link rel="canonical" href="https://andyjingliu.com/"' in homepage.data
    assert b"Andy Jing Liu" in homepage.data
    assert homepage.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in homepage.headers["Content-Security-Policy"]


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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM articles WHERE slug = 'security-test'"
            ).fetchone()[0]
            == 1
        )


def test_article_form_rejects_unsafe_image_path(logged_in_client):
    response = logged_in_client.post(
        "/admin/new-article",
        data={
            "csrf_token": session_csrf(logged_in_client),
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
            "hero_title": "Andy Jing Liu",
            "hero_subtitle": "Insights and analysis",
            "hero_image_path": "images/IMG_1761.webp",
            "about_title": "About Andy",
            "about_body": "About text",
            "x_url": "https://x.com/andy",
            "youtube_url": "https://www.youtube.com/@andy",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Follow on X" in response.data
    assert b"Watch on YouTube" in response.data


def test_unknown_page_returns_custom_404(client):
    response = client.get("/not-a-real-page")
    assert response.status_code == 404
    assert b"Page Not Found" in response.data


def test_healthcheck(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}
