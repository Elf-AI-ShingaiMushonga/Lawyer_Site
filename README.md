# Law Firm Intranet (ELF demo)

This is a functional intranet website for a law firm:
- User authentication + roles (admin/lawyer/staff/paralegal)
- MFA (TOTP + backup codes), session registry, and internal SSO-like auth endpoints
- Admin user provisioning from the web UI (`/admin/users`)
- Firm settings and rule administration (`/admin/settings/*`, `/admin/templates/*`, `/admin/rules/*`)
- Matters with team membership
- User-personalized matter shortcuts (pin matters + recently viewed history)
- Matter intake/workspace/parties/notes/stage transitions/closing workflows
- Matter executive summaries (objective, risk, budget, outcome, latest update)
- Matter timelines (filings, hearings, milestones, client updates)
- Human-readable matter activity feed
- Docketing and calendaring (`/calendar/*`, `/deadlines/*`)
- SLA-driven Priority Inbox (client response lag, intake follow-up due, and unbilled approved time leakage) with admin-configurable thresholds
- Tasks per matter (Todo/Doing/Done)
- Task templates, dependencies, checklists, approvals, recurrence
- Document upload/download with metadata (category, version, stage, owner, privilege) and SHA-256 integrity hash
- DMS normalization (containers + versions + locking + productions + Bates)
- Timekeeping (timers, manual entries, review/lock flow)
- Billing (rates, invoice generation, approvals, PDF, LEDES)
- Expenses with receipt handling and approvals
- Trust accounting (ledgers, deposits, disbursements, transfers, reconciliations)
- CRM/intake (leads, conflict checks, engagement workflows)
- CRM follow-up quick actions (mark done/reopen directly from lead and dashboard workflow queues)
- Scheduled Priority Inbox reminder digests (background jobs for active role-based triage notifications)
- Curated client portal (auth, scoped matter views, messages, uploads, invoices, payments)
- Analytics dashboards (utilization, realization, EHR, workload, profitability, forecast, burnout)
- Ops controls (backup status/run, restore verification, DR targets)
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

# optional background processing
python app.py scheduler --seed-defaults
python app.py worker --max-jobs 100
```

Open: http://127.0.0.1:5000

Demo routing:
- `/` is the demo hub landing page.
- `Law Firm Intranet` routes to `/login`.
- `UFC Prediction` routes to `/ufc/` and mounts `UFC_Elf/app.py` inside this app.

## Demo data seed (recommended for client walkthroughs)

Use the built-in seed command to prepopulate realistic records:

```bash
# make sure you seed the same DB your app uses (especially on Ubuntu/systemd)
set -a; source .env; set +a
flask --app app.py db upgrade -d migrations

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
- Matter/case portfolio with stage, risk, parties, notes, and timeline activity
- Timeline events and activity-feed entries
- Governance incident/change records
- Rich sample files (`.pdf`, `.docx`, `.txt`)
- DMS containers, versions, OCR text, productions, Bates ranges, and email capture
- Time entries/timers with policy validation events and invoice-ready coding
- Billing transactions (including settled + pending payments), account statements, and audit data
- Trust ledger postings, bank-statement imports, reconciliations, and Section 86 investment/accrual records
- CRM/intake conflict workflows and curated client-portal messages/uploads/invoices
- Office365 + third-party integration settings and export-ready sample data

Common seed error:
- `sqlite3.OperationalError: table matter has no column named objective`
- Cause: schema is older than current models, or `DATABASE_URL` was not loaded so command used default local SQLite.
- Fix:
  - `set -a; source .env; set +a`
  - `flask --app app.py db upgrade -d migrations`
  - re-run `python app.py seed-demo --reset --password "ClientDemo2026!"`
  - if you intentionally use SQLite and want a clean reset: `rm -f intranet.db && flask --app app.py db upgrade -d migrations`

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

## User documentation

- Lawyer user guide: `docs/lawyer_user_guide.ipynb`

## Required production env vars

- `FLASK_ENV=production`
- `FLASK_SECRET_KEY=<long-random-secret>`
- `DATABASE_URL=postgresql+psycopg://...`
- `BACKUP_ENCRYPTION_KEY=<32-byte-urlsafe-base64 or 64-char-hex>`
  - Example generation: `python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`

Production boot guards:
- App startup fails if `DATABASE_URL` is not PostgreSQL.
- App startup fails if `BACKUP_ENCRYPTION_KEY` is missing in production.
- App startup fails when `RATE_LIMIT_STORAGE_URI=memory://` and `GUNICORN_WORKERS>1` unless `ALLOW_IN_MEMORY_RATELIMIT=true`.

