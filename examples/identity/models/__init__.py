"""Register every model on one metadata registry; public imports remain stable."""

from examples.identity.database import Base  # noqa: F401

from .accounts import (
    User,  # noqa: F401
    UserIdentity,  # noqa: F401
    UserProfile,  # noqa: F401
    _normalise_profile_email,  # noqa: F401
)
from .associations import (
    group_features_table,  # noqa: F401
    site_groups_table,  # noqa: F401
    user_sites_table,  # noqa: F401
)
from .common import (
    DEPLOYMENT_STATUS_ACTIVE,  # noqa: F401
    DEPLOYMENT_STATUS_REVOKED,  # noqa: F401
    DEPLOYMENT_STATUS_VALUES,  # noqa: F401
    LEGAL_DOCUMENT_TYPE_AI_TERMS,  # noqa: F401
    LEGAL_DOCUMENT_TYPE_PRIVACY,  # noqa: F401
    LEGAL_DOCUMENT_TYPE_TERMS,  # noqa: F401
    LEGAL_DOCUMENT_TYPES,  # noqa: F401
    TENANT_STATUS_ACTIVE,  # noqa: F401
    TENANT_STATUS_DISABLED,  # noqa: F401
    TENANT_STATUS_VALUES,  # noqa: F401
    USER_STATUS_ACTIVE,  # noqa: F401
    USER_STATUS_EMAIL_UNVERIFIED,  # noqa: F401
    USER_STATUS_PENDING_ADMIN_APPROVAL,  # noqa: F401
    USER_STATUS_REJECTED,  # noqa: F401
    USER_STATUS_SUSPENDED,  # noqa: F401
    USER_STATUS_VALUES,  # noqa: F401
    utc_now,  # noqa: F401
)
from .legal import (
    LegalDocument,  # noqa: F401
    UserConsent,  # noqa: F401
)
from .permissions import (
    Feature,  # noqa: F401
    Group,  # noqa: F401
)
from .sites import (
    FcmDeviceToken,  # noqa: F401
    Notification,  # noqa: F401
    Site,  # noqa: F401
    SiteMediaCleanupJob,  # noqa: F401
    SiteNotificationPreference,  # noqa: F401
    StreamConfig,  # noqa: F401
)
from .tenancy import (
    Deployment,  # noqa: F401
    DeploymentEnrollmentCode,  # noqa: F401
    DeploymentEnrollmentCodeAuditLog,  # noqa: F401
    Tenant,  # noqa: F401
    _canonicalise_new_deployment,  # noqa: F401
    _revision_on_deployment_change,  # noqa: F401
)
from .violations import (
    Violation,  # noqa: F401
    ViolationFeedback,  # noqa: F401
    ViolationReviewAuditLog,  # noqa: F401
)
from .recovery import AccountActionToken  # noqa: F401
