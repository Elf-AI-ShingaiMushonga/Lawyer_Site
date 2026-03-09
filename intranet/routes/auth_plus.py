from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import hashlib
import secrets
from urllib.parse import urlencode

from flask import flash, jsonify, redirect, request, session, url_for
from flask_login import current_user, login_required
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db, limiter
from ..helpers import audit, register_user_session, revoke_trusted_device, revoke_user_session
from ..mfa import build_otpauth_uri, check_backup_code, generate_backup_codes, generate_totp_secret, hash_backup_code, verify_totp
from ..models import SSOApplication, SSOAuthorizationCode, SSOToken, TrustedDevice, User, UserMFABackupCode, UserSession
from ..roles import role_is_admin
from ..templates import page


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def register_auth_plus_routes(app):
    @app.route("/auth/mfa/setup", methods=["GET", "POST"])
    @login_required
    def auth_mfa_setup():
        temp_secret = session.get("_mfa_setup_secret") or current_user.mfa_secret
        if not temp_secret:
            temp_secret = generate_totp_secret()
            session["_mfa_setup_secret"] = temp_secret

        if request.method == "POST":
            code = (request.form.get("code") or "").strip()
            if not verify_totp(temp_secret, code):
                flash("Invalid MFA code.", "warning")
                return redirect(url_for("auth_mfa_setup"))

            current_user.mfa_secret = temp_secret
            current_user.mfa_enabled = True
            UserMFABackupCode.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
            codes = generate_backup_codes()
            for c in codes:
                db.session.add(UserMFABackupCode(user_id=current_user.id, code_hash=hash_backup_code(c)))
            db.session.commit()

            session.pop("_mfa_setup_secret", None)
            session["_new_backup_codes"] = codes
            audit("mfa_enabled", "User", current_user.id)
            flash("MFA enabled.", "info")
            return redirect(url_for("auth_mfa_backup_codes"))

        return page(
            "MFA Setup",
            "auth_plus/mfa_setup.html",
            temp_secret=temp_secret,
            otpauth_uri=build_otpauth_uri(temp_secret, current_user.email),
            mfa_enabled=current_user.mfa_enabled,
        )

    @app.post("/auth/mfa/verify")
    @login_required
    def auth_mfa_verify():
        code = (request.form.get("code") or "").strip()
        backup_code = (request.form.get("backup_code") or "").strip().upper()

        verified = False
        if current_user.mfa_secret and verify_totp(current_user.mfa_secret, code):
            verified = True
        elif backup_code:
            backup_rows = UserMFABackupCode.query.filter_by(user_id=current_user.id, used_at=None).all()
            for row in backup_rows:
                if check_backup_code(row.code_hash, backup_code):
                    row.used_at = utc_now()
                    verified = True
                    break

        if not verified:
            flash("MFA verification failed.", "warning")
            return redirect(url_for("auth_mfa_setup"))

        session["mfa_verified_at"] = utc_now().isoformat()
        register_user_session(current_user.id)
        db.session.commit()
        audit("mfa_verified", "User", current_user.id)
        flash("MFA verified.", "info")
        return redirect(url_for("dashboard"))

    @app.route("/auth/mfa/backup-codes", methods=["GET", "POST"])
    @login_required
    def auth_mfa_backup_codes():
        if request.method == "POST":
            codes = generate_backup_codes()
            UserMFABackupCode.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
            for code in codes:
                db.session.add(UserMFABackupCode(user_id=current_user.id, code_hash=hash_backup_code(code)))
            db.session.commit()
            session["_new_backup_codes"] = codes
            audit("mfa_backup_codes_regenerated", "User", current_user.id)
            flash("Backup codes regenerated.", "info")
            return redirect(url_for("auth_mfa_backup_codes"))

        backup_codes = session.pop("_new_backup_codes", None)
        active_code_count = UserMFABackupCode.query.filter_by(user_id=current_user.id, used_at=None).count()
        return page(
            "MFA Backup Codes",
            "auth_plus/mfa_backup_codes.html",
            backup_codes=backup_codes,
            active_code_count=active_code_count,
        )

    @app.get("/auth/sessions")
    @login_required
    def auth_sessions():
        rows = (
            UserSession.query.filter_by(user_id=current_user.id)
            .order_by(UserSession.created_at.desc())
            .limit(200)
            .all()
        )
        devices = (
            TrustedDevice.query.filter_by(user_id=current_user.id)
            .order_by(TrustedDevice.last_seen_at.desc(), TrustedDevice.created_at.desc())
            .limit(200)
            .all()
        )
        return page("Sessions", "auth_plus/sessions.html", sessions=rows, devices=devices)

    @app.post("/auth/sessions/<int:session_id>/revoke")
    @login_required
    def auth_session_revoke(session_id: int):
        if not revoke_user_session(session_id):
            flash("Session not found.", "warning")
        else:
            audit("session_revoke", "UserSession", session_id)
            flash("Session revoked.", "info")
        return redirect(url_for("auth_sessions"))

    @app.post("/auth/devices/<int:device_id>/revoke")
    @login_required
    def auth_device_revoke(device_id: int):
        if not revoke_trusted_device(device_id, current_user.id):
            flash("Trusted device not found.", "warning")
        else:
            audit("trusted_device_revoke", "TrustedDevice", device_id)
            flash("Trusted device revoked.", "info")
        return redirect(url_for("auth_sessions"))

    @app.get("/auth/sso/authorize")
    @login_required
    def auth_sso_authorize():
        client_id = (request.args.get("client_id") or "").strip()
        redirect_uri = (request.args.get("redirect_uri") or "").strip()
        state = (request.args.get("state") or "").strip()
        scope = (request.args.get("scope") or "openid profile email").strip()

        app_row = SSOApplication.query.filter_by(client_id=client_id, is_active=True).first()
        if not app_row or app_row.redirect_uri != redirect_uri:
            return jsonify({"error": "invalid_client"}), 400

        raw_code = secrets.token_urlsafe(24)
        code_hash = _hash_token(raw_code)
        auth_code = SSOAuthorizationCode(
            app_id=app_row.id,
            user_id=current_user.id,
            code_hash=code_hash,
            scope=scope,
            expires_at=utc_now() + dt.timedelta(minutes=5),
        )
        db.session.add(auth_code)
        db.session.commit()

        params = {"code": raw_code}
        if state:
            params["state"] = state
        return redirect(f"{redirect_uri}?{urlencode(params)}")

    @app.post("/auth/sso/token")
    @limiter.limit(lambda: app.config.get("AUTH_SSO_TOKEN_RATE_LIMIT", "60/minute"), methods=["POST"])
    def auth_sso_token():
        grant_type = (request.form.get("grant_type") or "authorization_code").strip()
        client_id = (request.form.get("client_id") or "").strip()
        client_secret = request.form.get("client_secret") or ""
        code = (request.form.get("code") or "").strip()

        if grant_type != "authorization_code":
            return jsonify({"error": "unsupported_grant_type"}), 400

        app_row = SSOApplication.query.filter_by(client_id=client_id, is_active=True).first()
        if not app_row or not check_password_hash(app_row.client_secret_hash, client_secret):
            return jsonify({"error": "invalid_client"}), 401

        auth_code = (
            SSOAuthorizationCode.query.filter_by(app_id=app_row.id, code_hash=_hash_token(code))
            .order_by(SSOAuthorizationCode.id.desc())
            .first()
        )
        if not auth_code or auth_code.consumed_at is not None or auth_code.expires_at < utc_now():
            return jsonify({"error": "invalid_grant"}), 400

        access_raw = secrets.token_urlsafe(32)
        refresh_raw = secrets.token_urlsafe(32)
        token = SSOToken(
            app_id=app_row.id,
            user_id=auth_code.user_id,
            access_token_hash=_hash_token(access_raw),
            refresh_token_hash=_hash_token(refresh_raw),
            scope=auth_code.scope,
            expires_at=utc_now() + dt.timedelta(hours=1),
        )
        auth_code.consumed_at = utc_now()
        db.session.add(token)
        db.session.commit()

        return jsonify(
            {
                "access_token": access_raw,
                "refresh_token": refresh_raw,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": token.scope,
            }
        )

    @app.get("/auth/sso/userinfo")
    def auth_sso_userinfo():
        auth_header = request.headers.get("Authorization") or ""
        token_raw = ""
        if auth_header.lower().startswith("bearer "):
            token_raw = auth_header.split(" ", 1)[1].strip()

        if not token_raw:
            return jsonify({"error": "invalid_token"}), 401

        token = SSOToken.query.filter_by(access_token_hash=_hash_token(token_raw)).first()
        if not token or token.revoked_at is not None or token.expires_at < utc_now():
            return jsonify({"error": "invalid_token"}), 401

        user = db.session.get(User, token.user_id)
        if not user:
            return jsonify({"error": "invalid_token"}), 401

        return jsonify(
            {
                "sub": str(user.id),
                "email": user.email,
                "name": user.full_name,
                "role": user.role,
            }
        )

    @app.route("/admin/settings/sso-apps", methods=["GET", "POST"])
    @login_required
    def admin_sso_apps():
        if not role_is_admin(getattr(current_user, "role", None)):
            return page("Forbidden", "errors/403.html"), 403

        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            redirect_uri = (request.form.get("redirect_uri") or "").strip()
            client_secret = request.form.get("client_secret") or ""
            if not name or not redirect_uri or len(client_secret) < 12:
                flash("Name, redirect URI, and strong client secret are required.", "warning")
                return redirect(url_for("admin_sso_apps"))
            app_row = SSOApplication(
                name=name,
                client_id=secrets.token_urlsafe(16),
                client_secret_hash=generate_password_hash(client_secret),
                redirect_uri=redirect_uri,
                is_active=True,
            )
            db.session.add(app_row)
            db.session.commit()
            audit("sso_app_create", "SSOApplication", app_row.id)
            flash("SSO application created.", "info")
            return redirect(url_for("admin_sso_apps"))

        apps = SSOApplication.query.order_by(SSOApplication.created_at.desc()).limit(200).all()
        return page("SSO Applications", "auth_plus/sso_apps.html", apps=apps)
