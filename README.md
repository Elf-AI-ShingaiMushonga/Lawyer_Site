# Law Firm Intranet (ELF demo)

This is a functional intranet website for a law firm:
- User authentication + roles (admin/lawyer/staff/paralegal)
- Admin user provisioning from the web UI (`/admin/users`)
- Matters with team membership
- Matter executive summaries (objective, risk, budget, outcome, latest update)
- Matter timelines (filings, hearings, milestones, client updates)
- Human-readable matter activity feed
- Tasks per matter (Todo/Doing/Done)
- Document upload/download with metadata (category, version, stage, owner, privilege) and SHA-256 integrity hash
- Contacts directory
- Knowledge base (internal articles)
- Search across core objects
- Trust center pages (data policy, security posture, incident/change register)
- Audit log (admin view)

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export FLASK_SECRET_KEY="change-me"
# optional: export DATABASE_URL="postgresql+psycopg://..."
flask --app app.py db upgrade -d migrations

python app.py create-user --email admin@firm.local --password "ChangeMeNow!" --role admin --name "Admin User"
python app.py run --debug
```

Open: http://127.0.0.1:5000

## Demo data seed (recommended for client walkthroughs)

Use the built-in seed command to prepopulate realistic records:

```bash
# resets existing records and writes demo data
python app.py seed-demo --reset --password "ClientDemo2026!"
```

Or run the helper script:

```bash
./scripts/seed_demo.sh
```

Seeded demo logins:
- `admin@elf-ai-demo.co.za`
- `partner@elf-ai-demo.co.za`
- `associate@elf-ai-demo.co.za`
- `paralegal@elf-ai-demo.co.za`
- `staff@elf-ai-demo.co.za`

Seeded demo content now includes:
- Three story-pack matters with business impact summaries
- Timeline events and activity-feed entries
- Governance incident/change records
- Rich sample files (`.pdf`, `.docx`, `.txt`)

## Client story mode

After signing in, click `Story Mode Off` in the top navigation to enable guided demo mode.

- `Story Mode On` adds a contextual walkthrough banner across pages.
- Use the `Story` nav tab for a full step-by-step playbook (`/story`).
- Disable it anytime with the same navbar toggle.
- On the login page, `Start Live Demo` signs in, enables story mode, and opens `/story` in one click.

## GitHub upload checklist

- Initialize repository (if needed): `git init`
- Copy environment template: `cp .env.example .env`
- Review `.gitignore` to ensure local secrets and runtime artifacts are excluded.
- Commit and push:
  - `git add .`
  - `git commit -m "Prepare intranet app for deployment"`
  - `git branch -M main`
  - `git remote add origin <your-github-repo-url>`
  - `git push -u origin main`

## Project structure

- `app.py`: entrypoint + CLI (`run`, `init-db`, `create-user`, `seed-demo`)
- `intranet/__init__.py`: Flask app factory and extension wiring
- `intranet/config.py`: environment parsing and config constants
- `intranet/models.py`: SQLAlchemy models
- `intranet/helpers.py`: shared business helpers (audit, access checks, file hash helpers)
- `intranet/routes/`: route modules split by domain (`auth`, `matters`, `content`, `admin`, `ops`)
- `intranet/templates/`: Jinja templates split by domain (`auth`, `matters`, `content`, `admin`, `trust`, `errors`)
- `intranet/security.py`: security headers and error handlers
- `migrations/`: Alembic migration scripts (managed via Flask-Migrate)
- `deploy/ubuntu/`: Ubuntu deployment artifacts (cloud-init, systemd service, Nginx config, Gunicorn config)
- `scripts/seed_demo.sh`: helper wrapper to load demo dataset quickly

## Required production env vars

- `FLASK_ENV=production`
- `FLASK_SECRET_KEY=<long-random-secret>`
- `DATABASE_URL=postgresql+psycopg://...`

Optional but recommended:
- `TRUST_PROXY=true`
- `TRUSTED_PROXY_HOPS=1` (set `2` when running behind ALB + Nginx)
- `FORCE_SECURE_COOKIE=true`
- `RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/0` for multi-worker production
- `RATE_LIMIT_STORAGE_URI=memory://` only for local dev or single-worker setups
- `AUTH_LOGIN_RATE_LIMIT=10/minute`
- `AUTH_REGISTER_RATE_LIMIT=5/hour`
- `GUNICORN_WORKERS=3`
- `GUNICORN_THREADS=2`
- `GUNICORN_TIMEOUT=60`

## Database migrations

- Generate a new migration after model changes:
  - `flask --app app.py db migrate -m "describe-change" -d migrations`
- Apply migrations:
  - `flask --app app.py db upgrade -d migrations`
- Roll back one revision:
  - `flask --app app.py db downgrade -d migrations`

## Ubuntu production deployment

