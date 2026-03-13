from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

from flask import flash, redirect, request, session, url_for
from flask_login import current_user, login_required

from ..extensions import db, limiter
from ..helpers import audit, register_user_session, revoke_trusted_device, revoke_user_session
from ..mfa import build_otpauth_uri, check_backup_code, generate_backup_codes, generate_totp_secret, hash_backup_code, verify_totp
from ..models import TrustedDevice, UserMFABackupCode, UserSession
from ..templates import page


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
