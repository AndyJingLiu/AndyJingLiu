import pytest
from werkzeug.security import generate_password_hash

import app as project


@pytest.fixture()
def flask_app(tmp_path):
    project.app.config.update(
        TESTING=True,
        DATABASE=str(tmp_path / "test.db"),
        SECRET_KEY="test-secret-key",
        ADMIN_USERNAME="andy",
        ADMIN_PASSWORD_HASH=generate_password_hash("correct-horse-battery-staple"),
        SITE_URL="https://andyjingliu.com",
        APP_ENV="testing",
        SESSION_COOKIE_SECURE=False,
    )
    project.login_attempts.clear()
    with project.app.app_context():
        project.init_db(seed=True)
    yield project.app


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture()
def csrf_token(client):
    client.get("/admin/login")
    with client.session_transaction() as session:
        return session["csrf_token"]


@pytest.fixture()
def logged_in_client(client, csrf_token):
    response = client.post(
        "/admin/login",
        data={
            "username": "andy",
            "password": "correct-horse-battery-staple",
            "csrf_token": csrf_token,
        },
    )
    assert response.status_code == 302
    client.get("/admin")
    return client
