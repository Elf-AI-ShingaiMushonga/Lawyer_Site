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
- `intranet/routes/`: route modules split by domain (`auth`, `matters`, `content`, `admin`, `ops`)
- `intranet/templates/`: Jinja templates split by domain (`auth`, `matters`, `content`, `admin`, `errors`)
- `intranet/security.py`: security headers and error handlers
- `deploy/ubuntu/`: Ubuntu deployment artifacts (cloud-init, systemd service, Nginx config)

## Ubuntu production deployment

- Assumptions:
  - Ubuntu 22.04+ (VM or EC2 Ubuntu AMI)
  - App path: `/home/ubuntu/law_firm_intranet`
  - Gunicorn managed by systemd, Nginx reverse proxy on port 80/443
  - Optional ALB health check path: `/healthz`

- One-click bootstrap with cloud-init (recommended for new EC2 instances):
  - Use `deploy/ubuntu/cloud-init.yaml` as EC2 user data.
  - Replace placeholders in that file first:
    - `REPO_URL`
    - `APP_DOMAIN`
    - `FLASK_SECRET_KEY`
    - `DATABASE_URL`
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
  - `git clone <your-github-repo-url> /home/ubuntu/law_firm_intranet`
  - `cd /home/ubuntu/law_firm_intranet`
  - `python3 -m venv venv`
  - `source venv/bin/activate`
  - `pip install -r requirements.txt`

- 3) Configure environment:
  - `cp .env.example .env`
  - Edit `.env` with real `FLASK_SECRET_KEY` and `DATABASE_URL`.

- 4) Initialize DB and bootstrap admin:
  - `source venv/bin/activate`
  - `python app.py init-db`
  - `python app.py create-user --email admin@firm.local --password "<strong-password>" --role admin --name "Admin User"`

- 5) Install systemd service:
  - `sudo cp deploy/ubuntu/systemd/law-intranet.service /etc/systemd/system/law-intranet.service`
  - `sudo systemctl daemon-reload`
  - `sudo systemctl enable --now law-intranet`
  - `sudo systemctl status law-intranet`

- 6) Install Nginx config:
  - `sudo cp deploy/ubuntu/nginx/law-intranet.conf /etc/nginx/sites-available/law-intranet.conf`
  - Edit `server_name` in `/etc/nginx/sites-available/law-intranet.conf`.
  - `sudo ln -sf /etc/nginx/sites-available/law-intranet.conf /etc/nginx/sites-enabled/law-intranet.conf`
  - `sudo rm -f /etc/nginx/sites-enabled/default`
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
  - Keep backups for DB and uploaded files (`uploads/`), or move uploads to S3.
