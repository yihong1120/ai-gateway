"""Register every model on one metadata registry; public imports remain stable."""
from __future__ import annotations

from .accounts import _normalise_profile_email  # noqa: F401 - public re-export
from .accounts import User  # noqa: F401 - public re-export
from .accounts import UserIdentity  # noqa: F401 - public re-export
from .accounts import UserProfile  # noqa: F401 - public re-export
from .associations import group_features_table  # noqa: F401 - public re-export
from .associations import site_groups_table  # noqa: F401 - public re-export
from .associations import user_sites_table  # noqa: F401 - public re-export
from .common import DEPLOYMENT_STATUS_ACTIVE  # noqa: F401 - public re-export
from .common import DEPLOYMENT_STATUS_REVOKED  # noqa: F401 - public re-export
from .common import DEPLOYMENT_STATUS_VALUES  # noqa: F401 - public re-export
from .common import LEGAL_DOCUMENT_TYPE_AI_TERMS  # noqa: F401 - public re-export
from .common import LEGAL_DOCUMENT_TYPE_PRIVACY  # noqa: F401 - public re-export
from .common import LEGAL_DOCUMENT_TYPE_TERMS  # noqa: F401 - public re-export
from .common import LEGAL_DOCUMENT_TYPES  # noqa: F401 - public re-export
from .common import TENANT_STATUS_ACTIVE  # noqa: F401 - public re-export
from .common import TENANT_STATUS_DISABLED  # noqa: F401 - public re-export
from .common import TENANT_STATUS_VALUES  # noqa: F401 - public re-export
from .common import USER_STATUS_ACTIVE  # noqa: F401 - public re-export
from .common import USER_STATUS_EMAIL_UNVERIFIED  # noqa: F401 - public re-export
from .common import USER_STATUS_PENDING_ADMIN_APPROVAL  # noqa: F401 - public re-export
from .common import USER_STATUS_REJECTED  # noqa: F401 - public re-export
from .common import USER_STATUS_SUSPENDED  # noqa: F401 - public re-export
from .common import USER_STATUS_VALUES  # noqa: F401 - public re-export
from .common import utc_now  # noqa: F401 - public re-export
from .legal import LegalDocument  # noqa: F401 - public re-export
from .legal import UserConsent  # noqa: F401 - public re-export
from .permissions import Feature  # noqa: F401 - public re-export
from .permissions import Group  # noqa: F401 - public re-export
from .recovery import AccountActionToken  # noqa: F401
from .sites import FcmDeviceToken  # noqa: F401 - public re-export
from .sites import Notification  # noqa: F401 - public re-export
from .sites import Site  # noqa: F401 - public re-export
from .sites import SiteMediaCleanupJob  # noqa: F401 - public re-export
from .sites import SiteNotificationPreference  # noqa: F401 - public re-export
from .sites import StreamConfig  # noqa: F401 - public re-export
from .tenancy import _canonicalise_new_deployment  # noqa: F401 - public re-export
from .tenancy import _revision_on_deployment_change  # noqa: F401 - public re-export
from .tenancy import Deployment  # noqa: F401 - public re-export
from .tenancy import DeploymentEnrollmentCode  # noqa: F401 - public re-export
from .tenancy import DeploymentEnrollmentCodeAuditLog  # noqa: F401 - public re-export
from .tenancy import Tenant  # noqa: F401 - public re-export
from .violations import Violation  # noqa: F401 - public re-export
from .violations import ViolationFeedback  # noqa: F401 - public re-export
from .violations import ViolationReviewAuditLog  # noqa: F401 - public re-export
from examples.identity.database import Base  # noqa: F401
