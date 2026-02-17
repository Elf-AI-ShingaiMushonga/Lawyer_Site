# Release Checklist

Use this checklist for each Ubuntu/EC2 production release.

## 1. Pre-Deploy

- [ ] Confirm branch/commit to deploy and tag it.
- [ ] Confirm `.env` exists and includes all required production keys:
  - `FLASK_ENV=production`
  - `FLASK_SECRET_KEY`
  - `DATABASE_URL` (PostgreSQL)
  - `BACKUP_ENCRYPTION_KEY`
  - `RATE_LIMIT_STORAGE_URI` (Redis for multi-worker)
  - proxy/cookie settings (`TRUST_PROXY`, `TRUSTED_PROXY_HOPS`, `FORCE_SECURE_COOKIE`)
- [ ] Verify DB backup snapshot exists and is recent.
- [ ] Verify security group ingress is correct (`80/443` only public, DB private).
- [ ] Run app test suite locally or in CI before deploy:
  - `PYTHONPATH=. pytest -q`

## 2. Application Update

- [ ] Pull latest code on server:
  - `git fetch --all --tags`
  - `git checkout <release-tag-or-commit>`
- [ ] Activate virtualenv and install dependencies:
  - `source venv/bin/activate`
  - `pip install -r requirements.txt`

## 3. Database and Seed Controls

- [ ] Run migrations:
  - `flask --app app.py db upgrade -d migrations`
- [ ] (Optional demo environments only) reseed:
  - `python app.py seed-demo --reset --password "<strong-password>"`
- [ ] Validate schema state:
  - `flask --app app.py db current -d migrations`

## 4. Service Restart

- [ ] Restart application service:
  - `sudo systemctl restart law-intranet`
- [ ] Restart background worker service:
  - `sudo systemctl restart law-intranet-worker`
- [ ] Restart scheduler service:
  - `sudo systemctl restart law-intranet-scheduler`
- [ ] Restart/reload reverse proxy:
  - `sudo systemctl reload nginx`
- [ ] Confirm required services are healthy:
  - `sudo systemctl status law-intranet --no-pager`
  - `sudo systemctl status law-intranet-worker --no-pager`
  - `sudo systemctl status law-intranet-scheduler --no-pager`
  - `sudo systemctl status nginx --no-pager`
  - `sudo systemctl status redis-server --no-pager`

## 5. Health and Readiness

- [ ] Internal app health check:
  - `curl -i http://127.0.0.1:8000/healthz`
- [ ] Internal readiness check:
  - `curl -i http://127.0.0.1:8000/readyz`
- [ ] Public endpoint health check:
  - `curl -i https://<domain>/healthz`
  - `curl -i https://<domain>/readyz`

## 6. Smoke Tests

- [ ] Login + MFA flow works.
- [ ] Matter directory list/search/sort works.
- [ ] Calendar pages (`my`, `team`, `matter`) load and actions work.
- [ ] DMS version page loads and file actions work.
- [ ] Billing invoice generation and detail page work.
- [ ] Trust ledger and reconciliation pages load.
- [ ] Portal login and uploads page load.
- [ ] No 5xx errors during smoke run.

## 7. Logs and Monitoring

- [ ] Check app logs:
  - `sudo journalctl -u law-intranet -n 200 --no-pager`
- [ ] Check nginx logs:
  - `sudo tail -n 200 /var/log/nginx/error.log`
- [ ] Verify queue/job health on ops dashboard.
- [ ] Verify backup freshness and last restore verification status.

## 8. Rollback Plan

- [ ] Keep previous release tag available on host.
- [ ] If rollback needed:
  - checkout previous tag
  - reinstall deps if required
  - run reverse migration only if explicitly validated
  - restart services
- [ ] Re-run `healthz` and `readyz` checks after rollback.

## 9. Sign-Off

- [ ] Product/ops sign-off recorded.
- [ ] Release notes updated in `README.md` or deployment log.
- [ ] Incident follow-ups captured for any deviations.
