from __future__ import annotations

from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

HAS_FLASK_MIGRATE = True
HAS_FLASK_LIMITER = True

try:
    from flask_migrate import Migrate
except ImportError:
    HAS_FLASK_MIGRATE = False

    class Migrate:  # pragma: no cover - fallback for missing optional dependency
        def __init__(self, *args, **kwargs):
            pass

        def init_app(self, *args, **kwargs):
            return None

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except ImportError:
    HAS_FLASK_LIMITER = False

    def get_remote_address():  # pragma: no cover - fallback for missing optional dependency
        return "127.0.0.1"

    class Limiter:  # pragma: no cover - fallback for missing optional dependency
        def __init__(self, *args, **kwargs):
            pass

        def init_app(self, *args, **kwargs):
            return None

        def limit(self, *args, **kwargs):
            def decorator(fn):
                return fn

            return decorator


db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate(compare_type=True)
limiter = Limiter(key_func=get_remote_address, default_limits=[])
