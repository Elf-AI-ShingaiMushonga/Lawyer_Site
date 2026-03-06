from __future__ import annotations

import datetime as dt

from intranet.cli import recover_user_mfa
from intranet.extensions import db
from intranet.mfa import hash_backup_code
from intranet.models import User, UserMFABackupCode


def _seed_user(email: str, *, mfa_enabled: bool, mfa_secret: str | None) -> User:
    user = User(
        email=email,
        full_name="MFA Recovery User",
        role="director",
        password_hash="x",
        mfa_enabled=mfa_enabled,
        mfa_secret=mfa_secret,
        failed_login_attempts=4,
        locked_until=dt.datetime.utcnow() + dt.timedelta(minutes=5),
        last_failed_login_at=dt.datetime.utcnow(),
    )
    user.set_password("StrongPassword123!")
    db.session.add(user)
    db.session.flush()
    db.session.add(
        UserMFABackupCode(
            user_id=user.id,
            code_hash=hash_backup_code("OLD-001"),
        )
    )
    db.session.commit()
    return user


def test_recover_user_mfa_rotates_secret_and_backup_codes(app):
    with app.app_context():
        user = _seed_user("recover-mfa@example.com", mfa_enabled=True, mfa_secret="JBSWY3DPEHPK3PXP")

        result = recover_user_mfa(app, user.email, disable=False)

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        backup_rows = UserMFABackupCode.query.filter_by(user_id=user.id, used_at=None).all()

        assert refreshed is not None
        assert refreshed.mfa_enabled is True
        assert refreshed.mfa_secret == result["mfa_secret"]
        assert refreshed.failed_login_attempts == 0
        assert refreshed.locked_until is None
        assert refreshed.last_failed_login_at is None
        assert isinstance(result["otpauth_uri"], str)
        assert "otpauth://totp/" in str(result["otpauth_uri"])
        assert len(result["backup_codes"]) == 10
        assert len(backup_rows) == 10


def test_recover_user_mfa_can_disable_mfa(app):
    with app.app_context():
        user = _seed_user("recover-disable@example.com", mfa_enabled=True, mfa_secret="JBSWY3DPEHPK3PXP")

        result = recover_user_mfa(app, user.email, disable=True)

        db.session.expire_all()
        refreshed = db.session.get(User, user.id)
        backup_count = UserMFABackupCode.query.filter_by(user_id=user.id, used_at=None).count()

        assert refreshed is not None
        assert refreshed.mfa_enabled is False
        assert refreshed.mfa_secret is None
        assert backup_count == 0
        assert result["mfa_secret"] is None
        assert result["otpauth_uri"] is None
        assert result["backup_codes"] == []