- Assumptions:
  - Ubuntu 22.04+ (VM or EC2 Ubuntu AMI)
  - App path: `/home/ubuntu/<app-dir>` (cloud-init default is `/home/ubuntu/Lawyer_Site`)
  - Gunicorn managed by systemd, Nginx reverse proxy on port 80/443
  - Optional ALB health check path: `/healthz`

- One-click bootstrap with cloud-init (recommended for new EC2 instances):
  - Use `deploy/ubuntu/cloud-init.yaml` as EC2 user data.
  - Optional path/user customization in cloud-init:
    - `APP_USER`
    - `APP_REPO_DIR`
  - Replace placeholders in that file first:
    - `REPO_URL`
    - `APP_DOMAIN` (defaults to `elf-ai-demo.co.za www.elf-ai-demo.co.za`)
    - `FLASK_SECRET_KEY`
    - `DATABASE_URL`
    - `TRUSTED_PROXY_HOPS` (`2` if ALB is in front of Nginx)
    - `ADMIN_PASSWORD`
  - Launch instance, then check bootstrap logs:
    - `sudo tail -n 200 /var/log/law-intranet-bootstrap.log`
    - `sudo tail -n 200 /var/log/cloud-init-output.log`
  - Verify services:
    - `sudo systemctl status law-intranet`
    - `sudo systemctl status nginx`
    - `curl http://127.0.0.1/healthz`

- 1) Install system packages:
  - `sudo apt update -y`
  - `sudo apt install -y python3 python3-venv python3-pip git nginx`

- 2) Clone app and install Python deps:
  - `git clone <your-github-repo-url> /home/ubuntu/Lawyer_Site`
  - `cd /home/ubuntu/Lawyer_Site`
  - `python3 -m venv venv`
  - `source venv/bin/activate`
  - `pip install -r requirements.txt`

- 3) Configure environment:
  - `cp .env.example .env`
  - Generate secret: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
  - Edit `.env` with real `FLASK_SECRET_KEY` and `DATABASE_URL`.
  - If `DATABASE_URL` password contains reserved URL chars (`@:/?#[]`), URL-encode it.
  - Set `TRUSTED_PROXY_HOPS=2` only if request path is `Client -> ALB -> Nginx -> Gunicorn`.

- 4) Initialize DB and bootstrap admin:
  - `source venv/bin/activate`
  - `flask --app app.py db upgrade -d migrations`
  - `python app.py create-user --email shingai.mushonga@elf-ai.co.za --password "<strong-password>" --role admin --name "Admin User"`

- 5) Install systemd service:
  - `sudo cp deploy/ubuntu/systemd/law-intranet.service /etc/systemd/system/law-intranet.service`
  - Rewrite service paths for your actual app directory:
    - `sudo sed -i "s#/home/ubuntu/Lawyer_Site#/home/ubuntu/<app-dir>#g" /etc/systemd/system/law-intranet.service`
  - Gunicorn tuning comes from `deploy/ubuntu/gunicorn.conf.py` and env vars in `.env`.
  - `sudo systemctl daemon-reload`
  - `sudo systemctl enable --now law-intranet`
  - `sudo systemctl status law-intranet`

- 6) Install Nginx config:
  - If your Nginx layout has `sites-available`:
    - `sudo cp deploy/ubuntu/nginx/law-intranet.conf /etc/nginx/sites-available/law-intranet.conf`
    - `sudo ln -sf /etc/nginx/sites-available/law-intranet.conf /etc/nginx/sites-enabled/law-intranet.conf`
    - `sudo rm -f /etc/nginx/sites-enabled/default`
  - If your Nginx layout does not have `sites-available` (conf.d layout):
    - `sudo mkdir -p /etc/nginx/conf.d`
    - `sudo cp deploy/ubuntu/nginx/law-intranet.conf /etc/nginx/conf.d/law-intranet.conf`
    - `sudo rm -f /etc/nginx/conf.d/default.conf`
  - `server_name` is preconfigured for `elf-ai-demo.co.za` and `www.elf-ai-demo.co.za`.
  - `sudo nginx -t`
  - `sudo systemctl enable --now nginx`
  - `sudo systemctl reload nginx`

- 7) Firewall / networking:
  - Allow inbound `80` and `443` (security group or host firewall).
  - Optional host firewall: `sudo ufw allow 'Nginx Full' && sudo ufw enable`
  - If using ALB, set health check path to `/healthz`.

- 8) TLS:
  - Attach ACM cert on ALB or configure Certbot on Nginx.

- 9) Operations:
  - Logs: `sudo journalctl -u law-intranet -f`
  - App health: `curl http://127.0.0.1/healthz`
  - After each code update: `source venv/bin/activate && pip install -r requirements.txt && flask --app app.py db upgrade -d migrations && sudo systemctl restart law-intranet`
  - Keep backups for DB and uploaded files (`uploads/`), or move uploads to S3.
