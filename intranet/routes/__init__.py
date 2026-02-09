from .admin import register_admin_routes
from .auth import register_auth_routes
from .content import register_content_routes
from .matters import register_matter_routes


def register_routes(app):
    register_auth_routes(app)
    register_matter_routes(app)
    register_content_routes(app)
    register_admin_routes(app)
