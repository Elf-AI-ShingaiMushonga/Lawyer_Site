from __future__ import annotations

from .config import VALID_ROLES, is_valid_email
from .extensions import db
from .models import User


def init_db(app):
    with app.app_context():
        db.create_all()


def create_user(app, email: str, password: str, role: str, full_name: str = "(Unnamed)"):
    email = email.strip().lower()
    if not is_valid_email(email):
        raise SystemExit("Invalid email format")
    if role not in VALID_ROLES:
        raise SystemExit(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")

    with app.app_context():
        if User.query.filter_by(email=email).first():
            raise SystemExit("User already exists")
        user = User(email=email, role=role, full_name=full_name, password_hash="x")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


def run_server(app, host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
    app.run(host=host, port=port, debug=debug)