Optional but recommended:
- `TRUST_PROXY=true`
- `TRUSTED_PROXY_HOPS=1` (set `2` when running behind ALB + Nginx)
- `FORCE_SECURE_COOKIE=true`
- `RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/0` for multi-worker production
- `RATE_LIMIT_STORAGE_URI=memory://` only for local dev or single-worker setups
- `ALLOW_IN_MEMORY_RATELIMIT=false` (keep false in production unless you intentionally bypass)
- `AUTH_LOGIN_RATE_LIMIT=10/minute`
- `AUTH_REGISTER_RATE_LIMIT=5/hour`
- `ENABLE_SCHEMA_COMPAT_SYNC=false` (recommended in production; run migrations explicitly)
- `SESSION_TOUCH_INTERVAL_SECONDS=60`
- `DB_POOL_SIZE=5`
- `DB_MAX_OVERFLOW=10`
- `DB_POOL_TIMEOUT_SECONDS=30`
- `GUNICORN_WORKERS=3`
- `GUNICORN_THREADS=2`
- `GUNICORN_TIMEOUT=60`
- `WORKER_LOOP_SLEEP_SECONDS=3`
- `SCHEDULER_LOOP_SLEEP_SECONDS=30`

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
  - Gunicorn + worker + scheduler managed by systemd, Nginx reverse proxy on port 80/443
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
    - `sudo systemctl status law-intranet-worker`
    - `sudo systemctl status law-intranet-scheduler`
    - `sudo systemctl status nginx`
    - `curl http://127.0.0.1/healthz`
  - Deployment readiness smoke checks:
    - `curl -I http://127.0.0.1/healthz`
    - `curl -I http://127.0.0.1/login`
    - `sudo journalctl -u law-intranet -n 100 --no-pager`
    - `sudo journalctl -u law-intranet-worker -n 100 --no-pager`

- 1) Install system packages:
  - `sudo apt update -y`
  - `sudo apt install -y python3 python3-venv python3-pip git nginx redis-server postgresql-client`

- 2) Clone app and install Python deps:
  - `git clone <your-github-repo-url> /home/ubuntu/Lawyer_Site`
  - `cd /home/ubuntu/Lawyer_Site`
  - `python3 -m venv venv`
  - `source venv/bin/activate`
  - `pip install -r requirements.txt`

- 3) Configure environment:
  - `cp .env.example .env`
  - Generate app secret: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
  - Generate backup key: `python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`
  - Edit `.env` with real `FLASK_SECRET_KEY`, `DATABASE_URL`, and `BACKUP_ENCRYPTION_KEY`.
  - If `DATABASE_URL` password contains reserved URL chars (`@:/?#[]`), URL-encode it.
  - Keep `RATE_LIMIT_STORAGE_URI=redis://127.0.0.1:6379/0` when running multiple Gunicorn workers.
  - Set `TRUSTED_PROXY_HOPS=2` only if request path is `Client -> ALB -> Nginx -> Gunicorn`.

- 4) Initialize DB and bootstrap admin:
  - `source venv/bin/activate`
  - `flask --app app.py db upgrade -d migrations`
  - `python app.py create-user --email shingai.mushonga@elf-ai.co.za --password "<strong-password>" --role admin --name "Admin User"`

- 5) Install systemd service:
  - `sudo cp deploy/ubuntu/systemd/law-intranet.service /etc/systemd/system/law-intranet.service`
  - `sudo cp deploy/ubuntu/systemd/law-intranet-worker.service /etc/systemd/system/law-intranet-worker.service`
  - `sudo cp deploy/ubuntu/systemd/law-intranet-scheduler.service /etc/systemd/system/law-intranet-scheduler.service`
  - Rewrite service paths for your actual app directory:
    - `sudo sed -i "s#/home/ubuntu/Lawyer_Site#/home/ubuntu/<app-dir>#g" /etc/systemd/system/law-intranet.service`
    - `sudo sed -i "s#/home/ubuntu/Lawyer_Site#/home/ubuntu/<app-dir>#g" /etc/systemd/system/law-intranet-worker.service`
    - `sudo sed -i "s#/home/ubuntu/Lawyer_Site#/home/ubuntu/<app-dir>#g" /etc/systemd/system/law-intranet-scheduler.service`
  - Gunicorn tuning comes from `deploy/ubuntu/gunicorn.conf.py` and env vars in `.env`.
  - `sudo systemctl daemon-reload`
  - `sudo systemctl enable --now redis-server`
  - `sudo systemctl enable --now law-intranet`
  - `sudo systemctl enable --now law-intranet-worker`
  - `sudo systemctl enable --now law-intranet-scheduler`
  - `sudo systemctl status law-intranet`
  - `sudo systemctl status law-intranet-worker`
  - `sudo systemctl status law-intranet-scheduler`

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
  - Update `server_name` in whichever file you installed (`/etc/nginx/sites-available/law-intranet.conf` or `/etc/nginx/conf.d/law-intranet.conf`).
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
  - Worker logs: `sudo journalctl -u law-intranet-worker -f`
  - Scheduler logs: `sudo journalctl -u law-intranet-scheduler -f`
  - App health: `curl http://127.0.0.1/healthz`
  - After each code update: `source venv/bin/activate && pip install -r requirements.txt && flask --app app.py db upgrade -d migrations && sudo systemctl restart law-intranet law-intranet-worker law-intranet-scheduler`
  - Keep backups for DB and uploaded files (`uploads/`), or move uploads to S3.
