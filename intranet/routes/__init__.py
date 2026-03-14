from .admin import register_admin_routes
from .admin_settings import register_admin_settings_routes
from .analytics import register_analytics_routes
from .auth import register_auth_routes
from .auth_plus import register_auth_plus_routes
from .billing import register_billing_routes
from .calendaring import register_calendar_routes
from .content import register_content_routes
from .crm import register_crm_routes
from .dms import register_dms_routes
from .expenses import register_expenses_routes
from .matters import register_matter_routes
from .matters_plus import register_matters_plus_routes
from .ops import register_ops_routes
from .ops_plus import register_ops_plus_routes
from .ops_service import register_ops_service_routes
from .portal import register_portal_routes
from .timekeeping import register_timekeeping_routes
from .trust_accounting import register_trust_accounting_routes
from .workflow import register_workflow_routes


def register_routes(app):
    register_ops_routes(app)
    register_auth_routes(app)
    register_auth_plus_routes(app)
    register_matter_routes(app)
    register_matters_plus_routes(app)
    register_calendar_routes(app)
    register_workflow_routes(app)
    register_dms_routes(app)
    register_timekeeping_routes(app)
    register_billing_routes(app)
    register_expenses_routes(app)
    register_trust_accounting_routes(app)
    register_crm_routes(app)
    register_portal_routes(app)
    register_analytics_routes(app)
    register_content_routes(app)
    register_admin_routes(app)
    register_admin_settings_routes(app)
    register_ops_plus_routes(app)
    register_ops_service_routes(app)
