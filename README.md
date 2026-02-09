# Law Firm Intranet (ELF demo)

This is a functional intranet website for a law firm:
- User authentication + roles (admin/lawyer/staff/paralegal)
- Matters with team membership
- Tasks per matter (Todo/Doing/Done)
- Document upload/download per matter with SHA-256 integrity hash
- Contacts directory
- Knowledge base (internal articles)
- Search across core objects
- Audit log (admin view)

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export FLASK_SECRET_KEY="change-me"
# optional: export DATABASE_URL="postgresql+psycopg://..."
python app.py init-db

python app.py create-user --email admin@firm.local --password "ChangeMeNow!" --role admin --name "Admin User"
python app.py run --debug
```

Open: http://127.0.0.1:5000

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

- `app.py`: entrypoint + CLI (`run`, `init-db`, `create-user`)
- `intranet/__init__.py`: Flask app factory and extension wiring
- `intranet/config.py`: environment parsing and config constants
- `intranet/models.py`: SQLAlchemy models
- `intranet/helpers.py`: shared business helpers (audit, access checks, file hash helpers)
- `intranet/routes/`: route modules split by domain (`auth`, `matters`, `content`, `admin`)
- `intranet/templates/`: Jinja templates split by domain (`auth`, `matters`, `content`, `admin`, `errors`)
- `intranet/security.py`: security headers and error handlers

## Production notes

- Set required env vars:
  - `FLASK_ENV=production`
  - `FLASK_SECRET_KEY=<long-random-secret>`
  - `DATABASE_URL=<postgresql+psycopg://...>`
  - `TRUST_PROXY=true` when behind a reverse proxy/load balancer
- Run with Gunicorn (example):
  - `gunicorn --workers 3 --bind 0.0.0.0:8000 app:app`
- Put the service behind HTTPS (Nginx/Caddy/Cloudflare) and ideally SSO/MFA.
- Store documents in object storage (S3) with encryption + lifecycle + immutable backups.
- Ship audit logs to centralized logging and retention policies.
