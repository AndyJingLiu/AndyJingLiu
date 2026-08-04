# AndyJingLiu.com

The source code for AndyJingLiu's bilingual personal website: articles, ideas, YouTube videos, and X links in one independent home.

## What the site includes

- Chinese-first public pages with a complete English version and language switcher
- A personal-brand homepage with editable bilingual hero and biography content
- Markdown article publishing, editing, deletion, and per-article language selection
- Sanitized Markdown rendering to prevent stored script injection
- Automatically refreshed latest-video feeds with privacy-enhanced YouTube embeds
- Password-protected admin routes
- CSRF protection on every form that changes data
- SQLite schema initialization and starter-content seeding
- SEO metadata, canonical URLs, Open Graph tags, `robots.txt`, and `sitemap.xml`
- Security headers, production cookies, login rate limiting, and safe environment configuration
- Docker and Gunicorn production startup
- GitHub Actions checks for formatting, linting, tests, and vulnerable dependencies

## Project structure

```text
AndyJingLiu/
├── .github/workflows/ci.yml
├── static/
│   ├── images/
│   ├── favicon.png
│   ├── script.js
│   └── styles.css
├── templates/
├── tests/
├── app.py
├── schema.sql
├── seed_data.json
├── Dockerfile
├── Procfile
├── requirements.txt
└── requirements-dev.txt
```

## Local setup

Create a virtual environment and install the development dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Copy the environment template and generate secure values:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
flask --app app hash-password
```

Put the random token in `SECRET_KEY` and the password command output in `ADMIN_PASSWORD_HASH` inside `.env`. The `.env` file is ignored by Git.

Initialize a new database with starter content, then start the site:

```bash
flask --app app init-db --seed
flask --app app run --debug
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). The admin login is at `/admin/login`.

The X and YouTube channel URLs can be entered under **Admin → Edit homepage**. The latest regular public videos are loaded from YouTube's official channel feed and cached for 15 minutes; Shorts are excluded. Empty social fields are not displayed publicly. Chinese pages live under `/zh/`; English pages live under `/en/`.

## Tests and security checks

```bash
python -m black --check app.py tests
python -m flake8 app.py tests
python -m pytest -q
python -m pip_audit -r requirements.txt
```

The same checks run automatically on GitHub pushes and pull requests.

## Production configuration

Set these environment variables in the hosting platform:

| Variable | Required production value |
| --- | --- |
| `APP_ENV` | `production` |
| `SECRET_KEY` | A long random value; never commit it |
| `ADMIN_USERNAME` | Private admin username |
| `ADMIN_PASSWORD_HASH` | Output from `flask --app app hash-password` |
| `DATABASE_PATH` | A path on a persistent disk, such as `/data/database.db` |
| `SITE_URL` | `https://andyjingliu.com` |
| `YOUTUBE_CHANNEL_ID` | `UCL6USkBdRjEeOpeLo2Lq9aA` |
| `YOUTUBE_CHANNEL_URL` | `https://www.youtube.com/@AndyJingLiu` |
| `TRUST_PROXY` | `true` when hosted behind a trusted reverse proxy |
| `TRUSTED_HOSTS` | `andyjingliu.com,www.andyjingliu.com` |

Run the container with one Gunicorn worker and multiple threads. A single worker is intentional while the site uses SQLite. The database path must be backed by persistent storage and should be included in regular backups.

## Deployment architecture

```text
GitHub repository
        ↓ automatic build/deploy
Python container host + persistent disk
        ↓ origin connection
Cloudflare DNS, HTTPS, caching, and proxy
        ↓
AndyJingLiu.com
```

GitHub Pages cannot run this application because it only serves static files, while this project requires Python, authentication, and a writable database. GitHub remains the source repository and CI trigger; the Flask container runs on a Python-capable host, and Cloudflare manages the public domain in front of it.

## Content data

`seed_data.json` contains public starter content for new deployments. Runtime edits are stored in the SQLite database and are not committed to Git. Back up the persistent database before platform migrations or major schema changes.
